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
"""短剧 IAA 第三步：把第二步账户放入实时效果数据集与客户看板，产出客户可看清单。"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from screen_accounts import ScreenError, as_raw, fetch, make_body, normalize, TZ, file_link
from screen_step2 import kconf_antou_ids, make_step2_body, normalize_step2

TPL_123403 = Path(__file__).resolve().parent.parent / "references" / "step3_123403_template.json"
TPL_68012103 = Path(__file__).resolve().parent.parent / "references" / "step3_68012103_template.json"

ACCOUNT_TARGET_123403 = 2076862
ACCOUNT_TARGET_68012103 = 3797590
ROI_COLUMN = "extend_column_78e5bb23-be44-4dcf-a56a-8b270d080729"

FORBIDDEN_GROUPS = ["硕才"]
FORBIDDEN_PRODUCTS = ["青青剧场微短剧", "快乐说漫", "月光阅读"]
FORBIDDEN_CLIENTS = ["番茄", "番茄漫剧", "阿里非电商", "漫森长沙", "漫森二部", "漫森三部", "山河", "美偲", "亿汇", "云栖", "鑫杰光年"]
CLIENT_NOT_CONTAIN = "书读"

EXPECTED_123403 = ["__time", "账户ID", "总消耗", "联盟总消耗",
                   "extend_column_9779d4d1-d60c-4950-8cb6-7d5680053a2f",
                   "超成本率", "平均浅度出价（下发口径）", "(浅度)转化数",
                   "extend_column_a959aa9c-c044-4f76-aca7-b4760e8bbec2",
                   "当日广告变现ROI"]
EXPECTED_68012103 = ["集团", "运营定义客户名", "产品名称", "账户ID", "总消耗", ROI_COLUMN, "超成本率"]


def response(ok: bool, code: int, started: float, **parts: Any) -> int:
    timeout = parts.get("error") == "API_TIMEOUT"
    action = "CONTINUE" if ok else "RETRY_ONCE_THEN_STOP" if timeout else "STOP"
    print(json.dumps({"ok": ok, "code": code,
                      "duration_ms": int((time.monotonic() - started) * 1000),
                      **parts,
                      "next_action": {"type": action, "retryable": timeout,
                                       "max_retries": 1 if timeout else 0}},
                     ensure_ascii=False))
    return code


def load_template(path: Path, data_set_id: int) -> dict[str, Any]:
    if not path.is_file():
        raise ScreenError("TEMPLATE_MISSING", f"请求模板缺失：{path.name}")
    body = json.loads(path.read_text(encoding="utf-8"))
    if body.get("dataSetId") != data_set_id or body.get("rowLimit") != 100000:
        raise ScreenError("TEMPLATE_INVALID", f"{path.name} 数据集或行数上限变化，请重新对齐看板")
    return body


def read_accounts_file(path: str) -> list[str]:
    accounts_path = Path(path)
    if not accounts_path.is_file():
        raise ScreenError("FILE_NOT_FOUND", f"账户清单不存在：{path}")
    try:
        with accounts_path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if "账户ID" not in (reader.fieldnames or []):
                raise ScreenError("VALIDATION_ERROR", f"{path} 缺少「账户ID」列")
            accounts = [row["账户ID"] for row in reader]
    except UnicodeDecodeError as exc:
        raise ScreenError("VALIDATION_ERROR", f"{path} 不是有效的 UTF-8 CSV") from exc
    if not accounts or any(not re.fullmatch(r"[0-9]+", account) for account in accounts):
        raise ScreenError("VALIDATION_ERROR", f"{path} 账户ID为空或格式非法")
    return accounts


def step2_accounts(day: datetime) -> tuple[list[str], dict[str, Any]]:
    kconf_ids, kconf_meta = kconf_antou_ids()
    step1_pairs, step1_summary = normalize(fetch(make_body(day, None)), None)
    step1_ids = {account for _label, account, _cost in step1_pairs}
    excluded = step1_ids | kconf_ids
    if not excluded:
        raise ScreenError("EXCLUSION_EMPTY", "第一步与 KConf 排除集合均为空，中止以保护口径")
    rows, details, columns = normalize_step2(fetch(make_step2_body(day, excluded)), excluded)
    account_index = columns.index("账户ID")
    accounts = sorted({str(as_raw(row[account_index])) for row in rows}, key=int)
    return accounts, {"excluded_step1_accounts": len(step1_ids),
                      "excluded_kconf_accounts": len(kconf_ids),
                      "excluded_union_accounts": len(excluded),
                      "step1_row_count": step1_summary["row_count"],
                      "step2_row_count": details["row_count"],
                      "step2_total_cost": details["total_cost"],
                      **kconf_meta}


def rewrite_common(body: dict[str, Any], day: datetime, accounts: list[str], target_id: int) -> None:
    begin = int(day.timestamp() * 1000)
    end = int((day + timedelta(days=1)).timestamp() * 1000) - 1
    datetimes = [f for f in body["filters"] if f.get("filterType") == "datetime"]
    account_filters = [f for f in body["filters"] if f.get("name") == "账户ID"]
    if len(datetimes) != 1 or len(account_filters) != 1:
        raise ScreenError("TEMPLATE_INVALID", "时间或账户ID过滤结构变化，请重新对齐看板")
    datetimes[0]["filterValue"].update(startTime=begin, endTime=end)
    if isinstance(datetimes[0].get("originalFilterValue"), dict):
        datetimes[0]["originalFilterValue"]["value"] = [begin, end]
    account_filter = account_filters[0]
    if account_filter.get("targetId") != target_id:
        raise ScreenError("TEMPLATE_INVALID", "账户ID字段目标变化，请重新对齐看板")
    account_filter["filterValue"]["value"] = list(accounts)
    if isinstance(account_filter.get("originalFilterValue"), dict):
        account_filter["originalFilterValue"]["value"] = list(accounts)
    body["useCache"] = False
    body["openQueryId"] = str(uuid.uuid4())
    if "queryTime" in body:
        body["queryTime"] = int(datetime.now(TZ).timestamp() * 1000)


def check_123403_invariants(body: dict[str, Any]) -> None:
    def find(name):
        return [f for f in body.get("filters", []) if f.get("name") == name]
    goals = find("转化目标（原优化目标）")
    industries = find("客户一级行业")
    if len(goals) != 1 or goals[0]["filterValue"] != {"condition": "in", "value": ["首日变现ROI（短剧IAA专用）"], "compareType": ""}:
        raise ScreenError("TEMPLATE_INVALID", "转化目标筛选变化，请重新对齐看板")
    if len(industries) != 1 or industries[0]["filterValue"] != {"condition": "in", "value": ["短剧小说"], "compareType": ""}:
        raise ScreenError("TEMPLATE_INVALID", "客户一级行业筛选变化，请重新对齐看板")
    pairs = [(f.get("name"), f.get("filterValue")) for f in body.get("resultFilters", [])]
    if pairs != [("总消耗", {"condition": "greater", "value": [250]})]:
        raise ScreenError("TEMPLATE_INVALID", "结果过滤条件变化，请重新对齐看板")
    output_fields = {f.get("name") for f in body.get("fields", [])}
    if not set(EXPECTED_123403).issubset(output_fields):
        raise ScreenError("TEMPLATE_INVALID", "输出列变化，请重新对齐看板")


def check_68012103_invariants(body: dict[str, Any]) -> None:
    def find(name):
        return [f for f in body.get("filters", []) if f.get("name") == name]
    goals = find("转化目标（原优化目标）")
    markets = find("营销目标")
    ranges = find("extend_column_0375ed68-bf39-4671-b511-9e13f883d289")
    groups = find("集团")
    products = find("产品名称")
    clients = find("运营定义客户名")
    if len(goals) != 1 or goals[0]["filterValue"] != {"condition": "in", "value": ["首日变现ROI（短剧IAA专用）"], "compareType": ""}:
        raise ScreenError("TEMPLATE_INVALID", "转化目标筛选变化，请重新对齐看板")
    if len(markets) != 1 or markets[0]["filterValue"] != {"condition": "in", "value": ["快手号推广-短剧推广"], "compareType": ""}:
        raise ScreenError("TEMPLATE_INVALID", "营销目标筛选变化，请重新对齐看板")
    if len(ranges) != 1 or ranges[0]["filterValue"] != {"condition": "in", "value": ["其他版权方", "版权方为空"], "compareType": ""}:
        raise ScreenError("TEMPLATE_INVALID", "可投范围筛选变化，请重新对齐看板")
    if len(groups) != 1 or groups[0]["filterValue"] != {"condition": "not-in", "value": ["硕才"], "compareType": ""}:
        raise ScreenError("TEMPLATE_INVALID", "集团筛选变化，请重新对齐看板")
    product_conds = sorted(json.dumps(f["filterValue"], ensure_ascii=False, sort_keys=True) for f in products)
    expected_products = sorted(json.dumps(v, ensure_ascii=False, sort_keys=True) for v in (
        {"condition": "not-in", "value": ["青青剧场微短剧", "快乐说漫"], "compareType": ""},
        {"condition": "not-in", "value": ["月光阅读"], "compareType": ""},
    ))
    if product_conds != expected_products:
        raise ScreenError("TEMPLATE_INVALID", "产品名称筛选变化，请重新对齐看板")
    client_conds = sorted(json.dumps(f["filterValue"], ensure_ascii=False, sort_keys=True) for f in clients)
    expected_clients = sorted(json.dumps(v, ensure_ascii=False, sort_keys=True) for v in (
        {"condition": "not-in", "value": ["番茄", "番茄漫剧", "漫森长沙", "漫森二部", "漫森三部", "山河", "美偲", "亿汇", "云栖", "鑫杰光年"], "compareType": ""},
        {"condition": "not-in", "value": ["阿里非电商"], "compareType": ""},
        {"condition": "not-contain", "value": ["书读"], "compareType": ""},
    ))
    if client_conds != expected_clients:
        raise ScreenError("TEMPLATE_INVALID", "运营定义客户名筛选变化，请重新对齐看板")
    pairs = [(f.get("name"), f.get("filterValue")) for f in body.get("resultFilters", [])]
    for name, value in (("超成本率", {"condition": "less", "value": [0.15]}),
                        ("变现ROI", {"condition": "greater", "value": [0.85]}),
                        ("总消耗", {"condition": "less", "value": [20000]}),
                        ("总消耗", {"condition": "greater", "value": [2000]})):
        if pairs.count((name, value)) != 1:
            raise ScreenError("TEMPLATE_INVALID", "结果过滤条件变化，请重新对齐看板")
    output_fields = {f.get("name") for f in body.get("fields", [])}
    if not set(EXPECTED_68012103).issubset(output_fields):
        raise ScreenError("TEMPLATE_INVALID", "输出列变化，请重新对齐看板")


def normalize_123403(data: dict[str, Any], account_set: set[str], day: datetime) -> tuple[list[list[Any]], list[str], dict[str, Any]]:
    columns = [c.get("name") for c in (data.get("columns") or [])]
    missing = [name for name in EXPECTED_123403 if name not in columns]
    if missing:
        raise ScreenError("COLUMNS_CHANGED", f"123403 返回列变化，缺少：{missing}")
    rows = data.get("dataList")
    if not isinstance(rows, list) or not isinstance(data.get("rowCount"), int):
        raise ScreenError("QUERY_FAILED", "123403 返回缺少明细或总行数")
    if data.get("truncated") or len(rows) >= 100000 or len(rows) != data["rowCount"]:
        raise ScreenError("RESULT_TRUNCATED", f"123403 返回 {len(rows)} 行，总行数 {data['rowCount']}")
    idx = {name: columns.index(name) for name in EXPECTED_123403}
    begin = int(day.timestamp() * 1000)
    end = int((day + timedelta(days=1)).timestamp() * 1000) - 1
    seen: set[tuple[str, str]] = set()
    for row in rows:
        account = str(as_raw(row[idx["账户ID"]]))
        time_ms = as_raw(row[idx["__time"]])
        cost = as_raw(row[idx["总消耗"]])
        if (account not in account_set or not re.fullmatch(r"[0-9]+", account)
                or not isinstance(time_ms, (int, float)) or not begin <= time_ms <= end
                or not isinstance(cost, (int, float)) or not math.isfinite(float(cost))
                or not float(cost) > 250):
            raise ScreenError("INVALID_ROW", "123403 返回行不满足筛选条件")
        key = (account, str(time_ms))
        if key in seen:
            raise ScreenError("INVALID_ROW", "123403 返回重复的账户×时间行")
        seen.add(key)
    accounts = sorted({str(as_raw(r[idx["账户ID"]])) for r in rows}, key=int)
    total_cost = round(sum(float(as_raw(r[idx["总消耗"]])) for r in rows), 2)
    return rows, accounts, {"rows": len(rows), "covered_accounts": len(accounts),
                            "total_cost": total_cost}


def normalize_68012103(data: dict[str, Any], account_set: set[str]) -> tuple[list[list[Any]], list[str], dict[str, Any]]:
    columns = [c.get("name") for c in (data.get("columns") or [])]
    missing = [name for name in EXPECTED_68012103 if name not in columns]
    if missing:
        raise ScreenError("COLUMNS_CHANGED", f"68012103 返回列变化，缺少：{missing}")
    rows = data.get("dataList")
    if not isinstance(rows, list) or not isinstance(data.get("rowCount"), int):
        raise ScreenError("QUERY_FAILED", "68012103 返回缺少明细或总行数")
    if data.get("truncated") or len(rows) >= 100000 or len(rows) != data["rowCount"]:
        raise ScreenError("RESULT_TRUNCATED", f"68012103 返回 {len(rows)} 行，总行数 {data['rowCount']}")
    idx = {name: columns.index(name) for name in EXPECTED_68012103}
    for row in rows:
        group = str(as_raw(row[idx["集团"]]))
        client = str(as_raw(row[idx["运营定义客户名"]]))
        product = str(as_raw(row[idx["产品名称"]]))
        account = str(as_raw(row[idx["账户ID"]]))
        cost = as_raw(row[idx["总消耗"]])
        roi = as_raw(row[idx[ROI_COLUMN]])
        over = as_raw(row[idx["超成本率"]])
        if (group in FORBIDDEN_GROUPS or product in FORBIDDEN_PRODUCTS
                or client in FORBIDDEN_CLIENTS or CLIENT_NOT_CONTAIN in client
                or account not in account_set or not re.fullmatch(r"[0-9]+", account)
                or not all(isinstance(v, (int, float)) and math.isfinite(float(v))
                           for v in (cost, roi, over))
                or not 2000 < float(cost) < 20000 or not float(roi) > 0.85
                or not float(over) < 0.15):
            raise ScreenError("INVALID_ROW", "68012103 返回行不满足筛选条件")
    accounts = sorted({str(as_raw(r[idx["账户ID"]])) for r in rows}, key=int)
    total_cost = round(sum(float(as_raw(r[idx["总消耗"]])) for r in rows), 2)
    return rows, accounts, {"rows": len(rows), "covered_accounts": len(accounts),
                            "total_cost": total_cost}


def save_csv(path: Path, columns: list[str], rows: list[list[Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(row)


def delivery_summary(date: str, started: float, accounts: list[str], step2_meta: dict[str, Any],
                     summary_123403: dict[str, Any], missing_123403: list[str],
                     summary_68012103: dict[str, Any], missing_68012103: list[str],
                     out_123403: Path, out_dashboard: Path) -> str:
    """固定交付样式，Agent 直接把本块文本转述给用户。"""
    lines = [f"完整链路跑通（{date} 口径），耗时 {int(time.monotonic() - started)} 秒："]
    if "step1_row_count" in step2_meta:
        kconf_ver = step2_meta.get("kconf_version") or "?"
        snapshot = step2_meta.get("kconf_snapshot_id") or "?"
        lines.append(
            f"第一步 {step2_meta['step1_row_count']:,} 行 → 排除账户"
            f"（第一步 {step2_meta['excluded_step1_accounts']:,} + KConf 暗投白名单 v{kconf_ver} / snapshot {snapshot}，"
            f"共 {step2_meta['excluded_union_accounts']:,} 个）")
        lines.append(f"第二步 {step2_meta['step2_row_count']:,} 行 / {len(accounts):,} 个去重账户，"
                     f"总消耗 {step2_meta['step2_total_cost']:,.2f} 元")
    else:
        lines.append(f"第二步账户来源：{step2_meta.get('accounts_source', '外部文件')}（未重跑，无第一步/第二步统计）")
    lines.append(f"第三步 {len(accounts):,} 个账户注入两个查询：")
    lines.append(f"123403 实时数据：{summary_123403['rows']:,} 行 / {summary_123403['covered_accounts']:,} 账户，"
                 f"总消耗 {summary_123403['total_cost']:,.2f} 元"
                 f"（缺 {len(missing_123403)} 个账户无数据）")
    lines.append(f"68012103 客户看板：{summary_68012103['rows']:,} 行 / {summary_68012103['covered_accounts']:,} 账户，"
                 f"总消耗 {summary_68012103['total_cost']:,.2f} 元"
                 f"（缺 {len(missing_68012103)} 个账户无数据，多为非短剧推广营销目标）")
    lines.append("")
    lines.append("交付文件：")
    lines.append(f"客户看板清单：{file_link(out_dashboard)}")
    lines.append(f"123403 实时明细：{file_link(out_123403)}")
    return "\n".join(lines)


def print_delivery(text: str) -> None:
    print("===DELIVERY_SUMMARY===")
    print(text, end="\n")
    print("===END_DELIVERY_SUMMARY===")


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ScreenError("INVALID_ARGUMENT", message)


def main() -> int:
    started = time.monotonic()
    try:
        parser = Parser(description="短剧 IAA 第三步：第二步账户 → 123403 实时数据 + 68012103 客户看板")
        parser.add_argument("--date", help="北京时间自然日，格式 YYYY-MM-DD；默认昨日")
        parser.add_argument("--accounts-file", help="第二步账户CSV（含账户ID列）；缺省时内部重跑第二步")
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

        if args.accounts_file:
            accounts = read_accounts_file(args.accounts_file)
            step2_meta: dict[str, Any] = {"accounts_source": args.accounts_file}
        else:
            accounts, step2_meta = step2_accounts(day)
            step2_meta["accounts_source"] = "step2_recomputed"
        account_set = set(accounts)

        body_123403 = load_template(TPL_123403, 123403)
        check_123403_invariants(body_123403)
        rewrite_common(body_123403, day, accounts, ACCOUNT_TARGET_123403)
        data_123403 = fetch(body_123403)
        rows_123403, covered_123403, summary_123403 = normalize_123403(data_123403, account_set, day)

        body_68012103 = load_template(TPL_68012103, 205802)
        check_68012103_invariants(body_68012103)
        rewrite_common(body_68012103, day, accounts, ACCOUNT_TARGET_68012103)
        data_68012103 = fetch(body_68012103)
        rows_68012103, covered_68012103, summary_68012103 = normalize_68012103(data_68012103, account_set)

        out = Path("tmp")
        out.mkdir(parents=True, exist_ok=True)
        prefix = f"duanju_iaa_{date:%Y%m%d}_step3"
        out_123403 = out / f"{prefix}_123403.csv"
        out_dashboard = out / f"{prefix}_client_dashboard.csv"

        cols_123403 = [c.get("nickName") or c.get("name") for c in (data_123403.get("columns") or [])]
        cols_123403 = ["时间" if name == "__time" else name for name in cols_123403]
        seen_names: dict[str, int] = {}
        final_cols_123403: list[str] = []
        for name in cols_123403:
            count = seen_names.get(name, 0) + 1
            seen_names[name] = count
            final_cols_123403.append(name if count == 1 else f"{name}_{count}")
        raw_123403 = []
        col_names_123403 = [c.get("name") for c in (data_123403.get("columns") or [])]
        idx_time = col_names_123403.index("__time")
        for row in rows_123403:
            values = [as_raw(cell) for cell in row]
            time_cell = row[idx_time]
            values[idx_time] = time_cell.get("v") if isinstance(time_cell, dict) else time_cell
            raw_123403.append(values)
        save_csv(out_123403, final_cols_123403, raw_123403)

        cols_68012103 = [c.get("nickName") or c.get("name") for c in (data_68012103.get("columns") or [])]
        cols_68012103 = ["变现ROI-iaa" if name == ROI_COLUMN else name for name in cols_68012103]
        raw_68012103 = [[as_raw(cell) for cell in row] for row in rows_68012103]
        save_csv(out_dashboard, cols_68012103, raw_68012103)

        missing_123403 = sorted(set(accounts) - set(covered_123403), key=int)
        missing_68012103 = sorted(set(accounts) - set(covered_68012103), key=int)
        delivery = delivery_summary(date.isoformat(), started, accounts, step2_meta,
                                    summary_123403, missing_123403,
                                    summary_68012103, missing_68012103,
                                    out_123403, out_dashboard)
        print_delivery(delivery)
        return response(True, 0, started, data={
            "date": date.isoformat(),
            "accounts_total": len(accounts),
            "dataset_123403": {"rows": summary_123403["rows"],
                               "covered_accounts": summary_123403["covered_accounts"],
                               "total_cost": summary_123403["total_cost"],
                               "missing_accounts": missing_123403,
                               "file": str(out_123403)},
            "dashboard_68012103": {"rows": summary_68012103["rows"],
                                   "covered_accounts": summary_68012103["covered_accounts"],
                                   "total_cost": summary_68012103["total_cost"],
                                   "missing_accounts": missing_68012103,
                                   "file": str(out_dashboard)},
            **step2_meta,
            "delivery": {"客户看板清单": file_link(out_dashboard), "123403实时明细": file_link(out_123403)},
        }, msg=f"第三步：{len(accounts)} 个账户 → 123403 覆盖 {summary_123403['covered_accounts']} 个，"
               f"客户看板覆盖 {summary_68012103['covered_accounts']} 个")
    except KeyboardInterrupt:
        return response(False, 130, started, error="INTERRUPTED", msg="用户中断")
    except ScreenError as exc:
        print(f"[screen_step3] {exc.error}: {exc}", file=sys.stderr)
        return response(False, exc.code, started, error=exc.error, msg=str(exc))
    except Exception as exc:
        print(f"[screen_step3] {type(exc).__name__}: {exc}", file=sys.stderr)
        return response(False, 1, started, error="UNEXPECTED_ERROR", msg=f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
