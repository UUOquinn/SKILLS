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
"""直连 OLAP 筛选上一完整自然日的短剧 IAA 账户。"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from auth_helper.sso_login_client import SmartAuthSession
import requests

URL = "https://app.example.corp/api/v1/data/query"
REFERER = ("https://bi.example.corp/pc/dashboard/preview?"
           "dashboardId=2060503&insight-chart=7288287&insightConfigId=66613605"
           "&sheetId=140171&tabIds=864253")
TEMPLATE = Path(__file__).resolve().parent.parent / "references" / "olap_query_template.json"
TZ = timezone(timedelta(hours=8))
RANGE_COLUMN = "extend_column_0375ed68-bf39-4671-b511-9e13f883d289"
RANGES = {"七猫": "qimao", "爱奇艺": "iqiyi", "番茄": "fanqie"}
COLUMNS = {RANGE_COLUMN, "账户ID", "总消耗"}


class ScreenError(Exception):
    def __init__(self, error: str, message: str, code: int = 1):
        super().__init__(message)
        self.error = error
        self.code = code


def file_link(path: Any) -> str:
    """绝对路径 → <FILE>工作区相对路径</FILE> 下载链接，交付时照抄。"""
    s = str(path)
    marker = "/workspace/"
    if marker in s:
        rel = s.split(marker, 1)[1]
    elif s.startswith("tmp/"):
        rel = s
    else:
        rel = Path(s).name
    return f"<FILE>{rel}</FILE>"


def response(ok: bool, code: int, started: float, **parts: Any) -> int:
    timeout = parts.get("error") == "API_TIMEOUT"
    action = "CONTINUE" if ok else "RETRY_ONCE_THEN_STOP" if timeout else "STOP"
    print(json.dumps({"ok": ok, "code": code, "duration_ms": int((time.monotonic() - started) * 1000),
                      **parts, "next_action": {"type": action, "retryable": timeout,
                                                "max_retries": 1 if timeout else 0}},
                     ensure_ascii=False))
    return code


def as_raw(cell: Any) -> Any:
    return cell.get("r", cell.get("v")) if isinstance(cell, dict) else cell


def make_body(date: datetime, item: str | None) -> dict[str, Any]:
    if not TEMPLATE.is_file():
        raise ScreenError("TEMPLATE_MISSING", "请求模板缺失，请恢复 references/olap_query_template.json")
    body = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    if body.get("dataSetId") != 205802 or body.get("rowLimit") != 100000:
        raise ScreenError("TEMPLATE_INVALID", "模板数据集或行数上限变化，请重新对齐看板")
    begin = int(date.timestamp() * 1000)
    end = int((date + timedelta(days=1)).timestamp() * 1000) - 1
    date_filters = [f for f in body["filters"] if f.get("name") == "__time" and f.get("filterType") == "datetime"]
    ranges = [f for f in body["filters"] if f.get("name") == "可投范围"]
    goals = [f for f in body["filters"] if f.get("name") == "转化目标（原优化目标）"]
    marketing = [f for f in body["filters"] if f.get("name") == "营销目标"]
    costs = [f for f in body["resultFilters"] if f.get("name") == "总消耗"]
    if len(date_filters) != 1 or len(ranges) != 1 or len(goals) != 1 or len(marketing) != 1 or len(costs) != 1:
        raise ScreenError("TEMPLATE_INVALID", "模板时间或业务筛选结构变化，请重新对齐看板")
    date_filter = date_filters[0]
    date_filter["filterValue"].update(startTime=begin, endTime=end)
    date_filter["originalFilterValue"]["value"] = [begin, end]
    value = ranges[0]["filterValue"]
    if set(value["value"]) != set(RANGES) or value["condition"] != "in":
        raise ScreenError("TEMPLATE_INVALID", "可投范围筛选变化，请重新对齐看板")
    ranges[0]["filterValue"]["value"] = [item] if item else list(value["value"])
    if goals[0]["filterValue"]["value"] != ["首日变现ROI（短剧IAA专用）"] or marketing[0]["filterValue"]["value"] != ["快手号推广-短剧推广"]:
        raise ScreenError("TEMPLATE_INVALID", "转化/营销目标筛选变化，请重新对齐看板")
    if costs[0]["filterValue"] != {"condition": "greater", "value": [0.1]}:
        raise ScreenError("TEMPLATE_INVALID", "消耗门槛变化，请重新对齐看板")
    body["useCache"] = False
    body["openQueryId"] = str(uuid.uuid4())
    return body


def fetch(body: dict[str, Any]) -> dict[str, Any]:
    try:
        resp = SmartAuthSession().request("POST", URL, json=body, headers={
            "Accept": "application/json", "Kwaibi-View-Query-Type": "8",
            "bi-request-module": "olap", "Referer": REFERER,
        }, timeout=180)
    except requests.exceptions.Timeout as exc:
        raise ScreenError("API_TIMEOUT", "OLAP 请求超时，确认网络后仅重试一次") from exc
    except requests.exceptions.RequestException as exc:
        raise ScreenError("NETWORK_ERROR", f"OLAP 网络错误：{exc}") from exc
    if resp.status_code in (401, 302) or "text/html" in resp.headers.get("Content-Type", "").lower():
        raise ScreenError("AUTH_EXPIRED", "OLAP 域名登录态失效，需先恢复该域名认证", 2)
    if resp.status_code == 403:
        raise ScreenError("AUTH_FAILED", "当前账号无数据集 205802 的 OLAP 查询权限", 2)
    if 400 <= resp.status_code < 500:
        raise ScreenError("API_4XX", f"OLAP 返回 HTTP {resp.status_code}：{resp.text[:300]}")
    if resp.status_code >= 500:
        raise ScreenError("API_5XX", f"OLAP 返回 HTTP {resp.status_code}")
    payload = resp.json()
    if payload.get("result") != 1 or not isinstance(payload.get("data"), dict):
        raise ScreenError("QUERY_FAILED", f"OLAP 查询失败：{str(payload.get('error_msg'))[:250]}")
    return payload["data"]


def normalize(data: dict[str, Any], item: str | None) -> tuple[list[tuple[str, str, float]], dict[str, Any]]:
    columns = [c.get("name") for c in (data.get("columns") or [])]
    if not COLUMNS.issubset(columns):
        raise ScreenError("COLUMNS_CHANGED", f"OLAP 返回列变化：{columns}")
    rows = data.get("dataList")
    if not isinstance(rows, list) or not isinstance(data.get("rowCount"), int):
        raise ScreenError("QUERY_FAILED", "OLAP 返回缺少明细或总行数")
    if data.get("truncated") or len(rows) >= 100000 or len(rows) != data["rowCount"]:
        raise ScreenError("RESULT_TRUNCATED", f"返回 {len(rows)} 行，总行数 {data['rowCount']}，可能被截断，不能交付不完整名单")
    indexes = {col: columns.index(col) for col in COLUMNS}
    result: list[tuple[str, str, float]] = []
    counts: Counter[str] = Counter()
    ids: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for row in rows:
        label = str(as_raw(row[indexes[RANGE_COLUMN]]))
        account = str(as_raw(row[indexes["账户ID"]]))
        try:
            cost = float(as_raw(row[indexes["总消耗"]]))
        except (TypeError, ValueError) as exc:
            raise ScreenError("INVALID_ROW", "OLAP 返回无效的原始消耗值") from exc
        if (label not in RANGES or (item and label != item) or not re.fullmatch(r"[0-9]+", account)
                or not math.isfinite(cost) or cost <= 0.1 or (account, label) in seen):
            raise ScreenError("INVALID_ROW", "返回的账户、可投范围或消耗不满足固定筛选条件")
        result.append((label, account, cost))
        counts[label] += 1
        ids.add(account)
        seen.add((account, label))
    if not result:
        raise ScreenError("EMPTY_RESULT", "OLAP 查询未返回命中账户，请核对所选日期、数据回补和看板状态")
    details = {"row_count": len(result), "account_count": len(ids),
               "range_counts": {label: counts[label] for label in RANGES},
               "total_cost": round(sum(r[2] for r in result), 2)}
    return result, details


def save(date: datetime, item: str | None, rows: list[tuple[str, str, float]]) -> tuple[Path, Path]:
    suffix = f"_{RANGES[item]}" if item else ""
    prefix = f"duanju_iaa_{date:%Y%m%d}{suffix}"
    out = Path("tmp")
    out.mkdir(parents=True, exist_ok=True)
    details_path = out / f"{prefix}_details.csv"
    accounts_path = out / f"{prefix}_accounts.csv"
    with details_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["可投范围", "账户ID", "总消耗_元"])
        writer.writerows(rows)
    with accounts_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["账户ID"])
        writer.writerows(([account] for account in sorted({r[1] for r in rows}, key=int)))
    return accounts_path, details_path


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ScreenError("INVALID_ARGUMENT", message)


def main() -> int:
    started = time.monotonic()
    try:
        parser = Parser(description="短剧 IAA 第一步：上一完整自然日账户筛选")
        parser.add_argument("--date", help="北京时间自然日，格式 YYYY-MM-DD；默认昨日")
        parser.add_argument("--item", choices=RANGES, help="单独补跑一个可投范围")
        args = parser.parse_args()
        today = datetime.now(TZ).date()
        if args.date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
            raise ScreenError("INVALID_DATE", "只能查询已完成的北京自然日，格式 YYYY-MM-DD")
        try:
            date = datetime.fromisoformat(args.date).date() if args.date else today - timedelta(days=1)
        except ValueError as exc:
            raise ScreenError("INVALID_DATE", "无效的北京自然日，格式 YYYY-MM-DD") from exc
        if date >= today:
            raise ScreenError("INVALID_DATE", "只能查询已完成的北京自然日，格式 YYYY-MM-DD")
        day = datetime(date.year, date.month, date.day, tzinfo=TZ)
        data = fetch(make_body(day, args.item))
        rows, summary = normalize(data, args.item)
        accounts_path, details_path = save(day, args.item, rows)
        return response(True, 0, started, data={
            "date": date.isoformat(), "window": [day.isoformat(), (day + timedelta(days=1)).isoformat()],
            **summary, "accounts_file": str(accounts_path), "details_file": str(details_path),
            "delivery": {"账户清单": file_link(accounts_path), "明细": file_link(details_path)},
            "endpoint": URL,
        }, msg=f"筛出 {summary['account_count']} 个去重账户，{summary['row_count']} 条范围×账户记录")
    except KeyboardInterrupt:
        return response(False, 130, started, error="INTERRUPTED", msg="用户中断")
    except ScreenError as exc:
        print(f"[screen_accounts] {exc.error}: {exc}", file=sys.stderr)
        return response(False, exc.code, started, error=exc.error, msg=str(exc))
    except Exception as exc:
        print(f"[screen_accounts] {type(exc).__name__}: {exc}", file=sys.stderr)
        return response(False, 1, started, error="UNEXPECTED_ERROR", msg=f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
