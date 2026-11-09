#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///

"""假期系数策略：本地时间窗口计算器。

把用户输入的目标日期、开始时间和结束时间换算成 epoch-ms（Asia/Shanghai，UTC+8 无夏令时），
供后续 mergeEditV2 / batchAddV2 的 beginTime / endTime 使用。纯本地计算，不调接口。
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone

SH = timezone(timedelta(hours=8))

def to_epoch_ms(day: str, clock: str) -> int:
    value = datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=SH)
    return int(value.timestamp() * 1000)


def fail(code: int, error: str, msg: str, duration_ms: int, next_action: dict) -> None:
    print(json.dumps({
        "ok": False,
        "code": code,
        "error": error,
        "msg": msg,
        "duration_ms": duration_ms,
        "next_action": next_action,
    }, ensure_ascii=False))
    sys.exit(code)


def main() -> None:
    started = time.time()
    parser = argparse.ArgumentParser(
        description="把目标日期 + 起止时间换算成 epoch-ms。")
    parser.add_argument("--day", required=True,
                        help="目标日期，格式 YYYY-MM-DD，例如 2026-10-10")
    parser.add_argument("--start-time", required=True,
                        help="开始时间，格式 HH:MM 或 HH:MM:SS，例如 04:00:00")
    parser.add_argument("--end-time", required=True,
                        help="结束时间，格式 HH:MM 或 HH:MM:SS，例如 08:00:00")
    parser.add_argument("--strategy-id", required=True,
                        help="策略 ID，仅用于输出回显，不限制具体数值")
    args = parser.parse_args()

    def elapsed() -> int:
        return int((time.time() - started) * 1000)

    try:
        begin_time = to_epoch_ms(args.day, args.start_time)
        end_time = to_epoch_ms(args.day, args.end_time)
    except ValueError:
        fail(1, "INVALID_DATETIME",
             "日期或时间格式非法：日期为 YYYY-MM-DD，时间为 HH:MM 或 HH:MM:SS",
             elapsed(), {"type": "STOP", "retryable": False})

    if not args.strategy_id.strip():
        fail(1, "INVALID_ID", "策略 ID 不能为空", elapsed(),
             {"type": "STOP", "retryable": False})
    if end_time <= begin_time:
        fail(1, "INVALID_TIME_RANGE", "结束时间必须晚于开始时间且必须在同一天内",
             elapsed(), {"type": "STOP", "retryable": False})

    print(json.dumps({
        "ok": True,
        "code": 0,
        "data": {
            "strategyId": args.strategy_id.strip(),
            "day": args.day,
            "startClock": args.start_time,
            "endClock": args.end_time,
            "beginTime": begin_time,
            "endTime": end_time,
        },
        "duration_ms": elapsed(),
        "next_action": {"type": "CONTINUE"},
    }, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
