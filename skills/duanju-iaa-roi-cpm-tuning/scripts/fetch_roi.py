#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests>=2.31.0,<3", "ks-aimate>=1.0.30"]
# [tool.uv.sources]
# "ks-aimate" = { index = "kuaishou" }
# [[tool.uv.index]]
# name = "kuaishou"
# url = "https://internal.example.corp/kuaishou/prod/+simple/"
# publish = false
# ///
"""直连 OLAP 取数并按账户聚合 ROI，替代对话式取数。

    uv run scripts/fetch_roi.py --output data/roi_202609151230.csv

为什么不在服务端过滤时间窗口：实测把模板的 __time 过滤改成「最近 20 分钟」
会触发 OLAP 的指标兼容校验（`指标维度组合不支持查询`）。可行的做法是保持
模板原样拉当天分钟粒度数据（实测 2.4 秒 / 25000+ 行），再在本地筛窗口。
所以模板必须整份原样提交，不要裁字段——裁掉隐藏配置就会被服务端拒。

聚合口径是消耗加权，不是算术平均：

    账户ROI = Σ(每个自然分钟的消耗 × 该分钟ROI) ÷ Σ(每个自然分钟的消耗)

这等价于「总变现 ÷ 总消耗」。算术平均会把小额分钟的极端 ROI 放大，实测
可以让结论从「观察区不动」翻转成「压价」，是会直接写错生产的口径错误。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

OLAP_URL = "https://internal.example.corp/api/v1/data/query"
SHARE_ID = "64385633"
DATASET_ID = "129496"
REFERER = (
    f"https://internal.example.corp/main?enter_from=tianji"
    f"&shareId={SHARE_ID}&dataSetId={DATASET_ID}"
)
TZ = timezone(timedelta(hours=8))

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "references" / "olap_query_template.json"
TIME_FIELD_ID = 2211389

COL_ACCOUNT = "账户ID"
COL_TIME = "__time"
COL_COST = "联盟总消耗"
COL_ROI = "IAA广告变现ROI"
COL_RATE = "浅度超成本率"


def _ok(data: dict[str, Any], msg: str = "") -> int:
    print(json.dumps(
        {"ok": True, "code": 0, "duration_ms": 0, "msg": msg, "data": data,
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

    OLAP 返回 `{"v": "25元", "r": 24.997}`：`v` 是带单位的展示值，`r` 才是
    原始值。拿 `v` 去算会因为「元」「%」这些后缀直接崩，或者被四舍五入过的
    展示值污染精度。
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


def build_body(template: dict[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    """按取数窗口重写模板里的日期 filter。

    模板是从网页抓包来的，`filterValue.startTime/endTime` 是**抓包那天**的
    毫秒时间戳（实测锁在 2026-09-15）。不重写就会一直拉那一天的数据：
    接口照样返回 200 和完整行，但本地按今天的窗口过滤后交集为空，
    表现成「窗口内没有任何账户数据」——或者更糟，跨天时拿昨天的数据调今天的价。

    注意这里是**毫秒时间戳**不是字符串：之前按字符串匹配写过一版修复，
    模板结构一变就静默失效了。所以下面找不到字段直接抛错，不允许静默跳过。
    """
    body = json.loads(json.dumps(template))
    body["queryTime"] = int(datetime.now(TZ).timestamp() * 1000)

    # 取数窗口可能跨天（如 23:55~00:15），按窗口两端所在自然日的首尾取并集。
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
            "模板里找不到日期 filter（filters[].filterType == 'datetime' 且带 "
            "filterValue.startTime）。模板结构可能变了——不能静默放行，"
            "否则会一直拉抓包那天的旧数据。重新抓一份 "
            "references/olap_query_template.json"
        )
    return body


def query_olap(session: Any, body: dict[str, Any]) -> dict[str, Any]:
    resp = session.request(
        "POST", OLAP_URL, json=body,
        headers={
            "Accept": "application/json",
            "Kwaibi-View-Query-Type": "8",
            "kwaibi-request-module": "olap",
            "Referer": REFERER,
        },
        timeout=180,
    )
    # SSO 失效不总是直接给 401/302：部分网关会跟随跳转并最终返回 200 的
    # 登录 HTML。若只看状态码，后面 json() 抛 JSONDecodeError 会被包装成
    # UNEXPECTED_ERROR，排查者无法知道该刷新 olap.example.corp 的登录态。
    final_url = str(getattr(resp, "url", ""))
    content_type = (resp.headers.get("Content-Type") or "").lower()
    is_login_page = (
        "login" in final_url.lower()
        or "sso" in final_url.lower()
        or "text/html" in content_type
    )
    if resp.status_code in (401, 302) or is_login_page:
        raise PermissionError("SSO_EXPIRED")
    if resp.status_code == 403:
        raise PermissionError("AUTH_FAILED")
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data") or {}
    rows = data.get("dataList") or []
    columns = [c.get("name") for c in data.get("columns") or []]
    # result=1 伴随 error_msg="canDiff" 是正常返回（同期对比提示），有数据即可用；
    # 真正的失败是拿不到任何列或行。
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


AUDIT_DIR = Path(__file__).resolve().parent.parent / "audit"


def _append_audit(result: dict[str, Any], total_rows: int) -> None:
    """每次取数都留一笔痕，方便事后定位「窗口内没数据」这类偶发异常。

    偶发取数异常（OLAP 返回不完整）事后无法复现，只能靠留痕回看当时的
    窗口、账户数和每个账户的数据点，判断是数据源问题还是脚本问题。
    """
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        log = AUDIT_DIR / "fetch_log.jsonl"
        record = {
            "ts": datetime.now(TZ).isoformat(timespec="seconds"),
            "window_start": result["window_start"],
            "window_end": result["window_end"],
            "account_count": len(result["accounts"]),
            "minute_rows_used": result["minute_rows_used"],
            "total_rows_fetched": total_rows,
            "accounts": result["accounts"],
        }
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 留痕失败不能影响取数主流程


def write_csv(path: Path, accounts: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["account_id", "rate", "current_coefficient", "cost", "minutes"]
        )
        for item in accounts:
            rate = "" if item.get("rate") is None else f"{item['rate']:.10f}"
            writer.writerow(
                [item["account_id"], rate, "", item["cost"], item["minutes"]]
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="直连 OLAP 取数并按账户聚合 ROI")
    parser.add_argument("--output", required=True, help="输出 CSV 路径")
    parser.add_argument("--window-minutes", type=int, default=20, help="窗口长度，默认 20")
    parser.add_argument("--lag-minutes", type=int, default=5, help="右端留白，默认 5")
    parser.add_argument("--raw-output", default=None, help="把 OLAP 原始响应也存一份")
    parser.add_argument(
        "--accounts",
        default=None,
        help="可选：逗号分隔的账户ID列表，只处理这些账户。不传则处理全量账户",
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

        from ks_aimate.sso_login_client import SmartSSOSession

        template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        session = SmartSSOSession()
        fetched = query_olap(session, build_body(template, start, end))

        if args.raw_output:
            raw = Path(args.raw_output)
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_text(json.dumps(fetched, ensure_ascii=False), encoding="utf-8")

        result = aggregate(fetched["columns"], fetched["rows"], start, end, account_filter)
        _append_audit(result, len(fetched["rows"]))
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
            "OLAP（olap.example.corp）登录态已失效，请刷新该域名的 SSO 后重试；"
            "adsdata 域名的 cookie 不会用于本脚本"
            if code == "SSO_EXPIRED"
            else "当前账号无 OLAP 看板 / 接口权限"
        )
        return _fail(code, hint, code=2)
    except KeyboardInterrupt:
        return _fail("INTERRUPTED", "用户中断", code=130)
    except Exception as exc:  # noqa: BLE001
        print(f"[fetch_roi] {type(exc).__name__}: {exc}", file=sys.stderr)
        return _fail("UNEXPECTED_ERROR", f"{type(exc).__name__}: {exc}", code=1)


if __name__ == "__main__":
    sys.exit(main())
