#!/usr/bin/env -S uv run --script
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
"""单条写入策略平台并记台账。管道第 4 段。

**单条**是刻意的：写策略是高影响生产操作，逐条落台账才能保证中断可恢复、
不重复写。要写 N 条就调用 N 次，不提供脚本内循环或并发。
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE_URL = "https://ops-platform.example.corp/ops/rest"
CREATE_PATH = f"{BASE_URL}/flowControl/mergeEditV2"
TZ = timezone(timedelta(hours=8))

LEDGER_HEADER = [
    "key", "written_at", "value", "strategy_name",
    "metric", "base_used", "window",
]


def _ok(data: dict[str, Any], msg: str = "") -> int:
    print(json.dumps(
        {"ok": True, "code": 0, "msg": msg, "data": data,
         "next_action": {"type": "CONTINUE", "retryable": False}},
        ensure_ascii=False,
    ))
    return 0


def _fail(error: str, msg: str, code: int = 1, retryable: bool = False) -> int:
    print(json.dumps(
        {"ok": False, "code": code, "error": error, "msg": msg,
         "next_action": {"type": "STOP", "retryable": retryable}},
        ensure_ascii=False,
    ))
    return code


def resolve_end_time(spec: Any) -> int:
    """算 endTime 毫秒时间戳。

    end_of_day 是最常用的：自动调整属于当日级干预，过了今天就该失效重新评估，
    不该留着跨天生效。
    """
    now = datetime.now(TZ)
    if spec in (None, "end_of_day"):
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        return int(end.timestamp() * 1000)
    if isinstance(spec, str) and spec.startswith("+"):
        # 形如 "+3d" / "+12h"
        unit = spec[-1]
        amount = int(spec[1:-1])
        delta = {"d": timedelta(days=amount), "h": timedelta(hours=amount)}.get(unit)
        if delta is None:
            raise RuntimeError(f"无法解析 write.end_time「{spec}」，支持 +Nd / +Nh")
        return int((now + delta).timestamp() * 1000)
    if isinstance(spec, (int, float)):
        return int(spec)
    raise RuntimeError(
        f"无法解析 write.end_time「{spec}」，支持 end_of_day / +Nd / +Nh / 毫秒时间戳"
    )


def check_name(name: str, limit: int) -> None:
    """中文按字符计，不是字节。超限平台直接拒。"""
    if len(name) > limit:
        raise RuntimeError(
            f"策略名 {len(name)} 字符超过上限 {limit}：{name}。"
            "压缩命名模板，平台会返回 size must be between 0 and N"
        )


def build_body(
    cfg: dict[str, Any], key: str, value: str, name: str,
    background: str, end_time_ms: int,
) -> dict[str, Any]:
    """按配置的字段映射拼请求体。

    不同策略类型字段名不同，所以走配置而不是写死。
    """
    write_cfg = cfg.get("write") or {}
    fields = write_cfg.get("fields") or {}

    body: dict[str, Any] = {
        fields.get("name", "name"): name,
        fields.get("background", "background"): background,
        fields.get("value", "bonusRate"): float(value),
        fields.get("id_list", "accountIds"): key,
        fields.get("begin_time", "beginTime"): int(
            datetime.now(TZ).timestamp() * 1000
        ),
        fields.get("end_time", "endTime"): end_time_ms,
    }
    for k, v in (write_cfg.get("constants") or {}).items():
        body[k] = v
    return body


def append_ledger(
    path: Path, key: str, value: str, name: str,
    metric: str, base_used: str, window: str,
) -> None:
    """追加一行台账。旧文件缺列时自动升级表头，旧行补空。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing = list(reader.fieldnames or [])
            rows = [dict(r) for r in reader]
        if existing != LEDGER_HEADER:
            for r in rows:
                for col in LEDGER_HEADER:
                    r.setdefault(col, "")

    rows.append({
        "key": key,
        "written_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "value": value, "strategy_name": name,
        "metric": metric, "base_used": base_used, "window": window,
    })

    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_HEADER)
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in LEDGER_HEADER})


def main() -> int:
    parser = argparse.ArgumentParser(description="单条写入策略平台并记台账")
    parser.add_argument("--config", required=True, help="pipeline 配置 JSON")
    parser.add_argument("--target", required=True, help="业务主键（账户ID 等）")
    parser.add_argument("--value", required=True, help="要写入的新值")
    parser.add_argument("--name", required=True, help="策略名")
    parser.add_argument("--ledger", required=True, help="台账 CSV")
    parser.add_argument("--base", default="", help="本次基于的基数，记台账用")
    parser.add_argument("--metric", default="", help="本次的指标值，记台账用")
    parser.add_argument("--window", default="", help="取数窗口，记台账用")
    parser.add_argument("--background", default="", help="策略备注，默认取配置模板")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只打印将提交的 body，不真写。第一次跑务必先用它确认字段",
    )
    args = parser.parse_args()

    try:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            return _fail("CONFIG_MISSING", f"找不到配置文件 {cfg_path}")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        write_cfg = cfg.get("write") or {}

        limit = int(write_cfg.get("name_max_length", 32))
        check_name(args.name, limit)

        background = args.background or write_cfg.get("background", "")[:limit]
        if background:
            check_name(background, limit)

        end_ms = resolve_end_time(write_cfg.get("end_time", "end_of_day"))
        body = build_body(
            cfg, args.target, args.value, args.name, background, end_ms,
        )

        if args.dry_run:
            print(json.dumps(body, ensure_ascii=False, indent=2))
            return _ok(
                {"dry_run": True, "body": body,
                 "end_time": datetime.fromtimestamp(end_ms / 1000, TZ).isoformat()},
                "dry-run：未真实写入。确认字段无误后去掉 --dry-run",
            )

        from ks_aimate.sso_login_client import SmartSSOSession

        session = SmartSSOSession()
        resp = session.request("POST", CREATE_PATH, json=body, timeout=120)
        if resp.status_code in (401, 302):
            return _fail("SSO_EXPIRED", "内网 SSO 已失效，请重新认证后重试", code=2)
        if resp.status_code == 403:
            return _fail("AUTH_FAILED", "无权在策略平台写入，确认账号权限", code=2)
        resp.raise_for_status()

        payload = resp.json()
        if payload.get("result") not in (1, 0, None) and payload.get("error_msg"):
            # 写失败不记台账——记了下一轮会当成已处理而跳过
            return _fail(
                "WRITE_REJECTED",
                f"平台拒绝写入：{payload.get('error_msg')}。"
                "该对象未记台账，下一轮会重试",
            )

        strategy_id = (payload.get("data") or {}).get("id") if isinstance(
            payload.get("data"), dict
        ) else payload.get("data")

        append_ledger(
            Path(args.ledger), args.target, args.value, args.name,
            args.metric, args.base, args.window,
        )

        return _ok(
            {
                "target": args.target, "value": args.value,
                "strategy_id": strategy_id, "strategy_name": args.name,
                "end_time": datetime.fromtimestamp(end_ms / 1000, TZ).isoformat(),
                "ledger": args.ledger,
            },
            f"已写入 {args.target} 新值 {args.value}"
            + (f"，平台策略 {strategy_id}" if strategy_id else "")
            + "，台账已记",
        )

    except Exception as exc:  # noqa: BLE001
        print(f"[write] {type(exc).__name__}: {exc}", file=sys.stderr)
        return _fail("UNEXPECTED_ERROR", f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
