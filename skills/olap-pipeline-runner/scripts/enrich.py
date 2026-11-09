#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests>=2.31.0,<3", "ks-aimate>=1.0.30"]
# [tool.uv.sources]
# "ks-aimate" = { index = "kuaishou" }
# [[tool.uv.index]]
# name = "kuaishou"
# url = "https://app.example.corp/kuaishou/prod/+simple/"
# publish = false
# ///
"""拉平台生效策略在本地建「主键 → 当前值」索引。管道第 2 段。

全量拉一次在本地建索引，比逐个查快一个数量级（实测 1.7 秒扫 474 条，
逐个查约 0.9 秒/个）。
"""

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

BASE_URL = "https://ops.example.corp/ops/rest"
QUERY_PATH = f"{BASE_URL}/boostControl/query"
PAGE_SIZE = 100
MAX_PAGES = 50


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


def keys_from_csv(path: Path) -> list[str]:
    """从第 1 段的输出里读主键。"""
    if not path.exists():
        raise RuntimeError(f"找不到输入文件 {path}")
    keys = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row.get("key") or "").strip()
            if key:
                keys.append(key)
    if not keys:
        raise RuntimeError(f"{path} 里没有任何主键，先确认第 1 段是否正常产出")
    return keys


def dig(item: dict[str, Any], path: str) -> Any:
    """按点号路径取嵌套字段，如 `adCluster.accountId`。

    平台把名单埋在子对象里，按顶层字段取会全拿到 None，而且不报错——
    索引建出来是空的，所有对象都被当成 NO_VALUE，直接拿默认基数覆盖真实现值。
    """
    cur: Any = item
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def split_id_field(raw: Any) -> list[str]:
    """按逗号 / 空格 / 换行拆名单字段。

    必须精确匹配，不能用字符串包含——否则 `114368154` 会误命中
    `1143681546`，把别人的策略当成自己的基数。
    """
    if raw is None:
        return []
    text = str(raw)
    for sep in (",", "\n", "\t", ";", "，"):
        text = text.replace(sep, " ")
    return [part.strip() for part in text.split(" ") if part.strip()]


def fetch_effective(
    session: Any, id_field: str, value_field: str,
    status: int, industry_version: str,
) -> list[dict[str, Any]]:
    """分页拉全量生效中的策略。

    分页只能信 `totalPage` / `totalCount`：实测该接口第 1 页就返回
    `hasMore=false`，但 `totalPage=5`。拿 `hasMore` 当停条会只拉到首页，后面的
    策略全部静默丢失——对象本来有现值却被当成没有，直接拿错基数。
    """
    out: list[dict[str, Any]] = []
    raw_items: list[dict[str, Any]] = []
    total_page: int | None = None

    for page in range(1, MAX_PAGES + 1):
        resp = session.request(
            "POST", QUERY_PATH,
            json={
                "pager": {"pageNum": page, "pageSize": PAGE_SIZE},
                "industryVersion": industry_version,
                "status": status,
            },
            timeout=120,
        )
        if resp.status_code in (401, 302):
            raise PermissionError("AUTH_EXPIRED")
        if resp.status_code == 403:
            raise PermissionError("AUTH_FAILED")
        resp.raise_for_status()

        data = resp.json().get("data") or {}
        batch = data.get("data") or []
        raw_items.extend(batch)

        for item in batch:
            ids = split_id_field(dig(item, id_field))
            if not ids:
                continue
            out.append({
                "strategy_id": item.get("id"),
                "name": item.get("name"),
                "value": dig(item, value_field),
                "ids": ids,
                "id_count": len(ids),
                "create_time": item.get("createTime") or 0,
                "update_time": item.get("updateTime") or 0,
            })

        if total_page is None:
            raw_page = data.get("totalPage")
            raw_count = data.get("totalCount")
            if isinstance(raw_page, int) and raw_page > 0:
                total_page = raw_page
            elif isinstance(raw_count, int) and raw_count > 0:
                total_page = -(-raw_count // PAGE_SIZE)

        if not batch:
            break
        if total_page is not None and page >= total_page:
            break
        if total_page is None and len(batch) < PAGE_SIZE:
            # 拿不到总页数时的保底：未满页即末页
            break
    else:
        raise RuntimeError(
            f"分页超过 {MAX_PAGES} 页仍未结束，拒绝返回可能不完整的索引"
        )

    if raw_items and not out:
        # 拉到了策略却一条名单都没解析出来，几乎肯定是 id_field 路径写错。
        # 不报错的后果是索引全空、所有对象被当成 NO_VALUE，然后拿默认基数
        # 去覆盖平台上真实存在的现值——静默写错比直接失败危险得多。
        sample = sorted(raw_items[0].keys())
        raise RuntimeError(
            f"拉到 {len(raw_items)} 条策略，但按 id_field=「{id_field}」一个名单都没解析出来。"
            f"首条策略的顶层字段：{sample}。"
            "先核对 enrich.id_field 路径（支持点号嵌套，如 adCluster.accountId）"
        )
    return out


def _recency_key(entry: dict[str, Any]) -> tuple[int, int, int]:
    """新→旧排序依据：createTime → updateTime → 策略 ID（自增主键兜底）。"""
    return (
        int(entry.get("create_time") or 0),
        int(entry.get("update_time") or 0),
        int(entry.get("strategy_id") or 0),
    )


def build_index(
    strategies: list[dict[str, Any]], keys: list[str], policy: str,
) -> dict[str, Any]:
    """建「主键 → 当前值」索引，按 multi_hit_policy 处理多命中。"""
    hits: dict[str, list[dict[str, Any]]] = {k: [] for k in keys}
    wanted = set(keys)
    for st in strategies:
        for one in st["ids"]:
            if one in wanted:
                hits[one].append(st)

    resolved: dict[str, Any] = {}
    conflicts: list[str] = []
    by_latest: list[str] = []

    for key in keys:
        found = sorted(hits[key], key=_recency_key, reverse=True)
        candidates = [
            {
                "strategy_id": s["strategy_id"], "name": s["name"],
                "value": s["value"], "id_count": s["id_count"],
                "create_time": s["create_time"], "update_time": s["update_time"],
            }
            for s in found
        ]

        if not found:
            resolved[key] = {
                "current_value": None, "source": "NO_VALUE",
                "candidates": [], "needs_user_choice": False,
            }
        elif len(found) == 1:
            resolved[key] = {
                "current_value": found[0]["value"], "source": "PLATFORM_UNIQUE",
                "chosen_strategy_id": found[0]["strategy_id"],
                "candidates": candidates, "needs_user_choice": False,
            }
        elif policy == "latest":
            resolved[key] = {
                "current_value": found[0]["value"], "source": "PLATFORM_LATEST",
                "chosen_strategy_id": found[0]["strategy_id"],
                "candidates": candidates, "needs_user_choice": False,
            }
            by_latest.append(key)
        elif policy == "fail":
            raise RuntimeError(
                f"主键 {key} 命中 {len(found)} 条生效策略，multi_hit_policy=fail 要求人工处理。"
                f"候选：{[c['strategy_id'] for c in candidates]}"
            )
        else:  # ask
            resolved[key] = {
                "current_value": None, "source": "MULTIPLE_EFFECTIVE",
                "candidates": candidates, "needs_user_choice": True,
            }
            conflicts.append(key)

    return {
        "values": resolved, "conflict_keys": conflicts,
        "resolved_by_latest": by_latest,
    }


def backfill_csv(path: Path, values: dict[str, Any]) -> int:
    """把当前值回填到第 1 段 CSV 的 current_value 列。"""
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fields = list(rows[0].keys()) if rows else []
    if "current_value" not in fields:
        raise RuntimeError(f"{path} 缺少 current_value 列，先确认第 1 段输出格式")

    filled = 0
    for row in rows:
        info = values.get((row.get("key") or "").strip())
        if info and info.get("current_value") is not None:
            row["current_value"] = info["current_value"]
            filled += 1

    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return filled


def main() -> int:
    parser = argparse.ArgumentParser(description="拉平台现值建索引并回填 CSV")
    parser.add_argument("--config", required=True, help="pipeline 配置 JSON")
    parser.add_argument("--input", required=True, help="第 1 段输出的 CSV")
    parser.add_argument("--output", default=None, help="索引明细 JSON（可选）")
    parser.add_argument(
        "--no-backfill", action="store_true",
        help="只产出索引，不回填 CSV 的 current_value 列",
    )
    args = parser.parse_args()

    try:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            return _fail("CONFIG_MISSING", f"找不到配置文件 {cfg_path}")
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        enrich_cfg = cfg.get("enrich") or {}

        policy = enrich_cfg.get("multi_hit_policy", "latest")
        if policy not in ("latest", "ask", "fail"):
            return _fail(
                "CONFIG_INVALID",
                f"enrich.multi_hit_policy 只能是 latest / ask / fail，收到「{policy}」",
            )
        id_field = enrich_cfg.get("id_field", "adCluster.accountId")
        value_field = enrich_cfg.get("value_field", "bonusRate")
        status = int(enrich_cfg.get("status", 4))
        industry_version = str(enrich_cfg.get("industry_version", "6.6"))

        keys = keys_from_csv(Path(args.input))

        from auth_helper.sso_login_client import SmartAuthSession

        session = SmartAuthSession()
        strategies = fetch_effective(
            session, id_field, value_field, status, industry_version,
        )
        index = build_index(strategies, keys, policy)

        if args.output:
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        filled = 0
        if not args.no_backfill:
            filled = backfill_csv(Path(args.input), index["values"])

        multi = len(index["resolved_by_latest"])
        conflict = len(index["conflict_keys"])
        parts = [f"扫描 {len(strategies)} 条生效策略，覆盖请求的 {len(keys)} 个主键"]
        if multi:
            parts.append(f"{multi} 个命中多条，已按最新取值")
        if conflict:
            parts.append(f"{conflict} 个待人工选择")
        if filled:
            parts.append(f"回填 {filled} 行")

        return _ok(
            {
                "values": index["values"],
                "conflict_keys": index["conflict_keys"],
                "resolved_by_latest": index["resolved_by_latest"],
                "requested_keys": len(keys),
                "scanned_strategies": len(strategies),
                "backfilled_rows": filled,
                "multi_hit_policy": policy,
                "output": args.output,
            },
            "；".join(parts),
        )

    except PermissionError as exc:
        code = str(exc)
        if code == "AUTH_EXPIRED":
            return _fail("AUTH_EXPIRED", "内网 Auth 已失效，请重新完成认证后重试", code=2)
        return _fail("AUTH_FAILED", "无权访问策略平台，确认账号权限", code=2)
    except Exception as exc:  # noqa: BLE001
        print(f"[enrich] {type(exc).__name__}: {exc}", file=sys.stderr)
        return _fail("UNEXPECTED_ERROR", f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
