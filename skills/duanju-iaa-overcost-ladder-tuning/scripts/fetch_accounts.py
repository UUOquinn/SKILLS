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
"""直连 OLAP 取数，按账户聚合浅度超成本率与联盟总消耗（1 小时窗口）。

    uv run scripts/fetch_accounts.py --output data/accounts_202609231700.csv

取数窗口固定为过去 1 小时（右端对齐到 5 分钟前，规避实时数据回补延迟）。
与决策节奏（每二十分钟一轮）相比窗口更长，相邻两轮窗口有大量
重叠——这是刻意设计，让每次都能看到过去 1 小时全貌。

为什么不在服务端过滤时间窗口：把模板的 __time 过滤改成「最近 N 分钟」会
触发 OLAP 的指标兼容校验（指标维度组合不支持查询）。可行做法是保持模板
原样拉当天分钟粒度数据，再在本地筛窗口。所以模板必须整份原样提交，不要
裁字段。

聚合口径是消耗加权：

    账户超成本率 = Σ(每分钟消耗 × 该分钟超成本率) ÷ Σ(每分钟消耗)
    账户消耗     = Σ(每分钟消耗)

阈值过滤（消耗>=500 且超成本率>=0.20）不在这里做，交给 decide.py——
取数脚本只负责把窗口内所有「首日变现ROI（短剧IAA专用）」账户的原始聚合
结果落盘，撤出判断需要看到「超成本率已经回落到 0.20 以下」的账户。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_STARTED_AT = time.perf_counter()

OLAP_URL = "https://app.example.corp/api/v1/data/query"
SHARE_ID = "64385633"
DATASET_ID = "DS_REALTIME"
REFERER = (
    f"https://app.example.corp/main?enter_from=tianji"
    f"&shareId={SHARE_ID}&dataSetId={DATASET_ID}"
)
TZ = timezone(timedelta(hours=8))

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "references" / "olap_query_template.json"
TIME_FIELD_ID = 2211389

COL_ACCOUNT = "账户ID"
COL_TIME = "__time"
COL_COST = "联盟总消耗"
COL_RATE = "浅度超成本率"


def _dur_ms() -> int:
    return int((time.perf_counter() - _STARTED_AT) * 1000)


def _ok(data: dict[str, Any], msg: str = "") -> int:
    print(json.dumps(
        {"ok": True, "code": 0, "duration_ms": _dur_ms(), "msg": msg, "data": data,
         "next_action": {"type": "CONTINUE", "retryable": False}},
        ensure_ascii=False,
    ))
    return 0


def _fail(error: str, msg: str, code: int = 1, retryable: bool = False) -> int:
    print(json.dumps(
        {"ok": False, "code": code, "error": error, "msg": msg,
         "duration_ms": _dur_ms(),
         "next_action": {"type": "STOP", "retryable": retryable}},
        ensure_ascii=False,
    ))
    return code


def raw_value(cell: Any) -> float:
    """取单元格原始数值。OLAP 返回 {"v": "25元", "r": 24.997}，`r` 才是原始值。"""
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


def build_body(template: dict[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    """重写模板里的日期 filter 为当前窗口所在自然日。找不到字段直接抛错。"""
    body = json.loads(json.dumps(template))
    body["queryTime"] = int(datetime.now(TZ).timestamp() * 1000)

    day_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = end.replace(hour=23, minute=59, second=59, microsecond=999000)
    patched = 0
    for f in body.get("filters") or []:
        if f.get("filterType") != "datetime":
            continue
        fv = f.get("filterValue")
        if not isinstance(fv, dict) or "startTime" not in fv:
            continue
        fv["startTime"] = int(day_start.timestamp() * 1000)
        fv["endTime"] = int(day_end.timestamp() * 1000)
        patched += 1

    if patched == 0:
        raise RuntimeError(
            "模板里找不到日期 filter（filters[].filterType == 'datetime'）。"
            "模板结构可能变了，重新抓一份 references/olap_query_template.json"
        )
    return body


def query_olap(session: Any, body: dict[str, Any]) -> dict[str, Any]:
    resp = session.request(
        "POST", OLAP_URL, json=body,
        headers={
            "Accept": "application/json",
            "Kwaibi-View-Query-Type": "8",
            "bi-request-module": "olap",
            "Referer": REFERER,
        },
        timeout=180,
    )
    final_url = str(getattr(resp, "url", ""))
    content_type = (resp.headers.get("Content-Type") or "").lower()
    is_login_page = (
        "login" in final_url.lower()
        or "sso" in final_url.lower()
        or "text/html" in content_type
    )
    if resp.status_code in (401, 302) or is_login_page:
        raise PermissionError("AUTH_EXPIRED")
    if resp.status_code == 403:
        raise PermissionError("AUTH_FAILED")
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data") or {}
    rows = data.get("dataList") or []
    columns = [c.get("name") for c in data.get("columns") or []]
    if not columns or not rows:
        raise RuntimeError(payload.get("error_msg") or "OLAP 返回空结果")
    return {"columns": columns, "rows": rows}


def aggregate(
    columns: list[str], rows: list[list[Any]], start: datetime, end: datetime,
    account_filter: set[str] | None = None,
) -> dict[str, Any]:
    for col in (COL_ACCOUNT, COL_TIME, COL_COST, COL_RATE):
        if col not in columns:
            raise RuntimeError(
                f"OLAP 返回缺少必需列「{col}」，实际列：{columns}。"
                "看板字段可能被改过，先核对 references/olap_query_template.json"
            )
    i_acc = columns.index(COL_ACCOUNT)
    i_time = columns.index(COL_TIME)
    i_cost = columns.index(COL_COST)
    i_rate = columns.index(COL_RATE)

    left = start.strftime("%Y-%m-%d %H:%M")
    right = end.strftime("%Y-%m-%d %H:%M")

    buckets: dict[str, dict[str, float]] = {}
    used_minutes = 0
    for row in rows:
        stamp = cell_text(row[i_time])
        if not (left <= stamp < right):
            continue
        acc = cell_text(row[i_acc]).strip()
        if not acc:
            continue
        if account_filter is not None and acc not in account_filter:
            continue
        cost = raw_value(row[i_cost])
        rate = raw_value(row[i_rate])
        slot = buckets.setdefault(
            acc, {"cost": 0.0, "weighted_rate": 0.0, "minutes": 0}
        )
        slot["cost"] += cost
        slot["weighted_rate"] += cost * rate
        slot["minutes"] += 1
        used_minutes += 1

    accounts = []
    for acc, slot in sorted(buckets.items()):
        cost = slot["cost"]
        rate = (slot["weighted_rate"] / cost) if cost else None
        accounts.append({
            "account_id": acc,
            "rate": rate,
            "cost": round(cost, 3),
            "minutes": int(slot["minutes"]),
        })
    return {
        "accounts": accounts,
        "window_start": left,
        "window_end": right,
        "minute_rows_used": used_minutes,
    }


def write_csv(path: Path, accounts: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["account_id", "rate", "cost", "minutes"])
        for item in accounts:
            rate = "" if item.get("rate") is None else f"{item['rate']:.10f}"
            writer.writerow(
                [item["account_id"], rate, item["cost"], item["minutes"]]
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="直连 OLAP 取数并按账户聚合（1 小时窗口）")
    parser.add_argument("--output", required=True, help="输出 CSV 路径")
    parser.add_argument("--window-minutes", type=int, default=60, help="窗口长度，默认 60")
    parser.add_argument("--lag-minutes", type=int, default=5, help="右端留白，默认 5")
    parser.add_argument(
        "--accounts", default=None, help="可选：逗号分隔的账户ID列表，只处理这些账户"
    )
    args = parser.parse_args()
    account_filter: set[str] | None = (
        {a.strip() for a in args.accounts.split(",") if a.strip()}
        if args.accounts
        else None
    )

    try:
        if args.window_minutes <= 0 or args.lag_minutes < 0:
            return _fail("INVALID_ARGUMENT", "窗口长度必须为正，右端留白不能为负")
        if not TEMPLATE_PATH.exists():
            return _fail("TEMPLATE_MISSING", f"找不到查询模板 {TEMPLATE_PATH}")

        now = datetime.now(TZ)
        end = now - timedelta(minutes=args.lag_minutes)
        start = end - timedelta(minutes=args.window_minutes)

        from auth_helper.sso_login_client import SmartAuthSession

        template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        session = SmartAuthSession()
        fetched = query_olap(session, build_body(template, start, end))

        result = aggregate(fetched["columns"], fetched["rows"], start, end, account_filter)
        if not result["accounts"]:
            return _fail(
                "EMPTY_WINDOW",
                f"窗口 {result['window_start']}~{result['window_end']} 内没有任何账户数据，"
                "先确认数据是否已回补，不要拿空结果去写策略",
            )

        out = Path(args.output)
        write_csv(out, result["accounts"])
        return _ok(
            {
                "output": str(out),
                "window_start": result["window_start"],
                "window_end": result["window_end"],
                "account_count": len(result["accounts"]),
                "minute_rows_used": result["minute_rows_used"],
                "total_rows_fetched": len(fetched["rows"]),
            },
            f"窗口 {result['window_start']}~{result['window_end']}，"
            f"聚合 {len(result['accounts'])} 个账户",
        )
    except PermissionError as exc:
        code = str(exc)
        hint = (
            "OLAP（olap.example.corp）登录态已失效，请刷新该域名 Auth 后重试"
            if code == "AUTH_EXPIRED"
            else "当前账号无 OLAP 看板 / 接口权限"
        )
        return _fail(code, hint, code=2)
    except KeyboardInterrupt:
        return _fail("INTERRUPTED", "用户中断", code=130)
    except Exception as exc:  # noqa: BLE001
        print(f"[fetch_accounts] {type(exc).__name__}: {exc}", file=sys.stderr)
        return _fail("UNEXPECTED_ERROR", f"{type(exc).__name__}: {exc}", code=1)


if __name__ == "__main__":
    sys.exit(main())
