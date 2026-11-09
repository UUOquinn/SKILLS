#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""往调整台账追加一行。每在策略平台成功新建一条策略就调一次。

台账同时承担三个作用：中断重跑时判断哪些账户已经写过、给 decide.py
做冷却期过滤、以及事后复盘时回答「当时基于什么数据做的这个决定」。
格式错了这三件事都会失效，所以别手写，用这个脚本。

    uv run scripts/append_ledger.py --ledger data/adjust_ledger.csv \
        --account 114368154 --coefficient 0.70 \
        --strategy 短剧-自动-114368154 \
        --roi 0.9752209806 --base-used 0.68 \
        --window "2026-09-15 12:02~12:32"

`--roi` / `--base-used` / `--window` 是可选的可读性字段，不影响冷却期逻辑，
但强烈建议带上——少了它们，事后只能看到「调了」，看不到「为什么调」。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from decide import LEDGER_HEADER, load_ledger, parse_time


def _emit(ok: bool, code: int, error: str | None, msg: str) -> None:
    """按 Response Contract 在 stdout 最后一行输出机器可读结果。

    退出码与这里的 code 必须一致：调用方先读 code，再按
    references/error-policy.json 的有限动作映射决定下一步。
    """
    payload: dict[str, object] = {
        "ok": ok,
        "code": code, "duration_ms": 0,
        "msg": msg,
        "next_action": {
            "type": "CONTINUE" if ok else "STOP",
            "retryable": False,
        },
    }
    if error:
        payload["error"] = error
    print(json.dumps(payload, ensure_ascii=False))


def _migrate_header(path: Path) -> None:
    """旧台账（只有前 4 列）自动升级到当前表头，旧行补空列。

    不升级的话新行比表头多 4 列，文件变成参差不齐的 CSV，
    后续用 DictReader 读会把多出的值扔进 None 键，复盘字段就丢了。
    """
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return
    header = [c.strip() for c in rows[0]]
    if header == list(LEDGER_HEADER):
        return
    if header != list(LEDGER_HEADER[: len(header)]):
        # 表头不是当前格式的前缀，不确定语义，不碰
        return
    width = len(LEDGER_HEADER)
    fixed = [list(LEDGER_HEADER)]
    for row in rows[1:]:
        if not row:
            continue
        fixed.append(list(row) + [""] * (width - len(row)) if len(row) < width else list(row))
    with path.open("w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(fixed)


def main() -> int:
    parser = argparse.ArgumentParser(description="追加一行调整台账")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--coefficient", required=True)
    parser.add_argument("--strategy", required=True, help="平台上的策略名")
    parser.add_argument("--at", default=None, help="调整时间，默认当前时间")
    parser.add_argument("--roi", default="", help="本轮该账户的聚合 ROI，复盘用")
    parser.add_argument("--base-used", default="", help="本次实际用的基数")
    parser.add_argument(
        "--window",
        default="",
        help="取数窗口，形如 '2026-09-15 12:02~12:32'；也可用 --window-start/--window-end",
    )
    parser.add_argument("--window-start", default="", help="窗口左端（与 --window 二选一）")
    parser.add_argument("--window-end", default="", help="窗口右端（与 --window 二选一）")
    parser.add_argument("--strategy-id", default="", help="策略ID（纯数字，报告脚本 report.py 用）")
    parser.add_argument(
        "--status",
        default="",
        help="写入后回读的策略状态：生效中 / 待审核 / 待发布 / 写入失败",
    )
    args = parser.parse_args()

    # P1方案4：参数校验前置
    required_fields = {
        "account": args.account,
        "coefficient": args.coefficient,
        "strategy": args.strategy,
        "strategy-id": args.strategy_id,
        "status": args.status,
        "roi": args.roi,
        "base-used": args.base_used,
        "window": args.window or (args.window_start and args.window_end),
    }
    missing = [k for k, v in required_fields.items() if not str(v).strip()]
    if missing:
        _emit(False, 1, "MISSING_REQUIRED", f"缺少必填参数：{missing}")
        return 1

    win_start = args.window_start.strip()
    win_end = args.window_end.strip()
    if args.window.strip() and not (win_start or win_end):
        text = args.window.strip()
        for sep in ("~", "\uff5e", " 到 ", "--", ".."):
            if sep in text:
                left, right = text.split(sep, 1)
                win_start, win_end = left.strip(), right.strip()
                break
        else:
            win_start = text

    when = parse_time(args.at) if args.at else datetime.now()
    path = Path(args.ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()

    if not is_new:
        _migrate_header(path)

    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(LEDGER_HEADER)
        writer.writerow([
            args.account,
            when.strftime("%Y-%m-%d %H:%M"),
            args.coefficient,
            args.strategy,
            args.roi,
            win_start,
            win_end,
            args.base_used,
            args.strategy_id,
            args.status,
        ])

    total = len(load_ledger(path))
    print(f"已记账：{args.account} -> {args.coefficient} @ {when:%Y-%m-%d %H:%M}"
          f"（台账现有 {total} 个账户）")
    _emit(True, 0, None,
          f"已记账 {args.account} -> {args.coefficient}（台账现有 {total} 个账户）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _emit(False, 130, "INTERRUPTED", "用户中断")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"[append_ledger] {type(exc).__name__}: {exc}", file=sys.stderr)
        _emit(False, 1, "UNEXPECTED_ERROR", f"{type(exc).__name__}: {exc}")
        sys.exit(1)
