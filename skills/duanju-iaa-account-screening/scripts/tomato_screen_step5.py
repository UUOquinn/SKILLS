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
"""番茄短剧筛选 Step 5：最终客户看板清单（账户ID IN，全字段输出）"""
from __future__ import annotations
import argparse, csv, json, re, sys, time, uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tomato_common import (ScreenError, TZ, as_raw, fetch, response_json, file_link)

TEMPLATE5 = Path(__file__).resolve().parent.parent / "references" / "tomato_step5_template.json"
ROI_COL = "extend_column_78e5bb23-be44-4dcf-a56a-8b270d080729"

class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ScreenError("INVALID_ARGUMENT", message)

def inject_accounts(body: dict[str, Any], accounts: list[str]) -> None:
    holder = [f for f in body.get("filters", []) if f.get("name") == "账户ID"]
    if not holder:
        raise ScreenError("TEMPLATE_INVALID", "Step5 缺账户ID过滤")
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

def main() -> int:
    started = time.monotonic()
    try:
        parser = Parser(description="番茄 Step 5：最终客户看板清单")
        parser.add_argument("--date", help="YYYY-MM-DD")
        parser.add_argument("--accounts-file", required=True, help="Step4 账户 CSV（含账户ID列）")
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
            accounts = [row["账户ID"] for row in rdr if re.fullmatch(r"[0-9]+", row["账户ID"])]
        if not accounts:
            raise ScreenError("EMPTY_INPUT", "账户清单为空")

        body = json.loads(TEMPLATE5.read_text(encoding="utf-8"))
        inject_accounts(body, accounts)
        rewrite_time(body, day)
        data = fetch(body)
        rows = data.get("dataList") or []
        cols = [c.get("nickName") or c.get("name") for c in (data.get("columns") or [])]
        cols = ["变现ROI-iaa" if n == ROI_COL else n for n in cols]

        out = Path("tmp"); out.mkdir(parents=True, exist_ok=True)
        prefix = f"tomato_iaa_{date:%Y%m%d}_step5"
        path_csv = out / f"{prefix}_client_dashboard.csv"
        with path_csv.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f); w.writerow(cols)
            w.writerows([[as_raw(c) for c in r] for r in rows])

        name_idx = [c.get("name") for c in (data.get("columns") or [])].index("账户ID")
        covered = sorted({str(as_raw(r[name_idx])) for r in rows}, key=int)
        total_cost = round(sum(float(as_raw(r[name_idx])) for r in rows) if rows else 0, 2)
        # 修正：用总消耗列
        cost_idx = [c.get("name") for c in (data.get("columns") or [])].index("总消耗")
        total_cost = round(sum(float(as_raw(r[cost_idx])) for r in rows), 2)

        return response_json(True, 0, started, data={
            "date": date.isoformat(), "accounts_total": len(accounts),
            "rows": len(rows), "covered_accounts": len(covered),
            "total_cost": total_cost, "file": str(path_csv),
            "delivery": {"客户看板清单": file_link(path_csv)},
        }, msg=f"Step 5: {len(rows)} 行 / {len(covered)} 账户，总消耗 {total_cost:,.2f} 元")
    except KeyboardInterrupt:
        return response_json(False, 130, started, error="INTERRUPTED", msg="中断")
    except ScreenError as exc:
        print(f"[step5] {exc.error}: {exc}", file=sys.stderr)
        return response_json(False, exc.code, started, error=exc.error, msg=str(exc))
    except Exception as exc:
        print(f"[step5] {type(exc).__name__}: {exc}", file=sys.stderr)
        return response_json(False, 1, started, error="UNEXPECTED_ERROR", msg=f"{type(exc).__name__}: {exc}")

if __name__ == "__main__":
    sys.exit(main())