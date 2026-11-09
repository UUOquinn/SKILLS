#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""把「浅度超成本率」映射为 cpm 系数调整动作。

阈值表是本仓库唯一权威来源（single source of truth），Agent 不得在对话里重新
心算或改写。用法见 ../SKILL.md。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from dataclasses import dataclass, asdict, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

# 缺省基数：账户在策略平台上查不到 cpm 系数基数时使用。
DEFAULT_BASE = Decimal("1.0")

# 冷却期：同一账户在这段时间内不重复调整。
#
# 默认关闭（0）。因为取数窗口长 20 分钟、触发间隔也是 20 分钟，相邻两轮
# 窗口首尾相接、零重叠，同一批转化数据只会被读到一次，不存在重复消费。
#
# 仅当窗口长度大于触发间隔（两轮数据有重叠）时才需要开启：此时传
# `--cooldown-minutes <窗口长度>`，否则同一批数据会被连续几轮重复计入，
# 对同一账户反复下手。
DEFAULT_COOLDOWN_MINUTES = 0

OWNED_PREFIX = "短剧-自动-"


def decide_write_mode(entry: dict[str, Any], account: str) -> dict[str, Any]:
    """判定对这个账户应该新建策略还是原地改系数。

    三种情况，只有第一种能原地改：

    1. 策略只含本账户 + 名字是本流程建的 -> UPDATE
    2. 策略还含其他账户 -> CREATE，改了会连带改掉别人的系数
    3. 只含本账户但是别人手建的 -> CREATE，不碰不属于自己的策略

    第 3 条是安全线：这个循环每 20 分钟一轮且无冷却期，不限定归属的话会
    持续改写平台上任何独占某账户的策略，包括人工建的业务策略，而对方
    收不到任何通知。原地修改是不可逆的，改完就没有历史值了。
    """
    ids = entry.get("account_ids") or []
    others = [x for x in ids if x != account]
    name = str(entry.get("name") or "")

    if others:
        return {
            "write_mode": "CREATE",
            "write_reason": f"最新策略还覆盖其他 {len(others)} 个账户，原地改会连带改掉他们",
            "other_account_count": len(others),
            "other_accounts_sample": others[:5],
        }

    if not name.startswith(OWNED_PREFIX):
        return {
            "write_mode": "CREATE",
            "write_reason": f"最新策略「{name}」不是本流程建的（名字不以 {OWNED_PREFIX} 开头），不碰",
            "other_account_count": 0,
            "other_accounts_sample": [],
        }

    return {
        "write_mode": "UPDATE",
        "write_reason": "最新策略只含本账户且为本流程所建，可原地改系数",
        "target_strategy_id": entry.get("strategy_id"),
        "other_account_count": 0,
        "other_accounts_sample": [],
    }


LEDGER_HEADER = (
    "account_id",
    "adjusted_at",
    "new_coefficient",
    "strategy_name",
    "roi",
    "window_start",
    "window_end",
    "base_used",
    # 以下两列是报告脚本 report.py 用的可读性字段，append_ledger.py 可选传。
    # 追加在末尾是为了让旧台账（前 8 列）能被 _migrate_header 自动升级。
    "strategy_id",
    "status",
)
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")

# 系数上下限：顶价封顶、压价封底。超出时直接截断到边界，仍正常写入，不转人工。
CAP_MIN = Decimal("0.10")  # 压价封底：系数最低 0.10
CAP_MAX = Decimal("1.40")  # 顶价封顶：系数最高 1.40

# 超成本率绝对值超过这个值，仍正常按最高/最低档定档写入，但标记“持续监控”。
# 实测真实数据里出现过 +10.90（超成本 1090%），不转人工，直接压 -0.20 并持续监控。
MONITOR_THRESHOLD = Decimal("1.0")

TWO_PLACES = Decimal("0.01")

NO_ACTION = "不操作"

# 导出里常见的汇总行标记。这些行必须挡掉，否则会被当成一个账户建策略。
_AGGREGATE_MARKS = ("合计", "总计", "汇总", "小计", "总和", "全部", "total", "sum", "all")

# 决策表：自上而下第一个命中即生效，互斥且无空档。
# (band_label, 比较方式, 阈值, 增量, 动作)
#
# 自变量是**浅度超成本率**，正负对称：
#   正值 = 超成本了 -> 压价，超得越多压得越狠
#   负值 = 没超、还有余量 -> 顶价，余量越大顶得越多
#   |x| < 0.05 = 正常波动 -> 不动
#
# 边界用**半开区间闭合**，不留空隙：切点取 0.05 / 0.11 / 0.2 / 0.3 / 0.4 / 0.5 / 1.0，
# 落在缝隙里的值归入较轻的那档。
_BANDS: tuple[tuple[str, str, Decimal | None, Decimal, str], ...] = (
    # 压价侧：超成本率为正
    ("rate >= 1.00", "ge", Decimal("1.00"), Decimal("-0.30"), "压价"),
    ("0.50 <= rate < 1.00", "ge", Decimal("0.50"), Decimal("-0.20"), "压价"),
    ("0.40 <= rate < 0.50", "ge", Decimal("0.40"), Decimal("-0.15"), "压价"),
    ("0.30 <= rate < 0.40", "ge", Decimal("0.30"), Decimal("-0.10"), "压价"),
    ("0.20 <= rate < 0.30", "ge", Decimal("0.20"), Decimal("-0.05"), "压价"),
    ("0.11 <= rate < 0.20", "ge", Decimal("0.11"), Decimal("-0.03"), "压价"),
    ("0.05 <= rate < 0.11", "ge", Decimal("0.05"), Decimal("-0.02"), "压价"),
    # 观察区：|rate| < 0.05
    ("-0.05 < rate < 0.05", "gt", Decimal("-0.05"), Decimal("0.00"), NO_ACTION),
    # 顶价侧：超成本率为负（没超成本，有余量）
    ("-0.11 < rate <= -0.05", "gt", Decimal("-0.11"), Decimal("0.02"), "顶价"),
    ("-0.20 < rate <= -0.11", "gt", Decimal("-0.20"), Decimal("0.03"), "顶价"),
    ("-0.30 < rate <= -0.20", "gt", Decimal("-0.30"), Decimal("0.05"), "顶价"),
    ("rate <= -0.30", "always", None, Decimal("0.10"), "顶价"),
)


@dataclass
class Decision:
    account_id: str
    rate: str
    band: str
    action: str
    delta: str
    base_used: str
    base_source: str
    new_coefficient: str
    status: str
    note: str


def decide_delta(rate: Decimal) -> tuple[str, str, Decimal]:
    """按浅度超成本率定档。返回 (band_label, action, delta)。

    rate 用原值比较，不预先四舍五入——档位切点密（0.05 / 0.11 / 0.2 / 0.3 / 0.4 / 0.5 / 1.0），
    提前舍入会把贴边的值拨到隔壁档。
    """
    for label, op, cutoff, delta, action in _BANDS:
        if op == "always":
            return label, action, delta
        if op == "gt" and rate > cutoff:
            return label, action, delta
        if op == "ge" and rate >= cutoff:
            return label, action, delta
    raise AssertionError("决策表存在空档，请检查 _BANDS")


def parse_time(text: str) -> datetime:
    cleaned = text.strip()
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间 {text!r}，支持格式如 2026-09-14 16:29")


def load_ledger(path: str | Path) -> dict[str, datetime]:
    """读取调整台账，返回每个账户最近一次调整时间。台账不存在就返回空。"""
    p = Path(path)
    if not p.exists():
        return {}
    latest: dict[str, datetime] = {}
    with p.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            acc = (row.get("account_id") or "").strip()
            raw = (row.get("adjusted_at") or "").strip()
            if not acc or not raw:
                continue
            try:
                when = parse_time(raw)
            except ValueError:
                continue
            if acc not in latest or when > latest[acc]:
                latest[acc] = when
    return latest


def _reject_non_account(account_id: str) -> str | None:
    """不是一个真实账户ID就返回原因，否则返回 None。

    导出表几乎一定带「合计」行，这行长得和数据行一样，判进决策表就会给一个
    不存在的账户建策略，所以在这里挡死。
    """
    text = account_id.strip()
    lowered = text.lower()
    for mark in _AGGREGATE_MARKS:
        if mark in lowered:
            return f"疑似汇总行（含「{mark}」），不是账户，已剔除"
    if not text.isdigit():
        return "账户ID 不是纯数字，疑似汇总行或表头错位，需人工确认"
    return None


def _parse_decimal(raw: object) -> Decimal | None:
    """只接受无歧义的小数。带 % 的值口径不明（85% 与 0.85 无法区分），一律拒绝。"""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if text in ("", "-", "--", "null", "None", "NULL", "N/A", "na"):
        return None
    if "%" in text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def evaluate(
    account_id: str,
    rate_raw: object = None,
    base_raw: object = None,
    *,
    default_base: Decimal = DEFAULT_BASE,
    cap_min: Decimal = CAP_MIN,
    cap_max: Decimal = CAP_MAX,
    last_adjusted_at: datetime | None = None,
    now: datetime | None = None,
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
) -> Decision:
    def _skip(status: str, note: str, rate_s: str = "-") -> Decision:
        return Decision(
            account_id=account_id, rate=rate_s, band="-",
            action="跳过", delta="0.00", base_used="-", base_source="-",
            new_coefficient="-", status=status, note=note,
        )

    not_account = _reject_non_account(account_id)
    if not_account is not None:
        return _skip("SKIP_NOT_ACCOUNT", not_account)

    rate = _parse_decimal(rate_raw)
    if rate is None:
        return _skip(
            "SKIP_NO_DATA",
            "浅度超成本率缺失或非数值，不操作，先核对取数口径",
            str(rate_raw).strip() if rate_raw is not None else "-",
        )

    # 超成本率绝对值过大（如 +10.90）不转人工，仍按最高/最低档正常定档写入，
    # 后续在 note 里标记“持续监控”提醒关注。
    band, action, delta = decide_delta(rate)

    if action == NO_ACTION:
        # 观察区不写入，所以不算新系数，避免报告里出现一个不会落地的数字。
        return Decision(
            account_id=account_id, rate=str(rate),
            band=band,
            action=NO_ACTION,
            delta="0.00",
            base_used="-",
            base_source="-",
            new_coefficient="-",
            status="NO_ACTION",
            note="落在观察区，规则未覆盖，本轮保持原系数不变",
        )

    if last_adjusted_at is not None and cooldown_minutes > 0:
        moment = now or datetime.now()
        elapsed = moment - last_adjusted_at
        if elapsed < timedelta(minutes=cooldown_minutes):
            mins = int(elapsed.total_seconds() // 60)
            return Decision(
                account_id=account_id, rate=str(rate),
                band=band,
                action="跳过",
                delta="0.00",
                base_used="-",
                base_source="-",
                new_coefficient="-",
                status="SKIP_COOLDOWN",
                note=f"距上次调整仅 {mins} 分钟（冷却期 {cooldown_minutes} 分钟），"
                     f"本轮沿用上次结果，不重复下手",
            )

    base = _parse_decimal(base_raw)
    if base is None:
        base = default_base
        base_source = "新建基数"
    else:
        base_source = "平台现有基数"

    new_coefficient = (base + delta).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    # 封顶/封底：顶价最高到上限、压价最低到下限，超出直接截断到边界，仍正常写入。
    status = "OK"
    note = ""
    if new_coefficient > cap_max:
        new_coefficient = cap_max
        note = f"顶价后系数封顶至上限 {cap_max}"
    elif new_coefficient < cap_min:
        new_coefficient = cap_min
        note = f"压价后系数封底至下限 {cap_min}"

    # 超成本率绝对值过大时标记“持续监控”，不阻断写入。
    if abs(rate) > MONITOR_THRESHOLD:
        suffix = "；超成本率异常，持续监控"
        note = note + suffix if note else suffix[1:]

    return Decision(
        account_id=account_id, rate=str(rate),
        band=band,
        action=action,
        delta=f"{delta:+.2f}",
        base_used=str(base.quantize(TWO_PLACES)),
        base_source=base_source,
        new_coefficient=str(new_coefficient),
        status=status,
        note=note,
    )


def _read_rows(text: str) -> list[dict[str, str]]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and all("\t" in ln for ln in lines):
        # 页面直接复制出来的是 TSV，但金额列常带千分位逗号。每行都有制表符时
        # 直接按 TSV 读，不交给 Sniffer 猜，否则可能被逗号带偏。
        dialect: type[csv.Dialect] | csv.Dialect = csv.excel_tab
    else:
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|")
        except csv.Error:
            dialect = csv.excel_tab if "\t" in text[:4096] else csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if reader.fieldnames is None:
        return []
    normalized = [(f or "").strip() for f in reader.fieldnames]
    reader.fieldnames = normalized
    return [dict(row) for row in reader]


_ACCOUNT_KEYS = ("account_id", "账户id", "账户ID", "accountid", "账户")
# 决策指标是「浅度超成本率」，fetch_roi.py 输出列名是 rate。
_RATE_KEYS = (
    "rate",
    "浅度超成本率",
)
_BASE_KEYS = ("current_coefficient", "base", "基数", "cpm系数", "当前系数")


def _pick(row: dict[str, str], keys: Iterable[str]) -> object:
    lowered = {k.lower(): v for k, v in row.items() if k}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="浅度超成本率 -> cpm 系数调整决策"
    )
    parser.add_argument(
        "--input",
        required=True,
        help="CSV/TSV 文件路径，或 - 表示标准输入。表头需含账户ID与超成本率列",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 而非对齐表格")
    parser.add_argument(
        "--bands-only",
        action="store_true",
        help="第一轮用：只判区间和动作，不算系数。据此只去平台查需要动的账户的基数",
    )
    parser.add_argument(
        "--default-base",
        default=str(DEFAULT_BASE),
        help=f"查不到基数时使用的基数，默认 {DEFAULT_BASE}",
    )
    parser.add_argument("--cap-min", default=str(CAP_MIN))
    parser.add_argument("--cap-max", default=str(CAP_MAX))
    parser.add_argument(
        "--ledger",
        default=None,
        help="调整台账 CSV。记录已写入的账户供中断重跑使用；只有同时传 --cooldown-minutes 才做冷却期判断",
    )
    parser.add_argument(
        "--cooldown-minutes",
        type=int,
        default=DEFAULT_COOLDOWN_MINUTES,
        help="冷却期分钟数，默认 0（关闭）。窗口与触发间隔等长时无需开启；仅当窗口比触发间隔长、两轮数据有重叠时才传窗口长度",
    )
    parser.add_argument("--now", default=None, help="当前时间，默认系统时间。用于复算历史轮次")
    args = parser.parse_args(argv)

    ledger = load_ledger(args.ledger) if args.ledger else {}
    now = parse_time(args.now) if args.now else datetime.now()
    if args.ledger and not ledger:
        print(f"提示：台账 {args.ledger} 为空或不存在，本轮不做冷却期过滤", file=sys.stderr)

    text = sys.stdin.read() if args.input == "-" else open(args.input, encoding="utf-8-sig").read()
    rows = _read_rows(text)
    if not rows:
        print("输入为空或缺少表头，未产生任何决策", file=sys.stderr)
        return 1

    decisions: list[Decision] = []
    for idx, row in enumerate(rows, start=1):
        account = _pick(row, _ACCOUNT_KEYS)
        if account is None or str(account).strip() == "":
            print(f"第 {idx} 行缺少账户ID，已忽略", file=sys.stderr)
            continue
        decisions.append(
            evaluate(
                str(account).strip(),
                _pick(row, _RATE_KEYS),
                None if args.bands_only else _pick(row, _BASE_KEYS),
                default_base=Decimal(args.default_base),
                cap_min=Decimal(args.cap_min),
                cap_max=Decimal(args.cap_max),
                last_adjusted_at=ledger.get(str(account).strip()),
                now=now,
                cooldown_minutes=args.cooldown_minutes,
            )
        )

    seen: dict[str, int] = {}
    for d in decisions:
        seen[d.account_id] = seen.get(d.account_id, 0) + 1
    dupes = sorted(a for a, n in seen.items() if n > 1)
    if dupes:
        print(f"账户ID 重复：{dupes}，请先按账户ID聚合再决策", file=sys.stderr)
        _emit(False, 3, "DUPLICATE_ACCOUNT",
              f"账户ID 重复：{dupes}，请先按账户ID聚合再决策")
        return 3

    if args.bands_only:
        # 这一轮还没查基数，把系数相关字段清空，避免误当成可落地的数字。
        decisions = [
            replace(d, base_used="-", base_source="-", new_coefficient="-", note="")
            for d in decisions
        ]

    if args.json:
        print(json.dumps([asdict(d) for d in decisions], ensure_ascii=False, indent=2))
    else:
        _print_table(decisions, bands_only=args.bands_only)

    ok_count = sum(1 for d in decisions if d.status == "OK")
    _emit(True, 0, None, f"决策完成，{ok_count} 个账户需要新建策略")
    return 0


def _print_table(decisions: list[Decision], bands_only: bool = False) -> None:
    if bands_only:
        headers = ["账户ID", "超成本率", "区间", "动作", "增量", "状态"]
        rows = [[d.account_id, d.rate, d.band, d.action, d.delta, d.status] for d in decisions]
    else:
        headers = ["账户ID", "超成本率", "区间", "动作", "增量", "基数", "基数来源", "新系数", "状态", "备注"]
        rows = [
            [d.account_id, d.rate, d.band, d.action, d.delta, d.base_used, d.base_source,
             d.new_coefficient, d.status, d.note]
            for d in decisions
        ]

    def width(text: str) -> int:
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)

    widths = [max(width(h), *(width(r[i]) for r in rows)) if rows else width(h)
              for i, h in enumerate(headers)]

    def fmt(cells: list[str]) -> str:
        return "  ".join(c + " " * (widths[i] - width(c)) for i, c in enumerate(cells))

    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))

    ups = sum(1 for d in decisions if d.action == "顶价")
    downs = sum(1 for d in decisions if d.action == "压价")
    monitors = sum(1 for d in decisions if "持续监控" in d.note)
    idle = sum(1 for d in decisions if d.status == "NO_ACTION")
    skips = sum(1 for d in decisions if d.status in ("SKIP_NO_DATA", "SKIP_NOT_ACCOUNT"))
    cooling = sum(1 for d in decisions if d.status == "SKIP_COOLDOWN")
    writable = sum(1 for d in decisions if d.status == "OK")
    print(
        f"\n合计 {len(decisions)} 个账户：顶价 {ups}，压价 {downs}，观察区 {idle}，"
        f"冷却中 {cooling}，跳过 {skips}，持续监控 {monitors}"
    )
    print(f"需要在策略平台新建策略的账户数：{writable}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _emit(False, 130, "INTERRUPTED", "用户中断")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        print(f"[decide] {type(exc).__name__}: {exc}", file=sys.stderr)
        _emit(False, 1, "UNEXPECTED_ERROR", f"{type(exc).__name__}: {exc}")
        sys.exit(1)
