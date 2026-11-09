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
"""番茄短剧筛选 Step 4：Step3 账户放入 123403 实时数据，筛选消耗>500"""
from __future__ import annotations
import argparse, csv, json, math, re, sys, time, uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tomato_common import (ScreenError, TZ, as_raw, response_json, file_link)
import requests
from ks_aimate.sso_login_client import SmartSSOSession, SSOAuthRequiredError

TEMPLATE4 = Path(__file__).resolve().parent.parent / "references" / "tomato_step4_template.json"
EXPECTED_COLS = ["__time", "账户ID", "总消耗", "联盟总消耗",
                 "extend_column_9779d4d1-d60c-4950-8cb6-7d5680053a2f",
                 "超成本率", "平均浅度出价（下发口径）", "(浅度)转化数",
                 "extend_column_a959aa9c-c044-4f76-aca7-b4760e8bbec2",
                 "当日广告变现ROI"]

class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ScreenError("INVALID_ARGUMENT", message)

def fetch_step4(body: dict[str, Any]) -> dict[str, Any]:
    url = "https://internal.example.corp/api/v1/data/query"
    try:
        resp = SmartSSOSession().request("POST", url, json=body,
                                          headers={"Content-Type": "application/json",
                                                   "Referer": "https://internal.example.corp/"},
                                          timeout=120)
    except SSOAuthRequiredError as exc:
        raise ScreenError("SSO_EXPIRED", "OLAP SSO 失效", 2) from exc
    except requests.Timeout:
        raise ScreenError("API_TIMEOUT", "OLAP 查询超时")
    if resp.status_code != 200:
        raise ScreenError("API_FAILED", f"OLAP HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("result") != 1:
        raise ScreenError("QUERY_FAILED", f"OLAP {data.get('error_msg','?')[:200]}")
    return data["data"]

def inject_accounts(body: dict[str, Any], accounts: list[str]) -> None:
    holder = [f for f in body.get("filters", []) if f.get("name") == "账户ID"]
    if not holder:
        raise ScreenError("TEMPLATE_INVALID", "Step4 缺账户ID过滤")
    holder[0]["filterValue"]["value"] = list(accounts)
    if isinstance(holder[0].get("originalFilterValue"), dict):
        holder[0]["originalFilterValue"]["value"] = list(accounts)

def rewrite_time(body: dict[str, Any], day: datetime) -> None:
    begin = int(day.timestamp() * 1000)
    end = int((day + timedelta(days=1)).timestamp() * 1000) - 1
    dts = [f for f in body["filters"] if f.get("filterType") == "datetime"]
    if dts:
        dts[0]["filterValue"].update(startTime=begin, endTime=end)
        if isinstance(dts[0].get("originalFilterValue"), dict):
            dts[0]["originalFilterValue"]["value"] = [begin, end]
    body["useCache"] = False
    body["openQueryId"] = str(uuid.uuid4())
    body["queryTime"] = int(datetime.now(TZ).timestamp() * 1000)

def normalize(data: dict, account_set: set[str], day: datetime) -> tuple[list[list], list[str], dict]:
    cols = [c.get("name") for c in (data.get("columns") or [])]
    rows = data.get("dataList") or []
    idx = {n: cols.index(n) for n in EXPECTED_COLS}
    begin = int(day.timestamp() * 1000)
    end = int((day + timedelta(days=1)).timestamp() * 1000) - 1
    seen: set[tuple] = set()
    out: list[list] = []
    for row in rows:
        acc = str(as_raw(row[idx["账户ID"]]))
        ts = as_raw(row[idx["__time"]])
        cost = float(as_raw(row[idx["总消耗"]]))
        if (acc not in account_set or not re.fullmatch(r"[0-9]+", acc)
            or not isinstance(ts, (int, float)) or not begin <= ts <= end
            or not math.isfinite(cost) or cost <= 500):
            raise ScreenError("INVALID_ROW", f"Step4 行不符 acc={acc} cost={cost}")
        key = (acc, str(ts))
        if key in seen:
            raise ScreenError("INVALID_ROW", "重复账户x时间行")
        seen.add(key)
        out.append(row)
    covered = sorted({str(as_raw(r[idx["账户ID"]])) for r in out}, key=int)
    total_cost = round(sum(float(as_raw(r[idx["总消耗"]])) for r in out), 2)
    return out, covered, {"rows": len(out), "covered_accounts": len(covered), "total_cost": total_cost}

def main() -> int:
    started = time.monotonic()
    try:
        parser = Parser(description="番茄 Step 4：123403 实时数据筛选消耗>500")
        parser.add_argument("--date", help="YYYY-MM-DD")
        parser.add_argument("--accounts-file", required=True, help="Step3 账户 CSV")
        args = parser.parse_args()
        today = datetime.now(TZ).date()
        try:
            date = datetime.fromisoformat(args.date).date() if args.date else today - timedelta(days=1)
        except ValueError as exc:
            raise ScreenError("INVALID_DATE") from exc
        if date >= today:
            raise ScreenError("INVALID_DATE")
        day = datetime(date.year, date.month, date.day, tzinfo=TZ)

        with Path(args.accounts_file).open(encoding="utf-8-sig", newline="") as f:
            rdr = csv.DictReader(f)
            accounts = [row["账户ID"] for row in rdr if row["账户ID"]]
        if not accounts:
            raise ScreenError("EMPTY_INPUT", "账户清单为空")
        account_set = set(accounts)

        body = json.loads(TEMPLATE4.read_text(encoding="utf-8"))
        inject_accounts(body, accounts)
        rewrite_time(body, day)
        data = fetch_step4(body)
        rows, covered, details = normalize(data, account_set, day)

        out = Path("tmp"); out.mkdir(parents=True, exist_ok=True)
        prefix = f"tomato_iaa_{date:%Y%m%d}_step4"
        path_123403 = out / f"{prefix}_123403.csv"
        col_names = [c.get("nickName") or c.get("name") for c in (data.get("columns") or [])]
        col_names = ["时间" if n == "__time" else n for n in col_names]
        raw_rows = []
        for r in rows:
            vals = [as_raw(c) for c in r]
            ts_idx = [c.get("name") for c in (data.get("columns") or [])].index("__time")
            vals[ts_idx] = r[ts_idx].get("v") if isinstance(r[ts_idx], dict) else r[ts_idx]
            raw_rows.append(vals)
        with path_123403.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f); w.writerow(col_names)
            w.writerows(raw_rows)

        missing = sorted(set(accounts) - set(covered), key=int)
        return response_json(True, 0, started, data={
            "date": date.isoformat(), "accounts_total": len(accounts),
            **details, "missing_accounts": missing, "file": str(path_123403),
            "delivery": {"123403实时明细": file_link(path_123403)},
        }, msg=f"Step 4: {details['rows']} 行 / {details['covered_accounts']} 账户，消耗 {details['total_cost']:,.2f}")
    except KeyboardInterrupt:
        return response_json(False, 130, started, error="INTERRUPTED", msg="中断")
    except ScreenError as exc:
        print(f"[step4] {exc.error}: {exc}", file=sys.stderr)
        return response_json(False, exc.code, started, error=exc.error, msg=str(exc))
    except Exception as exc:
        print(f"[step4] {type(exc).__name__}: {exc}", file=sys.stderr)
        return response_json(False, 1, started, error="UNEXPECTED_ERROR", msg=f"{type(exc).__name__}: {exc}")

if __name__ == "__main__":
    sys.exit(main())