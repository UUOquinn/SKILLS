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
"""查询 4 条公用阶梯策略（AI_短剧_系数0.8/0.6/0.4/0.2）的当前名单。

    uv run scripts/fetch_strategies.py --output data/strategies_202609231700.json

「平台即状态」：这 4 条策略的 accountId 名单就是账本。账户在哪条策略里，
就处于哪一档系数。decide.py 据此判断每个账户是该进入、降档还是撤出，
不额外维护本地台账。

只查生效中（status=4）的策略：撤销/待审核的策略不代表当前生效意图，
不纳入判断。新建策略后要等审核生效（平台有自动审核，通常几分钟）再跑
循环，否则 fetch_strategies 看不到它、会误判为「缺失需新建」。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_STARTED_AT = time.perf_counter()

BASE_URL = "https://ops-platform.example.corp/ops/rest"
QUERY_PATH = f"{BASE_URL}/flowControl/query"

TYPE_CEILING_PRICE = 3
STATUS_EFFECTIVE = 4
INDUSTRY_VERSION = "6.6"
PAGE_SIZE = 100
MAX_PAGES = 50

# 阶梯档位：从高到低。名字格式固定为「AI_短剧_系数{level}」。
LEVELS = ("0.8", "0.6", "0.4", "0.2")
NAME_PREFIX = "AI_短剧_系数"


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


def split_account_field(raw: Any) -> list[str]:
    if not raw:
        return []
    text = str(raw)
    out: list[str] = []
    for chunk in text.replace("\n", ",").replace("，", ",").split(","):
        acc = chunk.strip()
        if acc and acc not in out:
            out.append(acc)
    return out


def level_from_name(name: str) -> str | None:
    if not name or not name.startswith(NAME_PREFIX):
        return None
    suffix = name[len(NAME_PREFIX):].strip()
    return suffix if suffix in LEVELS else None


def fetch_effective_strategies(session: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    total_page: int | None = None
    for page in range(1, MAX_PAGES + 1):
        body = {
            "pager": {"pageNum": page, "pageSize": PAGE_SIZE},
            "industryVersion": INDUSTRY_VERSION,
            "status": STATUS_EFFECTIVE,
        }
        resp = session.request("POST", QUERY_PATH, json=body, timeout=60)
        if resp.status_code in (401, 302):
            raise PermissionError("SSO_EXPIRED")
        if resp.status_code == 403:
            raise PermissionError("AUTH_FAILED")
        resp.raise_for_status()
        data = (resp.json().get("data") or {})
        batch = data.get("data") or []
        items.extend(batch)

        if total_page is None:
            raw_total_page = data.get("totalPage")
            raw_total_count = data.get("totalCount")
            if isinstance(raw_total_page, int) and raw_total_page > 0:
                total_page = raw_total_page
            elif isinstance(raw_total_count, int) and raw_total_count > 0:
                total_page = -(-raw_total_count // PAGE_SIZE)

        if not batch:
            break
        if total_page is not None and page >= total_page:
            break
        if total_page is None and len(batch) < PAGE_SIZE:
            break
    else:
        raise RuntimeError(f"分页超过 {MAX_PAGES} 页仍未结束，拒绝返回可能不完整的名单")
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="查询 4 条公用阶梯策略名单")
    parser.add_argument("--output", default=None, help="把完整结果写到该 JSON 文件")
    args = parser.parse_args()

    try:
        from ks_aimate.sso_login_client import SmartSSOSession

        session = SmartSSOSession()
        strategies = fetch_effective_strategies(session)

        strategies_by_level: dict[str, dict[str, Any]] = {}
        account_to_level: dict[str, str] = {}
        conflicts: dict[str, list[str]] = {}

        for item in strategies:
            if item.get("type") != TYPE_CEILING_PRICE:
                continue
            name = str(item.get("name") or "")
            level = level_from_name(name)
            if level is None:
                continue
            cluster = item.get("adCluster") or {}
            ids = split_account_field(cluster.get("accountId"))
            strategies_by_level[level] = {
                "strategy_id": item.get("id"),
                "name": name,
                "bonus_rate": item.get("bonusRate"),
                "account_ids": ids,
            }
            for acc in ids:
                if acc in account_to_level and account_to_level[acc] != level:
                    conflicts.setdefault(acc, []).append(account_to_level[acc])
                    conflicts[acc].append(level)
                else:
                    account_to_level[acc] = level

        missing = [lv for lv in LEVELS if lv not in strategies_by_level]

        result = {
            "strategies": strategies_by_level,
            "account_to_level": account_to_level,
            "missing_levels": missing,
            "scanned_strategies": len(strategies),
        }

        if args.output:
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            result["output"] = str(out)

        msg = f"扫描 {len(strategies)} 条生效策略，找到 {len(strategies_by_level)} 档阶梯策略"
        if missing:
            msg += f"；缺失档位：{missing}"
        if conflicts:
            msg += f"；{len(conflicts)} 个账户同时出现在多个档位（名单脏数据，需人工核对）"
        return _ok(result, msg)
    except PermissionError as exc:
        code = str(exc)
        hint = "内网 SSO 已失效，请重新完成认证后重试" if code == "SSO_EXPIRED" \
            else "当前账号无该接口权限"
        return _fail(code, hint, code=2)
    except KeyboardInterrupt:
        return _fail("INTERRUPTED", "用户中断", code=130)
    except Exception as exc:  # noqa: BLE001
        print(f"[fetch_strategies] {type(exc).__name__}: {exc}", file=sys.stderr)
        return _fail("UNEXPECTED_ERROR", f"{type(exc).__name__}: {exc}", code=1)


if __name__ == "__main__":
    sys.exit(main())
