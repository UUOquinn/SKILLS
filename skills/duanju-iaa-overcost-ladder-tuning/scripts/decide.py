#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""把 OLAP 聚合结果 + 阶梯策略名单组合成迁移决策。

    uv run scripts/decide.py \
      --accounts data/accounts_202609231700.csv \
      --strategies data/strategies_202609231700.json

阈值与档位是本仓库唯一权威来源，Agent 不得在对话里心算或改写：

    筛选（压价触发）：联盟总消耗 >= 500 且 浅度超成本率 >= 0.20
    阶梯：0.8 -> 0.6 -> 0.4 -> 0.2（每步 -0.2，基数为 1.0）
    封底：压到 0.2 仍超，保持 0.2 不动，标记持续监控
    撤出：账户在策略里但超成本率 < 0.20（不再超），从策略名单拿掉

「平台即状态」：账户当前档位 = 它在哪条阶梯策略的名单里。无本地台账，
重跑天然幂等——账户已经降档后，下轮查询策略名单看到的就是新档位，不会
重复下手。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_STARTED_AT = time.perf_counter()

LEVELS = ("0.8", "0.6", "0.4", "0.2")
FLOOR_LEVEL = "0.2"
COST_THRESHOLD = Decimal("500")
RATE_THRESHOLD = Decimal("0.20")

ACT_ENTER = "进入0.8"
ACT_DOWN = "降档"
ACT_FLOOR = "保持0.2封底"
ACT_HOLD = "保持当前档"
ACT_REMOVE = "撤出"
ACT_NONE = "无操作"
ACT_BLOCKED = "档位缺失"


def _dur_ms() -> int:
    return int((time.perf_counter() - _STARTED_AT) * 1000)


def _ok(data: dict[str, Any], msg: str = "") -> int:
    print(json.dumps(
        {"ok": True, "code": 0, "duration_ms": _dur_ms(), "msg": msg, "data": data,
         "next_action": {"type": "CONTINUE", "retryable": False}},
        ensure_ascii=False,
    ))
    return 0


def _fail(error: str, msg: str, code: int = 1, retryable: bool = False) -> int:
    print(json.dumps(
        {"ok": False, "code": code, "error": error, "msg": msg,
         "duration_ms": _dur_ms(),
         "next_action": {"type": "STOP", "retryable": retryable}},
        ensure_ascii=False,
    ))
    return code


def _dec(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if text in ("", "-", "--", "null", "None", "NULL", "N/A", "na"):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _next_level(level: str) -> str | None:
    try:
        idx = LEVELS.index(level)
    except ValueError:
        return None
    if idx + 1 < len(LEVELS):
        return LEVELS[idx + 1]
    return None


def _load_accounts(path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            acc = (row.get("account_id") or "").strip()
            if not acc:
                continue
            out.append({
                "account_id": acc,
                "rate": _dec(row.get("rate")),
                "cost": _dec(row.get("cost")) or Decimal("0"),
            })
    return out


def _load_strategies(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def evaluate(
    account: dict[str, Any],
    account_to_level: dict[str, str],
    strategies: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    acc = account["account_id"]
    rate = account["rate"]
    cost = account["cost"]
    level = account_to_level.get(acc)

    base = {
        "account_id": acc,
        "rate": str(rate) if rate is not None else None,
        "cost": str(cost),
        "current_level": level,
    }

    def blocked(target_level: str, why: str) -> dict[str, Any]:
        return {
            **base,
            "action": ACT_BLOCKED,
            "target_level": target_level,
            "note": why,
        }

    if rate is None:
        return {**base, "action": ACT_NONE, "note": "窗口内无超成本率数据，不操作"}

    over = rate >= RATE_THRESHOLD
    enough_cost = cost >= COST_THRESHOLD

    if level is None:
        # 未被任何阶梯策略管理
        if over and enough_cost:
            target = LEVELS[0]
            if target not in strategies:
                return blocked(target, f"目标档位 {target} 策略缺失，需先创建")
            return {
                **base,
                "action": ACT_ENTER,
                "target_level": target,
                "target_strategy_id": strategies[target].get("strategy_id"),
                "note": f"超成本率 {rate} >= 0.20 且消耗 {cost} >= 500，进入 {target} 档",
            }
        return {**base, "action": ACT_NONE, "note": "未命中筛选，不纳入管理"}

    # 已在某档策略里
    if not over:
        return {
            **base,
            "action": ACT_REMOVE,
            "target_level": level,
            "from_strategy_id": strategies.get(level, {}).get("strategy_id"),
            "note": f"超成本率 {rate} 回落到 0.20 以下，从 {level} 档撤出",
        }

    if not enough_cost:
        return {
            **base,
            "action": ACT_HOLD,
            "target_level": level,
            "note": f"仍超成本（{rate}）但消耗 {cost} < 500，暂不继续压，保持 {level} 档",
        }

    nxt = _next_level(level)
    if nxt is None:
        return {
            **base,
            "action": ACT_FLOOR,
            "target_level": FLOOR_LEVEL,
            "note": f"已到封底 {FLOOR_LEVEL} 仍超成本（{rate}），保持不动，持续监控",
        }
    if nxt not in strategies:
        return blocked(nxt, f"目标档位 {nxt} 策略缺失，需先创建")
    return {
        **base,
        "action": ACT_DOWN,
        "target_level": nxt,
        "from_strategy_id": strategies.get(level, {}).get("strategy_id"),
        "to_strategy_id": strategies[nxt].get("strategy_id"),
        "note": f"仍超成本（{rate} >= 0.20），从 {level} 档降到 {nxt} 档",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="阶梯压价迁移决策")
    parser.add_argument("--accounts", required=True, help="fetch_accounts.py 输出的 CSV")
    parser.add_argument("--strategies", required=True, help="fetch_strategies.py 输出的 JSON")
    args = parser.parse_args()

    try:
        accounts_path = Path(args.accounts)
        strategies_path = Path(args.strategies)
        if not accounts_path.exists():
            return _fail("FILE_NOT_FOUND", f"找不到账户文件 {accounts_path}")
        if not strategies_path.exists():
            return _fail("FILE_NOT_FOUND", f"找不到策略名单文件 {strategies_path}")

        accounts = _load_accounts(str(accounts_path))
        strategies_data = _load_strategies(str(strategies_path))
        account_to_level = strategies_data.get("account_to_level") or {}
        strategies = strategies_data.get("strategies") or {}
        missing = strategies_data.get("missing_levels") or []

        decisions = [evaluate(a, account_to_level, strategies) for a in accounts]

        # 汇总按档位分组，供写入步骤直接批量 add-ids / remove-ids
        adds: dict[str, list[str]] = {}
        removes: dict[str, list[str]] = {}
        counts: dict[str, int] = {}
        for d in decisions:
            action = d.get("action")
            counts[action] = counts.get(action, 0) + 1
            if action == ACT_ENTER:
                adds.setdefault(d["target_level"], []).append(d["account_id"])
            elif action == ACT_DOWN:
                removes.setdefault(d["current_level"], []).append(d["account_id"])
                adds.setdefault(d["target_level"], []).append(d["account_id"])
            elif action == ACT_REMOVE:
                removes.setdefault(d["current_level"], []).append(d["account_id"])

        result = {
            "decisions": decisions,
            "adds": adds,
            "removes": removes,
            "summary": counts,
            "missing_levels": missing,
        }

        _print_table(decisions)

        msg = f"决策完成：{len(decisions)} 个账户，{counts}"
        if missing:
            msg += f"；注意缺失档位 {missing}，相关决策需先建策略"
        return _ok(result, msg)
    except KeyboardInterrupt:
        return _fail("INTERRUPTED", "用户中断", code=130)
    except Exception as exc:  # noqa: BLE001
        print(f"[decide] {type(exc).__name__}: {exc}", file=sys.stderr)
        return _fail("UNEXPECTED_ERROR", f"{type(exc).__name__}: {exc}", code=1)


def _print_table(decisions: list[dict[str, Any]]) -> None:
    headers = ["账户ID", "超成本率", "消耗", "当前档", "动作", "目标档", "备注"]
    rows = [
        [
            d.get("account_id") or "",
            _fmt(d.get("rate")),
            _fmt(d.get("cost")),
            d.get("current_level") or "-",
            d.get("action") or "-",
            d.get("target_level") or "-",
            d.get("note") or "",
        ]
        for d in decisions
    ]

    def width(text: str) -> int:
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(text))

    widths = [max(width(h), *(width(r[i]) for r in rows)) if rows else width(h)
              for i, h in enumerate(headers)]

    def fmt(cells: list[str]) -> str:
        return "  ".join(str(c) + " " * (widths[i] - width(c)) for i, c in enumerate(cells))

    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))
    print()


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    return str(v)


if __name__ == "__main__":
    sys.exit(main())
