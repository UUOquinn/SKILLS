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
"""番茄短剧筛选 Step 1：大池子筛账户（可投范围=七猫/爱奇艺/番茄，消耗>0.1）"""
from __future__ import annotations
import argparse, csv, re, sys, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tomato_common import (ScreenError, TZ, as_raw, fetch, make_body, normalize_step1, response_json)

class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ScreenError("INVALID_ARGUMENT", message)

def main() -> int:
    started = time.monotonic()
    try:
        parser = Parser(description="番茄 Step 1：大池子筛账户")
        parser.add_argument("--date", help="北京时间自然日 YYYY-MM-DD，默认昨日")
        parser.add_argument("--item", choices=["七猫", "爱奇艺", "番茄"],
                           help="单范围补跑")
        args = parser.parse_args()
        today = datetime.now(TZ).date()
        if args.date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
            raise ScreenError("INVALID_DATE", "格式 YYYY-MM-DD")
        try:
            date = datetime.fromisoformat(args.date).date() if args.date else today - timedelta(days=1)
        except ValueError as exc:
            raise ScreenError("INVALID_DATE", "无效日期") from exc
        if date >= today:
            raise ScreenError("INVALID_DATE", "只能查询已完成的自然日")
        day = datetime(date.year, date.month, date.day, tzinfo=TZ)

        items = [args.item] if args.item else ["七猫", "爱奇艺", "番茄"]
        all_pairs: list[tuple[str, str, str]] = []
        for label in items:
            body = make_body(day, label)
            data = fetch(body)
            pairs, summary = normalize_step1(data, label)
            all_pairs.extend(pairs)

        accounts = sorted({a for _, a, _ in all_pairs}, key=int)
        out = Path("tmp")
        out.mkdir(parents=True, exist_ok=True)
        prefix = f"tomato_iaa_{date:%Y%m%d}"
        acc_file = out / f"{prefix}_accounts.csv"
        det_file = out / f"{prefix}_details.csv"
        with acc_file.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f); w.writerow(["账户ID"])
            w.writerows([[a] for a in accounts])
        with det_file.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f); w.writerow(["可投范围", "账户ID", "总消耗"])
            w.writerows(all_pairs)
        total_cost = round(sum(float(c) for _, _, c in all_pairs), 2)
        return response_json(True, 0, started, data={
            "date": date.isoformat(), "row_count": len(all_pairs),
            "account_count": len(accounts), "total_cost": total_cost,
            "accounts_file": str(acc_file), "details_file": str(det_file),
        }, msg=f"Step 1: {len(all_pairs)} 行 / {len(accounts)} 去重账户，总消耗 {total_cost:,.2f} 元")
    except KeyboardInterrupt:
        return response_json(False, 130, started, error="INTERRUPTED", msg="中断")
    except ScreenError as exc:
        print(f"[step1] {exc.error}: {exc}", file=sys.stderr)
        return response_json(False, exc.code, started, error=exc.error, msg=str(exc))
    except Exception as exc:
        print(f"[step1] {type(exc).__name__}: {exc}", file=sys.stderr)
        return response_json(False, 1, started, error="UNEXPECTED_ERROR", msg=f"{type(exc).__name__}: {exc}")

if __name__ == "__main__":
    sys.exit(main())