#!/usr/bin/env -S uv run --script
# Response Contract: stdout 末行 JSON 含 "code" / "next_action" / "duration_ms"，退出码经 sys.exit(main()) 返回

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
"""番茄短剧筛选 Step 3：累加排除 Step2 账户，消耗 10000~30000 + 超成本率<0.15"""
from __future__ import annotations
import argparse, csv, json, math, re, sys, time, uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tomato_common import (ScreenError, TZ, as_raw, fetch, make_body, normalize_step1,
                    response_json, config_whitelist_ids, CONFIG_BASE, CONFIG_KEY, AB_KEY, file_link)
# import step2 logic inline
import importlib.util as _iu; _iu_p = Path(__file__).parent / "tomato_screen_step2.py"
_spec = _iu.spec_from_file_location("tomato_screen_step2", _iu_p); _m = _iu.module_from_spec(_spec); _spec.loader.exec_module(_m)
step2_main = _m

TEMPLATE3 = Path(__file__).resolve().parent.parent / "references" / "tomato_step3_template.json"
EXPECTED_COLS = ["账户ID", "总消耗", "超成本率"]

class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ScreenError("INVALID_ARGUMENT", message)

def load_template() -> dict[str, Any]:
    if not TEMPLATE3.is_file():
        raise ScreenError("TEMPLATE_MISSING", "Step3 模板缺失")
    body = json.loads(TEMPLATE3.read_text(encoding="utf-8"))
    if body.get("dataSetId") != 205802:
        raise ScreenError("TEMPLATE_INVALID", "Step3 数据集变化")
    return body

def inject_accounts(body: dict[str, Any], accounts: set[str]) -> None:
    """把所有 账户ID NOT IN placeholder 替换为排除集。"""
    replaced = 0
    for f in body["filters"]:
        if f.get("name") == "账户ID" and f.get("filterValue", {}).get("value") == ["__ACCOUNTS_PLACEHOLDER__"]:
            f["filterValue"]["value"] = sorted(accounts)
            if isinstance(f.get("originalFilterValue"), dict):
                f["originalFilterValue"]["value"] = sorted(accounts)
            replaced += 1
    if not replaced:
        raise ScreenError("TEMPLATE_INVALID", "Step3 缺少账户占位")

def rewrite_time(body: dict[str, Any], day: datetime) -> None:
    begin = int(day.timestamp() * 1000)
    end = int((day + timedelta(days=1)).timestamp() * 1000) - 1
    dts = [f for f in body["filters"] if f.get("filterType") == "datetime"]
    if len(dts) != 1:
        raise ScreenError("TEMPLATE_INVALID", "Step3 时间过滤结构变化")
    dts[0]["filterValue"].update(startTime=begin, endTime=end)
    if isinstance(dts[0].get("originalFilterValue"), dict):
        dts[0]["originalFilterValue"]["value"] = [begin, end]
    body["useCache"] = False
    body["openQueryId"] = str(uuid.uuid4())
    body["queryTime"] = int(datetime.now(TZ).timestamp() * 1000)

def normalize(data_columns: list[dict], data_list: list[list], excluded: set[str]) -> tuple[list[list], dict]:
    names = [c["name"] for c in data_columns]
    missing = [n for n in EXPECTED_COLS if n not in names]
    if missing:
        raise ScreenError("COLUMNS_CHANGED", f"Step3 缺列 {missing}")
    idx = {n: names.index(n) for n in EXPECTED_COLS}
    rows: list[list] = []
    seen: set[str] = set()
    for row in data_list:
        acc = str(as_raw(row[idx["账户ID"]]))
        cost = float(as_raw(row[idx["总消耗"]]))
        over = float(as_raw(row[idx["超成本率"]]))
        if (acc in excluded or not re.fullmatch(r"[0-9]+", acc)
            or not math.isfinite(cost) or not 10000 < cost < 30000
            or not math.isfinite(over) or not over < 0.15):
            raise ScreenError("INVALID_ROW", f"Step3 行不符 account={acc} cost={cost} over={over}")
        rows.append(row)
        seen.add(acc)
    total_cost = round(sum(float(as_raw(r[idx["总消耗"]])) for r in rows), 2)
    return rows, {"row_count": len(rows), "account_count": len(seen), "total_cost": total_cost}

def step2_results(day: datetime) -> tuple[list[str], dict[str, Any]]:
    """内联运行 Step2 获取账户集和 meta。"""
    # 不可能直接调用 main()（参数系统），此地复制 Step2 逻辑
    config_ids, config_meta = config_whitelist_ids()
    step1_pairs, step1_summary = normalize_step1(fetch(make_body(day, None)), None)
    step1_ids = {a for _, a, _ in step1_pairs}
    excluded = step1_ids | config_ids
    body = _m.load_template()
    _m.inject_exclusion(body, excluded)
    _m.rewrite_time(body, day)
    data = fetch(body)
    rows, details = _m.normalize(data["dataList"], data["columns"], excluded)
    cols = [c["name"] for c in data["columns"]]
    accounts = sorted({str(as_raw(r[cols.index("账户ID")])) for r in rows}, key=int)
    return accounts, {"excluded_step1": len(step1_ids), "excluded_config": len(config_ids),
                      "excluded_union": len(excluded), "step1_rows": step1_summary["row_count"],
                      "step2_rows": details["row_count"], **config_meta}

def main() -> int:
    started = time.monotonic()
    try:
        parser = Parser(description="番茄 Step 3：累加排除+消耗区间筛选")
        parser.add_argument("--date", help="YYYY-MM-DD")
        parser.add_argument("--accounts-file", help="Step2 账户 CSV")
        args = parser.parse_args()
        today = datetime.now(TZ).date()
        try:
            date = datetime.fromisoformat(args.date).date() if args.date else today - timedelta(days=1)
        except ValueError as exc:
            raise ScreenError("INVALID_DATE") from exc
        if date >= today:
            raise ScreenError("INVALID_DATE", "只能查询已完成自然日")
        day = datetime(date.year, date.month, date.day, tzinfo=TZ)

        if args.accounts_file:
            with Path(args.accounts_file).open(encoding="utf-8-sig", newline="") as f:
                rdr = csv.DictReader(f)
                if "账户ID" not in (rdr.fieldnames or []):
                    raise ScreenError("VALIDATION_ERROR", "缺少账户ID列")
                accounts = [row["账户ID"] for row in rdr]
            step2_meta = {"accounts_source": args.accounts_file}
        else:
            accounts, step2_meta = step2_results(day)
            step2_meta["accounts_source"] = "step2_recomputed"
        account_set = set(accounts)
        if not account_set:
            raise ScreenError("EMPTY_INPUT", "Step2 无账户")

        body = load_template()
        inject_accounts(body, account_set)
        rewrite_time(body, day)
        data = fetch(body)
        rows, details = normalize(data["columns"], data["dataList"], account_set)

        out = Path("tmp"); out.mkdir(parents=True, exist_ok=True)
        prefix = f"tomato_iaa_{date:%Y%m%d}_step3"
        det_path = out / f"{prefix}_details.csv"
        acc_path = out / f"{prefix}_accounts.csv"
        names = [c["name"] for c in data["columns"]]
        with det_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f); w.writerow(names)
            for r in rows: w.writerow([as_raw(c) for c in r])
        step3_accounts = sorted({str(as_raw(r[names.index("账户ID")])) for r in rows}, key=int)
        with acc_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f); w.writerow(["账户ID"])
            w.writerows([[a] for a in step3_accounts])

        return response_json(True, 0, started, data={
            "date": date.isoformat(), **step2_meta, **details,
            "accounts_file": str(acc_path), "details_file": str(det_path),
            "delivery": {"账户清单": file_link(acc_path), "明细": file_link(det_path)},
        }, msg=f"Step 3: {details['row_count']} 行 / {details['account_count']} 账户，总消耗 {details['total_cost']:,.2f} 元")
    except KeyboardInterrupt:
        return response_json(False, 130, started, error="INTERRUPTED", msg="中断")
    except ScreenError as exc:
        print(f"[step3] {exc.error}: {exc}", file=sys.stderr)
        return response_json(False, exc.code, started, error=exc.error, msg=str(exc))
    except Exception as exc:
        print(f"[step3] {type(exc).__name__}: {exc}", file=sys.stderr)
        return response_json(False, 1, started, error="UNEXPECTED_ERROR", msg=f"{type(exc).__name__}: {exc}")

if __name__ == "__main__":
    sys.exit(main())