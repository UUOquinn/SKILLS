#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""把 decide.py 的决策结果和调整台账合并成完整播报文本。

输出是一整段可直接转发到群的播报消息——执行 AI 不需要拼表格、不需要改格式，把 stdout 原样传给 message 工具即可。

用法：
    uv run scripts/decide.py --input data/roi.csv --json > data/decision.json
    ... 逐账户写入，每写一条调 append_ledger.py（带 --strategy-id --status --window）...
    uv run scripts/report.py \\
        --decision data/decision.json \\
        --ledger data/adjust_ledger.csv \\
        --window "14:35~14:55"
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from decide import parse_time


def _parse_window(text: str) -> tuple[str, str]:
    """'13:35~13:55' 或 '2026-09-24 13:35~13:55' -> (start, end)。"""
    for sep in ("~", "\uff5e", " 到 ", "--", ".."):
        if sep in text:
            left, right = text.split(sep, 1)
            return left.strip(), right.strip()
    return text.strip(), ""


def _short_time(time_str: str) -> str:
    """'2026-09-24 14:55' -> '14:55'；'14:55' -> '14:55'。"""
    if not time_str:
        return time_str
    s = time_str.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return s  # already HH:MM or unrecognised


def _add_minutes(time_str: str, offset_min: int) -> str:
    """HH:MM 或 YYYY-MM-DD HH:MM 字符串加 offset_min 分钟，返回 HH:MM。"""
    if not time_str:
        return "??:??"
    t: datetime | None = None
    for fmt in ("%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            t = datetime.strptime(time_str.strip(), fmt)
            break
        except ValueError:
            continue
    if t is None:
        return "??:??"
    t = t + timedelta(minutes=offset_min)
    return t.strftime("%H:%M")


def _emit(ok: bool, code: int, error: str | None, msg: str) -> None:
    """按 Response Contract 在 stdout 最后一行输出机器可读结果。"""
    payload: dict[str, object] = {
        "ok": ok,
        "code": code,
        "duration_ms": 0,
        "msg": msg,
        "next_action": {
            "type": "CONTINUE" if ok else "STOP",
            "retryable": False,
        },
    }
    if error:
        payload["error"] = error
    print(json.dumps(payload, ensure_ascii=False))


_SKIP_LABELS = {
    "SKIP_NO_DATA": "无数据，跳过",
    "SKIP_NOT_ACCOUNT": "非账户，跳过",
    "SKIP_COOLDOWN": "冷却中，跳过",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成完整播报文本（可直接转发到群）"
    )
    parser.add_argument("--decision", required=True, help="decide.py --json 的输出文件")
    parser.add_argument("--ledger", required=True, help="调整台账 CSV")
    parser.add_argument("--window", default="", help="本轮取数窗口，如 '14:35~14:55'")
    parser.add_argument(
        "--since",
        default="",
        help="兜底：读该时间之后记的台账行（窗口匹配不到时用）",
    )
    parser.add_argument(
        "--round",
        default="",
        help="轮次编号（可选），如 '87'",
    )
    parser.add_argument(
        "--now",
        default="",
        help="当前时间 HH:MM，用于推算下一轮时间（可选）",
    )
    args = parser.parse_args()

    decision_path = Path(args.decision)
    if not decision_path.exists():
        print(f"决策文件不存在：{decision_path}", file=sys.stderr)
        _emit(False, 2, "MISSING_DECISION", f"决策文件不存在：{decision_path}")
        return 2
    try:
        text = decision_path.read_text(encoding="utf-8")
        decisions, _ = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        print(f"决策文件不是合法 JSON：{exc}", file=sys.stderr)
        _emit(False, 2, "BAD_DECISION_JSON", f"决策文件不是合法 JSON：{exc}")
        return 2
    if not isinstance(decisions, list):
        print("决策文件顶层应为 JSON 数组", file=sys.stderr)
        _emit(False, 2, "BAD_DECISION_JSON", "决策文件顶层应为 JSON 数组")
        return 2

    win_start, win_end = _parse_window(args.window)
    since: datetime | None = None
    if args.since:
        try:
            since = parse_time(args.since)
        except ValueError:
            print(f"--since 无法解析：{args.since}", file=sys.stderr)
            _emit(False, 2, "BAD_SINCE", f"--since 无法解析：{args.since}")
            return 2

    ledger_map: dict[str, dict[str, str]] = {}
    ledger_path = Path(args.ledger)
    if ledger_path.exists():
        with ledger_path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                acc = (row.get("account_id") or "").strip()
                if not acc:
                    continue
                r_ws = (row.get("window_start") or "").strip()
                r_at = (row.get("adjusted_at") or "").strip()

                if win_start:
                    if not (r_ws.endswith(win_start) or win_start.endswith(r_ws)):
                        continue
                elif since and r_at:
                    try:
                        if parse_time(r_at) < since:
                            continue
                    except ValueError:
                        pass
                prev = ledger_map.get(acc)
                if prev and r_at and prev.get("adjusted_at", "") > r_at:
                    continue
                ledger_map[acc] = {
                    "strategy_id": (row.get("strategy_id") or "").strip(),
                    "strategy_name": (row.get("strategy_name") or "").strip(),
                    "status": (row.get("status") or "").strip(),
                    "adjusted_at": r_at,
                }

    rows: list[list[str]] = []
    observe: list[str] = []
    ups = downs = 0
    creates = updates = 0
    for d in decisions:
        acc = str(d.get("account_id") or "").strip()
        rate = d.get("rate") if d.get("rate") not in (None, "") else "-"
        base = d.get("base_used") if d.get("base_used") not in (None, "") else "-"
        newc = d.get("new_coefficient") if d.get("new_coefficient") not in (None, "") else "-"
        action = d.get("action") or ""
        d_status = d.get("status") or ""
        wm = d.get("write_mode") or ""

        if action == "顶价":
            ups += 1
        elif action == "压价":
            downs += 1
        if d_status == "OK":
            if wm == "CREATE":
                creates += 1
            else:
                updates += 1

        lr = ledger_map.get(acc, {})
        strategy = lr.get("strategy_id") or lr.get("strategy_name") or "-"
        effect = lr.get("status") or "-"

        if d_status == "NO_ACTION":
            observe.append(f"{acc}（rate {rate}）")
            strategy = "—"
            effect = "观察区，未操作"
        elif d_status in _SKIP_LABELS:
            strategy = "—"
            effect = _SKIP_LABELS[d_status]

        rows.append([acc, str(rate), str(base), str(newc), strategy, effect])

    if not rows:
        print("没有可报告的行", file=sys.stderr)
        _emit(False, 3, "EMPTY_ROWS", "没有可报告的行")
        return 3

    writes = creates + updates

    # ── 标题行 ──
    round_tag = f"第 {args.round} 轮 " if args.round else ""
    round_time = args.now or ""
    if not round_time and win_end:
        # 用窗口右端 + 5 分钟推算当前轮时间
        round_time = _add_minutes(win_end, 5)
    header = f"{round_tag}{round_time}｜窗口 {_short_time(win_start)}~{_short_time(win_end)}｜写入 {writes} 条"
    detail_parts = []
    if creates:
        detail_parts.append(f"新建 {creates}")
    if updates:
        detail_parts.append(f"改系数 {updates}")
    if detail_parts:
        header += "（" + " + ".join(detail_parts) + "）"
    header += f"｜顶价 {ups} 压价 {downs}"
    print(header)
    print()

    # ── 表格 ──
    print("| 账户ID | 当前rate | 当前系数 | 调整后系数 | 策略ID | 是否生效 |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print("| " + " | ".join(r) + " |")

    _emit(True, 0, None,
          f"播报生成：写入 {writes} 条（新建 {creates} + 改系数 {updates}），"
          f"顶价 {ups}，压价 {downs}，观察区 {len(observe)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _emit(False, 130, "INTERRUPTED", "用户中断")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"[report] {type(exc).__name__}: {exc}", file=sys.stderr)
        _emit(False, 1, "UNEXPECTED_ERROR", f"{type(exc).__name__}: {exc}")
        sys.exit(1)