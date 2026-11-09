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
"""短剧 IAA 第二步：排除第一步账户与暗投白名单后，筛选客户维度数据账户。"""

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

import requests
from ks_aimate.sso_login_client import SmartSSOSession, SSOAuthRequiredError

from screen_accounts import ScreenError, as_raw, fetch, normalize, make_body, TZ, file_link

TEMPLATE2 = Path(__file__).resolve().parent.parent / "references" / "step2_query_template.json"
KCONF_BASE = "https://kconf.example.corp"
KCONF_KEY = "ad.adtarget3.universeAddChainGrayConfig"
AB_KEY = "enable_chain_antou_30_990_0"
ROI_COLUMN = "extend_column_78e5bb23-be44-4dcf-a56a-8b270d080729"
EXPECTED_COLUMNS = ["集团", "运营定义客户名", "产品名称", "账户ID", "总消耗", "超成本率",
                    "ROI出价系数_浅度", ROI_COLUMN]

FORBIDDEN_GROUPS = ["硕才"]
FORBIDDEN_PRODUCTS = ["青青剧场微短剧", "快乐说漫", "月光阅读"]
FORBIDDEN_CLIENTS = ["番茄", "番茄漫剧", "阿里非电商", "漫森长沙", "漫森二部", "漫森三部", "山河", "美偲", "亿汇", "云栖", "鑫杰光年"]
CLIENT_NOT_CONTAIN = "书读"


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


def kconf_antou_ids() -> tuple[set[str], dict[str, Any]]:
    """读取暗投配置中 enable_chain_antou_30_990_0 的账户白名单。"""
    try:
        resp = SmartSSOSession().request(
            "GET", f"{KCONF_BASE}/api/w/config/get", params={"key": KCONF_KEY}, timeout=30)
    except SSOAuthRequiredError as exc:
        raise ScreenError("KCONF_SSO_EXPIRED", "KConf 登录态失效，请先恢复该域认证", 2) from exc
    except requests.Timeout as exc:
        raise ScreenError("API_TIMEOUT", "KConf 请求超时") from exc
    except requests.RequestException as exc:
        raise ScreenError("NETWORK_ERROR", f"无法连接 KConf：{exc}") from exc
    if resp.status_code in (401, 403):
        raise ScreenError("KCONF_SSO_EXPIRED", "KConf 登录态失效或无权限，请先恢复该域认证", 2)
    payload = resp.json()
    if payload.get("result") == 101:
        raise ScreenError("KCONF_SSO_EXPIRED", "KConf 登录态失效，请先恢复该域认证", 2)
    if payload.get("result") != 1:
        raise ScreenError("KCONF_QUERY_FAILED", f"KConf 读取失败：{str(payload.get('message'))[:200]}")
    data = payload.get("data") or {}
    sub = next((item for item in data.get("subConfigs") or [] if item.get("stage") == "production"), None)
    if sub is None:
        raise ScreenError("KCONF_QUERY_FAILED", "KConf 无 production 子配置")
    raw = (sub.get("curConfig") or {}).get("data", "")
    if not isinstance(raw, str) or not raw.strip():
        raise ScreenError("KCONF_QUERY_FAILED", "KConf 生产配置内容为空")
    content = parse_commented_json(raw)
    configs = content.get("configs")
    if not isinstance(configs, list):
        raise ScreenError("KCONF_PARSE_ERROR", "KConf 配置缺少 configs 数组")
    hit = next((item for item in configs if item.get("ab_key") == AB_KEY), None)
    if hit is None:
        raise ScreenError("KCONF_AB_KEY_MISSING", f"配置中未找到 ab_key={AB_KEY}")
    ids = {str(value) for value in (hit.get("account_white_list") or [])}
    meta = data.get("meta") or {}
    return ids, {"kconf_version": meta.get("version"),
                 "kconf_snapshot_id": (sub.get("curConfig") or {}).get("snapshotId"),
                 "kconf_step_cur": sub.get("stepCur")}


def parse_commented_json(raw: str) -> dict[str, Any]:
    lines = []
    for line in raw.splitlines():
        in_str = escaped = False
        for i, ch in enumerate(line):
            if escaped:
                escaped = False
                continue
            if ch == "\\" and in_str:
                escaped = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if not in_str and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                line = line[:i]
                break
        lines.append(line)
    try:
        return json.loads("\n".join(lines))
    except json.JSONDecodeError as exc:
        raise ScreenError("KCONF_PARSE_ERROR", f"KConf 配置 JSON 解析失败：{exc}") from exc


def make_step2_body(day: datetime, excluded: set[str]) -> dict[str, Any]:
    if not TEMPLATE2.is_file():
        raise ScreenError("TEMPLATE_MISSING", "第二步请求模板缺失，请恢复 references/step2_query_template.json")
    body = json.loads(TEMPLATE2.read_text(encoding="utf-8"))
    if body.get("dataSetId") != 205802 or body.get("rowLimit") != 100000:
        raise ScreenError("TEMPLATE_INVALID", "第二步模板数据集或行数上限变化，请重新对齐看板")
    check_invariants(body)
    begin = int(day.timestamp() * 1000)
    end = int((day + timedelta(days=1)).timestamp() * 1000) - 1
    datetimes = [f for f in body["filters"] if f.get("filterType") == "datetime"]
    if len(datetimes) != 1:
        raise ScreenError("TEMPLATE_INVALID", "第二步模板时间过滤结构变化，请重新对齐看板")
    datetimes[0]["filterValue"].update(startTime=begin, endTime=end)
    datetimes[0]["originalFilterValue"]["value"] = [begin, end]
    body["filters"].append({
        "filterType": "string", "targetId": 3797590, "urn": "ks:dimension:ad_v2/account_id",
        "name": "账户ID", "order": 7, "nickName": "账户ID", "type": "dimension",
        "valueType": "string",
        "originalFilterValue": {"condition": "not-in", "value": sorted(excluded)},
        "filterValue": {"condition": "not-in", "value": sorted(excluded), "compareType": ""},
        "uuid": str(uuid.uuid4()), "disabled": False, "disabledTip": "",
    })
    body["useCache"] = False
    body["openQueryId"] = str(uuid.uuid4())
    body["queryTime"] = int(datetime.now(TZ).timestamp() * 1000)
    return body


def check_invariants(body: dict[str, Any]) -> None:
    def find_filter(pred):
        return [f for f in body.get("filters", []) if pred(f)]

    goals = find_filter(lambda f: f.get("name") == "转化目标（原优化目标）")
    ranges = find_filter(lambda f: f.get("name") == "extend_column_0375ed68-bf39-4671-b511-9e13f883d289")
    products = find_filter(lambda f: f.get("name") == "产品名称")
    clients = find_filter(lambda f: f.get("name") == "运营定义客户名")
    groups = find_filter(lambda f: f.get("name") == "集团")
    if len(goals) != 1 or goals[0]["filterValue"] != {"condition": "in", "value": ["首日变现ROI（短剧IAA专用）"], "compareType": ""}:
        raise ScreenError("TEMPLATE_INVALID", "转化目标筛选变化，请重新对齐看板")
    if len(ranges) != 1 or ranges[0]["filterValue"] != {"condition": "in", "value": ["其他版权方", "版权方为空"], "compareType": ""}:
        raise ScreenError("TEMPLATE_INVALID", "可投范围筛选变化，请重新对齐看板")
    if len(products) != 1 or products[0]["filterValue"] != {"condition": "not-in", "value": ["青青剧场微短剧", "快乐说漫", "月光阅读"], "compareType": ""}:
        raise ScreenError("TEMPLATE_INVALID", "产品名称筛选变化，请重新对齐看板")
    if len(clients) != 2 or len(groups) != 1 or groups[0]["filterValue"] != {"condition": "not-in", "value": ["硕才"], "compareType": ""}:
        raise ScreenError("TEMPLATE_INVALID", "客户名或集团筛选结构变化，请重新对齐看板")
    client_conds = sorted(json.dumps(c["filterValue"], ensure_ascii=False, sort_keys=True) for c in clients)
    expected = sorted(json.dumps(v, ensure_ascii=False, sort_keys=True) for v in (
        {"condition": "not-in", "value": ["番茄", "番茄漫剧", "阿里非电商", "漫森长沙", "漫森二部", "漫森三部", "山河", "美偲", "亿汇", "云栖", "鑫杰光年"], "compareType": ""},
        {"condition": "not-contain", "value": ["书读"], "compareType": ""},
    ))
    if client_conds != expected:
        raise ScreenError("TEMPLATE_INVALID", "运营定义客户名筛选变化，请重新对齐看板")
    result_pairs = [(f.get("name"), f.get("filterValue")) for f in body.get("resultFilters", [])]
    if result_pairs.count(("超成本率", {"condition": "less", "value": [0.15]})) != 1 \
            or result_pairs.count(("变现ROI", {"condition": "greater", "value": [0.85]})) != 1 \
            or result_pairs.count(("总消耗", {"condition": "less", "value": [20000]})) != 1 \
            or result_pairs.count(("总消耗", {"condition": "greater", "value": [2000]})) != 1:
        raise ScreenError("TEMPLATE_INVALID", "结果过滤条件变化，请重新对齐看板")
    output_fields = {f.get("name") for f in body.get("fields", [])}
    if not set(EXPECTED_COLUMNS).issubset(output_fields):
        raise ScreenError("TEMPLATE_INVALID", "第二步输出列变化，请重新对齐看板")


def normalize_step2(data: dict[str, Any], excluded: set[str]) -> tuple[list[list[Any]], dict[str, Any], list[str]]:
    columns = [c.get("name") for c in (data.get("columns") or [])]
    missing = [name for name in EXPECTED_COLUMNS if name not in columns]
    if missing:
        raise ScreenError("COLUMNS_CHANGED", f"OLAP 返回列变化，缺少：{missing}")
    rows = data.get("dataList")
    if not isinstance(rows, list) or not isinstance(data.get("rowCount"), int):
        raise ScreenError("QUERY_FAILED", "OLAP 返回缺少明细或总行数")
    if data.get("truncated") or len(rows) >= 100000 or len(rows) != data["rowCount"]:
        raise ScreenError("RESULT_TRUNCATED", f"返回 {len(rows)} 行，总行数 {data['rowCount']}，可能被截断")
    idx = {name: columns.index(name) for name in EXPECTED_COLUMNS}
    result: list[list[Any]] = []
    accounts: set[str] = set()
    for row in rows:
        group = str(as_raw(row[idx["集团"]]))
        client = str(as_raw(row[idx["运营定义客户名"]]))
        product = str(as_raw(row[idx["产品名称"]]))
        account = str(as_raw(row[idx["账户ID"]]))
        try:
            cost = float(as_raw(row[idx["总消耗"]]))
            over = float(as_raw(row[idx["超成本率"]]))
            roi = float(as_raw(row[idx[ROI_COLUMN]]))
        except (TypeError, ValueError) as exc:
            raise ScreenError("INVALID_ROW", "OLAP 返回无效的数值单元格") from exc
        if (group in FORBIDDEN_GROUPS or product in FORBIDDEN_PRODUCTS
                or client in FORBIDDEN_CLIENTS or CLIENT_NOT_CONTAIN in client
                or account in excluded or not re.fullmatch(r"[0-9]+", account)
                or not math.isfinite(cost) or not math.isfinite(over) or not math.isfinite(roi)
                or not 2000 < cost < 20000 or not roi > 0.85 or not over < 0.15):
            raise ScreenError("INVALID_ROW", "返回行不满足第二步筛选条件")
        result.append(row)
        accounts.add(account)
    if result:
        costs = [float(as_raw(r[idx["总消耗"]])) for r in result]
        details = {"row_count": len(result), "account_count": len(accounts),
                   "total_cost": round(sum(costs), 2), "min_cost": min(costs), "max_cost": max(costs)}
    else:
        details = {"row_count": 0, "account_count": 0, "total_cost": 0.0,
                   "min_cost": None, "max_cost": None}
    return result, details, columns


def save_step2(day: datetime, rows: list[list[Any]], columns: list[str]) -> tuple[Path, Path]:
    prefix = f"duanju_iaa_{day:%Y%m%d}_step2"
    out = Path("tmp")
    out.mkdir(parents=True, exist_ok=True)
    details_path = out / f"{prefix}_details.csv"
    accounts_path = out / f"{prefix}_accounts.csv"
    col_names = list(columns)
    with details_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(col_names)
        for row in rows:
            writer.writerow([as_raw(cell) for cell in row])
    account_index = columns.index("账户ID")
    with accounts_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["账户ID"])
        unique = sorted({str(as_raw(r[account_index])) for r in rows}, key=int)
        writer.writerows([[account] for account in unique])
    return accounts_path, details_path


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ScreenError("INVALID_ARGUMENT", message)


def main() -> int:
    started = time.monotonic()
    try:
        parser = Parser(description="短剧 IAA 第二步：客户维度账户筛选")
        parser.add_argument("--date", help="北京时间自然日，格式 YYYY-MM-DD；默认昨日")
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

        kconf_ids, kconf_meta = kconf_antou_ids()
        step1_pairs, step1_summary = normalize(fetch(make_body(day, None)), None)
        step1_ids = {account for _label, account, _cost in step1_pairs}
        excluded = step1_ids | kconf_ids
        if not excluded:
            raise ScreenError("EXCLUSION_EMPTY", "第一步与 KConf 排除集合均为空，中止以保护口径")

        data = fetch(make_step2_body(day, excluded))
        rows, details, columns = normalize_step2(data, excluded)
        accounts_path, details_path = save_step2(day, rows, columns)
        return response(True, 0, started, data={
            "date": date.isoformat(), "window": [day.isoformat(), (day + timedelta(days=1)).isoformat()],
            **details, "excluded_step1_accounts": len(step1_ids),
            "excluded_kconf_accounts": len(kconf_ids),
            "excluded_union_accounts": len(excluded),
            "step1_row_count": step1_summary["row_count"],
            **kconf_meta,
            "accounts_file": str(accounts_path), "details_file": str(details_path),
            "delivery": {"账户清单": file_link(accounts_path), "明细": file_link(details_path)},
            "endpoint": "https://internal.example.corp/api/v1/data/query",
        }, msg=f"第二步筛出 {details['account_count']} 个去重账户，{details['row_count']} 行客户维度记录")
    except KeyboardInterrupt:
        return response(False, 130, started, error="INTERRUPTED", msg="用户中断")
    except ScreenError as exc:
        print(f"[screen_step2] {exc.error}: {exc}", file=sys.stderr)
        return response(False, exc.code, started, error=exc.error, msg=str(exc))
    except Exception as exc:
        print(f"[screen_step2] {type(exc).__name__}: {exc}", file=sys.stderr)
        return response(False, 1, started, error="UNEXPECTED_ERROR", msg=f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
