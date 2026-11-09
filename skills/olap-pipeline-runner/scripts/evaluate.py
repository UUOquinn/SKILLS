#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""按配置的档位表做决策。管道第 3 段，纯本地计算。

档位、护栏、冷却期全部来自配置。这个脚本不知道自己在算 ROI 还是别的什么。
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

LEDGER_HEADER = [
    "key", "written_at", "value", "strategy_name",
    "metric", "base_used", "window",
]


def _ok(data: dict[str, Any], msg: str = "", code: int = 0) -> int:
    print(json.dumps(
        {"ok": True, "code": code, "msg": msg, "data": data,
         "next_action": {"type": "CONTINUE", "retryable": False}},
        ensure_ascii=False,
    ))
    return code


def _fail(error: str, msg: str, code: int = 1, retryable: bool = False) -> int:
    print(json.dumps(
        {"ok": False, "code": code, "error": error, "msg": msg,
         "next_action": {"type": "STOP", "retryable": retryable}},
        ensure_ascii=False,
    ))
    return code


def validate_bands(bands: list[dict[str, Any]]) -> None:
    """档位区间是左闭右开 [min, max)，检查重叠和空隙。

    规则有洞比规则算错更难发现——某个值落不进任何档位时，如果不校验就会
    静默变成「不动」，看起来跟观察区一模一样。
    """
    if not bands:
        raise RuntimeError("配置 evaluate.bands 为空，没有档位就无法决策")

    spans = []
    for i, band in enumerate(bands):
        if "delta" not in band:
            raise RuntimeError(f"档位 #{i + 1} 缺少 delta 字段")
        lo = band.get("min")
        hi = band.get("max")
        lo = float("-inf") if lo is None else float(lo)
        hi = float("inf") if hi is None else float(hi)
        if lo >= hi:
            raise RuntimeError(f"档位 #{i + 1} 区间非法：min={lo} 必须小于 max={hi}")
        spans.append((lo, hi, i + 1))

    spans.sort()
    for (lo1, hi1, n1), (lo2, hi2, n2) in zip(spans, spans[1:]):
        if lo2 < hi1:
            raise RuntimeError(
                f"档位 #{n1} [{lo1}, {hi1}) 与 #{n2} [{lo2}, {hi2}) 重叠，"
                "同一个值会命中两档"
            )
        if lo2 > hi1:
            raise RuntimeError(
                f"档位 #{n1} [{lo1}, {hi1}) 与 #{n2} [{lo2}, {hi2}) 之间有空隙，"
                f"落在 [{hi1}, {lo2}) 的值无档位可用——先补全规则再跑"
            )


def match_band(value: Decimal, bands: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    v = float(value)
    for band in bands:
        lo = band.get("min")
        hi = band.get("max")
        lo = float("-inf") if lo is None else float(lo)
        hi = float("inf") if hi is None else float(hi)
        if lo <= v < hi:
            return band
    return None


def band_label(band: dict[str, Any]) -> str:
    lo, hi = band.get("min"), band.get("max")
    if lo is None:
        return f"value < {hi}"
    if hi is None:
        return f"value >= {lo}"
    return f"{lo} <= value < {hi}"


def load_ledger(path: Optional[str]) -> dict[str, datetime]:
    """读台账，返回每个主键最后一次写入时间。"""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    last: dict[str, datetime] = {}
    with p.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row.get("key") or "").strip()
            stamp = (row.get("written_at") or "").strip()
            if not key or not stamp:
                continue
            try:
                when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if key not in last or when > last[key]:
                last[key] = when
    return last


def evaluate_one(
    key: str, raw_value: str, current: str,
    bands: list[dict[str, Any]], guardrail: dict[str, Any],
    default_base: Optional[float], decimals: int,
    cooldown_minutes: int, last_written: Optional[datetime],
    now: datetime, key_pattern: Optional[str],
) -> dict[str, Any]:
    base_row = {
        "key": key, "value": raw_value, "band": "-", "delta": "0.00",
        "base": "-", "base_source": "-", "new_value": "-",
        "status": "OK", "note": "",
    }

    if key_pattern and not key.isdigit():
        # 汇总行、合计行这类非业务主键，早筛掉省下平台查询
        return {**base_row, "status": "SKIP_NOT_TARGET", "note": "非业务主键，跳过"}

    try:
        value = Decimal(str(raw_value).strip())
    except (InvalidOperation, AttributeError, TypeError):
        return {**base_row, "status": "SKIP_NO_DATA", "note": "指标为空或非数值，未操作"}

    band = match_band(value, bands)
    if band is None:
        return {
            **base_row, "status": "SKIP_NO_BAND",
            "note": f"值 {value} 落不进任何档位——规则有洞，先补全配置",
        }

    label = band_label(band)
    delta = Decimal(str(band["delta"]))

    if delta == 0:
        return {
            **base_row, "band": label, "status": "NO_ACTION",
            "note": f"落在观察区（{label}），保持原值",
        }

    # 冷却期在档位之后判：观察区本来就不动，不该被报成「冷却中」
    if cooldown_minutes > 0 and last_written is not None:
        elapsed = now - last_written
        if elapsed < timedelta(minutes=cooldown_minutes):
            mins = int(elapsed.total_seconds() // 60)
            return {
                **base_row, "band": label, "status": "SKIP_COOLDOWN",
                "note": f"距上次写入仅 {mins} 分钟（冷却期 {cooldown_minutes} 分钟），"
                        "本轮不重复下手",
            }

    current = (current or "").strip()
    if current:
        try:
            base = Decimal(current)
            source = "平台现值"
        except InvalidOperation:
            return {
                **base_row, "band": label, "status": "SKIP_NO_DATA",
                "note": f"平台现值「{current}」非数值，先核对第 2 段输出",
            }
    elif default_base is not None:
        base = Decimal(str(default_base))
        source = "新建默认值"
    else:
        return {
            **base_row, "band": label, "status": "SKIP_NO_DATA",
            "note": "查不到平台现值且未配置 enrich.default_value，无法计算",
        }

    quant = Decimal(1).scaleb(-decimals)
    new_value = (base + delta).quantize(quant)

    row = {
        **base_row, "band": label, "delta": f"{delta:+.2f}",
        "base": f"{base:.{decimals}f}", "base_source": source,
        "new_value": f"{new_value:.{decimals}f}",
    }

    lo = guardrail.get("min")
    hi = guardrail.get("max")
    if lo is not None and new_value < Decimal(str(lo)):
        return {**row, "status": "HOLD_NEEDS_CONFIRM",
                "note": f"新值 {new_value} 低于护栏下界 {lo}，待确认"}
    if hi is not None and new_value > Decimal(str(hi)):
        return {**row, "status": "HOLD_NEEDS_CONFIRM",
                "note": f"新值 {new_value} 超出护栏上界 {hi}，待确认"}
    return row


def print_table(rows: list[dict[str, Any]]) -> None:
    cols = ["key", "value", "band", "delta", "base", "base_source",
            "new_value", "status", "note"]
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols} \
        if rows else {c: len(c) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))


def main() -> int:
    parser = argparse.ArgumentParser(description="按配置档位做决策")
    parser.add_argument("--config", required=True, help="pipeline 配置 JSON")
    parser.add_argument("--input", required=True, help="第 1/2 段产出的 CSV")
    parser.add_argument("--ledger", default=None, help="台账 CSV，用于冷却期与中断恢复")
    parser.add_argument("--output", default=None, help="决策结果 JSON（可选）")
    parser.add_argument("--now", default=None, help="当前时间，用于复算历史轮次")
    parser.add_argument("--bands-only", action="store_true",
                        help="只按档位初判，不要求 current_value")
    args = parser.parse_args()

    try:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            return _fail("CONFIG_MISSING", f"找不到配置文件 {cfg_path}")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        ev = cfg.get("evaluate") or {}

        bands = ev.get("bands") or []
        validate_bands(bands)

        guardrail = ev.get("guardrail") or {}
        cooldown = int(ev.get("cooldown_minutes", 0))
        decimals = int(ev.get("decimals", 2))
        default_base = (cfg.get("enrich") or {}).get("default_value")
        key_pattern = ev.get("numeric_key_only", True)

        now = datetime.strptime(args.now, "%Y-%m-%d %H:%M:%S") if args.now \
            else datetime.now()
        ledger = load_ledger(args.ledger)

        in_path = Path(args.input)
        if not in_path.exists():
            return _fail("INPUT_MISSING", f"找不到输入文件 {in_path}")
        with in_path.open(encoding="utf-8") as f:
            src = list(csv.DictReader(f))
        if not src:
            return _fail("INPUT_EMPTY", f"{in_path} 没有数据行")

        seen: dict[str, int] = {}
        for row in src:
            k = (row.get("key") or "").strip()
            if k:
                seen[k] = seen.get(k, 0) + 1
        dups = [k for k, n in seen.items() if n > 1]
        if dups:
            return _fail(
                "DUPLICATE_KEY",
                f"主键重复：{dups[:5]}（共 {len(dups)} 个）。"
                "先按主键聚合再重跑，否则会给同一对象写两条",
                code=3,
            )

        decisions = [
            evaluate_one(
                (r.get("key") or "").strip(), r.get("value") or "",
                "" if args.bands_only else (r.get("current_value") or ""),
                bands, guardrail, default_base, decimals,
                cooldown, ledger.get((r.get("key") or "").strip()),
                now, "numeric" if key_pattern else None,
            )
            for r in src
        ]

        print_table(decisions)

        counts: dict[str, int] = {}
        for d in decisions:
            counts[d["status"]] = counts.get(d["status"], 0) + 1
        writable = counts.get("OK", 0)
        holds = counts.get("HOLD_NEEDS_CONFIRM", 0)
        no_band = counts.get("SKIP_NO_BAND", 0)

        print(f"\n合计 {len(decisions)} 个对象："
              + "，".join(f"{k} {v}" for k, v in sorted(counts.items())))
        print(f"需要写入的对象数：{writable}")

        if args.output:
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(decisions, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        if no_band:
            return _fail(
                "BAND_GAP",
                f"{no_band} 个对象落不进任何档位，规则有洞。先补全 evaluate.bands 再跑",
            )
        if holds:
            return _ok(
                {"decisions": decisions, "counts": counts, "writable": writable},
                f"{holds} 个对象落在护栏外（HOLD_NEEDS_CONFIRM），"
                "这些不要写入，单列等确认",
                code=2,
            )
        return _ok(
            {"decisions": decisions, "counts": counts, "writable": writable},
            f"决策完成，{writable} 个对象需要写入",
        )

    except Exception as exc:  # noqa: BLE001
        print(f"[evaluate] {type(exc).__name__}: {exc}", file=sys.stderr)
        return _fail("UNEXPECTED_ERROR", f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
