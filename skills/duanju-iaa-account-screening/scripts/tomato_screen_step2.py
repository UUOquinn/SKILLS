#!/usr/bin/env -S uv run --script
# Response Contract: stdout 末行 JSON 含 "code" / "next_action" / "duration_ms"，退出码经 sys.exit(main()) 返回

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
"""番茄短剧筛选 Step 2：排除第一步+KConf，按固定条件筛候选账户（总消耗>0.1）"""
from __future__ import annotations
import argparse, csv, json, math, re, sys, time, uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tomato_common import (ScreenError, TZ, as_raw, fetch, make_body, normalize_step1,
                    response_json, kconf_antou_ids, KCONF_BASE, KCONF_KEY, AB_KEY, file_link)

TEMPLATE2 = Path(__file__).resolve().parent.parent / "references" / "tomato_step2_template.json"
EXPECTED_COLUMNS = ["账户ID", "总消耗"]

class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ScreenError("INVALID_ARGUMENT", message)

def load_template() -> dict[str, Any]:
    if not TEMPLATE2.is_file():
        raise ScreenError("TEMPLATE_MISSING", "Step2 模板缺失")
    body = json.loads(TEMPLATE2.read_text(encoding="utf-8"))
    if body.get("dataSetId") != 205802:
        raise ScreenError("TEMPLATE_INVALID", "Step2 数据集变化")
    return body

def inject_exclusion(body: dict[str, Any], excluded: set[str]) -> None:
    """把所有 账户ID NOT IN placeholder 替换为排除集。"""
    replaced = 0
    for f in body["filters"]:
        if f.get("name") == "账户ID" and f.get("filterValue", {}).get("value") == ["__ACCOUNTS_PLACEHOLDER__"]:
            f["filterValue"]["value"] = sorted(excluded)
            if isinstance(f.get("originalFilterValue"), dict):
                f["originalFilterValue"]["value"] = sorted(excluded)
            replaced += 1
    if not replaced:
        body["filters"].append({
            "filterType": "string", "targetId": 3797590,
            "name": "账户ID", "nickName": "账户ID", "type": "dimension", "valueType": "string",
            "filterValue": {"condition": "not-in", "value": sorted(excluded), "compareType": ""},
            "originalFilterValue": {"condition": "not-in", "value": sorted(excluded)},
            "uuid": str(uuid.uuid4()), "disabled": False, "disabledTip": "",
        })

def rewrite_time(body: dict[str, Any], day: datetime) -> None:
    begin = int(day.timestamp() * 1000)
    end = int((day + timedelta(days=1)).timestamp() * 1000) - 1
    dts = [f for f in body["filters"] if f.get("filterType") == "datetime"]
    if len(dts) != 1:
        raise ScreenError("TEMPLATE_INVALID", "Step2 时间过滤结构变化")
    dts[0]["filterValue"].update(startTime=begin, endTime=end)
    if isinstance(dts[0].get("originalFilterValue"), dict):
        dts[0]["originalFilterValue"]["value"] = [begin, end]
    body["useCache"] = False
    body["openQueryId"] = str(uuid.uuid4())
    body["queryTime"] = int(datetime.now(TZ).timestamp() * 1000)

def normalize(items: list[list[Any]], columns: list[dict], excluded: set[str]) -> tuple[list[list[Any]], dict[str, Any]]:
    names = [c.get("name") for c in columns]
    missing = [n for n in EXPECTED_COLUMNS if n not in names]
    if missing:
        raise ScreenError("COLUMNS_CHANGED", f"Step2 列变化，缺 {missing}")
    idx = {n: names.index(n) for n in EXPECTED_COLUMNS}
    results: list[list[Any]] = []
    seen: set[str] = set()
    for row in items:
        cost_raw = as_raw(row[idx["总消耗"]])
        account = str(as_raw(row[idx["账户ID"]]))
        cost = float(cost_raw) if cost_raw is not None else math.nan
        if (account in excluded or not re.fullmatch(r"[0-9]+", account)
            or not math.isfinite(cost) or cost <= 0.1):
            continue  # 本地 post-filter 兜底 OLAP resultFilter 零星未过滤行
        results.append(row)
        seen.add(account)
    total_cost = round(sum(float(as_raw(r[idx["总消耗"]])) for r in results), 2)
    return results, {"row_count": len(results), "account_count": len(seen), "total_cost": total_cost}

def main() -> int:
    started = time.monotonic()
    try:
        parser = Parser(description="番茄 Step 2：排除 Step1+KConf 候选筛查")
        parser.add_argument("--date", help="YYYY-MM-DD，默认昨日")
        args = parser.parse_args()
        today = datetime.now(TZ).date()
        try:
            date = datetime.fromisoformat(args.date).date() if args.date else today - timedelta(days=1)
        except ValueError as exc:
            raise ScreenError("INVALID_DATE", "无效日期") from exc
        if date >= today:
            raise ScreenError("INVALID_DATE", "只能查询已完成自然日")
        day = datetime(date.year, date.month, date.day, tzinfo=TZ)

        kconf_ids, kconf_meta = kconf_antou_ids()
        step1_pairs, step1_summary = normalize_step1(fetch(make_body(day, None)), None)
        step1_ids = {a for _, a, _ in step1_pairs}
        excluded = step1_ids | kconf_ids
        if not excluded:
            raise ScreenError("EXCLUSION_EMPTY", "排除集为空，中止")

        body = load_template()
        inject_exclusion(body, excluded)
        rewrite_time(body, day)
        data = fetch(body)
        rows, details = normalize(data["dataList"], data["columns"], excluded)

        out = Path("tmp")
        out.mkdir(parents=True, exist_ok=True)
        prefix = f"tomato_iaa_{date:%Y%m%d}_step2"
        det_path = out / f"{prefix}_details.csv"
        acc_path = out / f"{prefix}_accounts.csv"
        col_names = [c.get("name") for c in data["columns"]]
        with det_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f); w.writerow(col_names)
            for r in rows: w.writerow([as_raw(c) for c in r])
        accounts = sorted({str(as_raw(r[col_names.index("账户ID")])) for r in rows}, key=int)
        with acc_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f); w.writerow(["账户ID"])
            w.writerows([[a] for a in accounts])

        return response_json(True, 0, started, data={
            "date": date.isoformat(),
            "excluded_step1": len(step1_ids), "excluded_kconf": len(kconf_ids),
            "excluded_union": len(excluded), "step1_row_count": step1_summary["row_count"],
            **details, **kconf_meta,
            "accounts_file": str(acc_path), "details_file": str(det_path),
            "delivery": {"账户清单": file_link(acc_path), "明细": file_link(det_path)},
        }, msg=f"Step 2: {details['row_count']} 行 / {details['account_count']} 账户，总消耗 {details['total_cost']:,.2f} 元")
    except KeyboardInterrupt:
        return response_json(False, 130, started, error="INTERRUPTED", msg="中断")
    except ScreenError as exc:
        print(f"[step2] {exc.error}: {exc}", file=sys.stderr)
        return response_json(False, exc.code, started, error=exc.error, msg=str(exc))
    except Exception as exc:
        print(f"[step2] {type(exc).__name__}: {exc}", file=sys.stderr)
        return response_json(False, 1, started, error="UNEXPECTED_ERROR", msg=f"{type(exc).__name__}: {exc}")

if __name__ == "__main__":
    sys.exit(main())