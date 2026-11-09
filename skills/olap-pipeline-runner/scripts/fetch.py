#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests>=2.31.0,<3", "ks-aimate>=1.0.30"]
# [tool.uv.sources]
# "ks-aimate" = { index = "kuaishou" }
# [[tool.uv.index]]
# name = "kuaishou"
# url = "https://app.example.corp/kuaishou/prod/+simple/"
# publish = false
# ///
"""OLAP 直连取数 + 本地聚合。管道第 1 段。

不含任何业务字段：数据集模板路径、列名映射、窗口长度、聚合口径全部来自
配置文件。
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

OLAP_URL = "https://app.example.corp/api/v1/data/query"
TZ = timezone(timedelta(hours=8))


def _ok(data: dict[str, Any], msg: str = "") -> int:
    print(json.dumps(
        {"ok": True, "code": 0, "msg": msg, "data": data,
         "next_action": {"type": "CONTINUE", "retryable": False}},
        ensure_ascii=False,
    ))
    return 0


def _fail(error: str, msg: str, code: int = 1, retryable: bool = False) -> int:
    print(json.dumps(
        {"ok": False, "code": code, "error": error, "msg": msg,
         "next_action": {"type": "STOP", "retryable": retryable}},
        ensure_ascii=False,
    ))
    return code


def raw_value(cell: Any) -> float:
    """取单元格的原始数值。

    OLAP 返回 `{"v": "25元", "r": 24.997}`：`v` 是带单位的展示值且被四舍五入
    过，`r` 才是原始值。拿 `v` 去算要么因为「元」「%」后缀直接崩，要么精度被
    污染。
    """
    if isinstance(cell, dict):
        cell = cell.get("r", cell.get("v"))
    try:
        return float(cell or 0)
    except (TypeError, ValueError):
        return 0.0


def cell_text(cell: Any) -> str:
    if isinstance(cell, dict):
        return str(cell.get("v") or "")
    return str(cell or "")


def build_body(
    template: dict[str, Any], window_start: datetime, window_end: datetime
) -> dict[str, Any]:
    """把模板里的日期 filter 重写成覆盖取数窗口的自然日范围。

    模板通常是从网页 HAR 抓下来的，`filters[].filterValue.startTime/endTime`
    里写死了抓包当天的日期。不重写就会从次日零点开始一直取到旧数据，而接口
    照样返回 200、数据完整、格式正确——静默拿错日期的数据去写策略，比直接
    报错危险得多。

    所以找不到该字段时直接抛错，不静默继续。
    """
    body = json.loads(json.dumps(template))
    body["queryTime"] = int(datetime.now(TZ).timestamp() * 1000)

    day_start = window_start.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = window_end.replace(hour=23, minute=59, second=59, microsecond=999000)

    rewritten = 0
    for flt in body.get("filters") or []:
        fv = flt.get("filterValue")
        if isinstance(fv, dict) and "startTime" in fv and "endTime" in fv:
            fv["startTime"] = int(day_start.timestamp() * 1000)
            fv["endTime"] = int(day_end.timestamp() * 1000)
            rewritten += 1

    if rewritten == 0:
        raise RuntimeError(
            "模板里找不到日期 filter（filters[].filterValue.startTime/endTime），"
            "看板字段可能被改过。不能继续——否则会拿模板里写死的旧日期取数。"
        )
    return body


def query_olap(session: Any, body: dict[str, Any], referer: str) -> dict[str, Any]:
    resp = session.request(
        "POST", OLAP_URL, json=body,
        headers={
            "Accept": "application/json",
            "Kwaibi-View-Query-Type": "8",
            "bi-request-module": "olap",
            "Referer": referer,
        },
        timeout=180,
    )
    if resp.status_code in (401, 302):
        raise PermissionError("AUTH_EXPIRED")
    if resp.status_code == 403:
        raise PermissionError("AUTH_FAILED")
    resp.raise_for_status()

    payload = resp.json()
    data = payload.get("data") or {}
    rows = data.get("dataList") or []
    columns = [c.get("name") for c in data.get("columns") or []]
    # result=1 伴随 error_msg="canDiff" 是正常返回（同期对比提示），有数据即可用。
    # 真正的失败是拿不到任何列或行。
    if not columns or not rows:
        raise RuntimeError(payload.get("error_msg") or "OLAP 返回空结果")
    return {"columns": columns, "rows": rows}


def aggregate(
    columns: list[str], rows: list[list[Any]],
    cols: dict[str, str], mode: str,
    start: datetime, end: datetime,
) -> dict[str, Any]:
    """按主键把窗口内的分钟明细聚合成一行。

    weighted_mean 是默认口径：Σ(权重×指标) ÷ Σ权重，等价于「总量比总量」。
    算术平均会在各分钟权重差异大时给出相反结论，所以不是默认。
    """
    for role in ("key", "time", "metric"):
        if role not in cols:
            raise RuntimeError(f"配置 fetch.columns 缺少必需角色「{role}」")
    if mode == "weighted_mean" and "weight" not in cols:
        raise RuntimeError("aggregation=weighted_mean 需要配置 fetch.columns.weight")

    for role, name in cols.items():
        if name not in columns:
            raise RuntimeError(
                f"OLAP 返回缺少列「{name}」（配置角色 {role}），实际列：{columns}。"
                "看板字段可能被改过，先核对模板文件"
            )

    i_key = columns.index(cols["key"])
    i_time = columns.index(cols["time"])
    i_metric = columns.index(cols["metric"])
    i_weight = columns.index(cols["weight"]) if "weight" in cols else None

    left = start.strftime("%Y-%m-%d %H:%M")
    right = end.strftime("%Y-%m-%d %H:%M")

    buckets: dict[str, dict[str, float]] = {}
    used = 0
    for row in rows:
        stamp = cell_text(row[i_time])
        if not (left <= stamp < right):
            continue
        key = cell_text(row[i_key]).strip()
        if not key:
            continue
        metric = raw_value(row[i_metric])
        weight = raw_value(row[i_weight]) if i_weight is not None else 1.0
        slot = buckets.setdefault(key, {"weight": 0.0, "acc": 0.0, "rows": 0})
        slot["weight"] += weight
        slot["acc"] += weight * metric
        slot["rows"] += 1
        used += 1

    targets = []
    for key, slot in sorted(buckets.items()):
        total = slot["weight"]
        if mode == "weighted_mean":
            value = (slot["acc"] / total) if total else None
        elif mode == "sum":
            value = slot["acc"]
        elif mode == "mean":
            value = (slot["acc"] / slot["rows"]) if slot["rows"] else None
        else:
            raise RuntimeError(
                f"未知聚合口径「{mode}」，支持 weighted_mean / sum / mean"
            )
        targets.append({
            "key": key, "value": value,
            "weight": round(total, 3), "rows": int(slot["rows"]),
        })

    return {
        "targets": targets, "window_start": left, "window_end": right,
        "rows_used": used,
    }


def write_csv(path: Path, targets: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["key", "value", "current_value", "weight", "rows"])
        for t in targets:
            writer.writerow([
                t["key"],
                "" if t["value"] is None else f"{t['value']:.10f}",
                "", t["weight"], t["rows"],
            ])


def main() -> int:
    parser = argparse.ArgumentParser(description="OLAP 直连取数 + 本地聚合")
    parser.add_argument("--config", required=True, help="pipeline 配置 JSON")
    parser.add_argument("--output", required=True, help="聚合结果 CSV")
    parser.add_argument("--raw-output", default=None, help="原始响应 JSON，强烈建议带上")
    parser.add_argument("--window-minutes", type=int, default=None, help="覆盖配置里的窗口长度")
    parser.add_argument("--lag-minutes", type=int, default=None, help="覆盖配置里的右端留白")
    args = parser.parse_args()

    try:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            return _fail("CONFIG_MISSING", f"找不到配置文件 {cfg_path}")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        fetch_cfg = cfg.get("fetch") or {}

        tpl_rel = fetch_cfg.get("template")
        if not tpl_rel:
            return _fail("CONFIG_INVALID", "配置缺少 fetch.template")
        tpl_path = Path(tpl_rel)
        if not tpl_path.is_absolute():
            tpl_path = cfg_path.parent / tpl_rel
        if not tpl_path.exists():
            return _fail("TEMPLATE_MISSING", f"找不到查询模板 {tpl_path}")

        window = args.window_minutes or fetch_cfg.get("window_minutes", 15)
        lag = args.lag_minutes if args.lag_minutes is not None \
            else fetch_cfg.get("lag_minutes", 5)
        if window <= 0 or lag < 0:
            return _fail("INVALID_ARGUMENT", "窗口长度必须为正，右端留白不能为负")

        now = datetime.now(TZ)
        end = now - timedelta(minutes=lag)
        start = end - timedelta(minutes=window)

        from auth_helper.sso_login_client import SmartAuthSession

        template = json.loads(tpl_path.read_text(encoding="utf-8"))
        referer = fetch_cfg.get("referer") or "https://app.example.corp/"
        session = SmartAuthSession()
        fetched = query_olap(session, build_body(template, start, end), referer)

        if args.raw_output:
            raw = Path(args.raw_output)
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_text(json.dumps(fetched, ensure_ascii=False), encoding="utf-8")

        result = aggregate(
            fetched["columns"], fetched["rows"],
            fetch_cfg.get("columns") or {},
            fetch_cfg.get("aggregation", "weighted_mean"),
            start, end,
        )
        if not result["targets"]:
            return _fail(
                "EMPTY_WINDOW",
                f"窗口 {result['window_start']}~{result['window_end']} 内没有任何数据，"
                "先确认数据是否已回补，不要拿空结果去写入",
            )

        write_csv(Path(args.output), result["targets"])
        return _ok(
            {
                "output": args.output,
                "window_start": result["window_start"],
                "window_end": result["window_end"],
                "target_count": len(result["targets"]),
                "rows_used": result["rows_used"],
                "total_rows_fetched": len(fetched["rows"]),
                "aggregation": fetch_cfg.get("aggregation", "weighted_mean"),
            },
            f"窗口 {result['window_start']}~{result['window_end']}，"
            f"聚合 {len(result['targets'])} 个对象",
        )

    except PermissionError as exc:
        code = str(exc)
        if code == "AUTH_EXPIRED":
            return _fail("AUTH_EXPIRED", "内网 Auth 已失效，请重新完成认证后重试", code=2)
        return _fail("AUTH_FAILED", "无权访问该数据集，确认账号有看板权限", code=2)
    except Exception as exc:  # noqa: BLE001
        print(f"[fetch] {type(exc).__name__}: {exc}", file=sys.stderr)
        return _fail("UNEXPECTED_ERROR", f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
