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
"""批量拉取策略平台生效中的顶价策略，在本地建账户 -> 基数索引。

替代「逐账户查基数」：原来 N 个账户要 N 次请求（实测约 0.9 秒/次，50 个
账户就是 45 秒），现在分页拉全量生效策略，一次建索引，本地 O(1) 查。

    uv run scripts/fetch_bases.py --accounts 114368154,120729915
    uv run scripts/fetch_bases.py --accounts-from data/roi_202609151230.csv \
        --output data/bases_202609151230.json
    uv run scripts/fetch_bases.py --merge-csv data/roi_202609151230.csv \
        --output data/bases_202609151230.json

--merge-csv 模式从 CSV 读账户ID，并把查到的基数自动回填进该 CSV 的
current_coefficient 列（NO_BASE 清空），输出 <输入名>_with_base.csv 供
decide.py 直接消费——替代「手工把 base 填回列」的易错步骤。

输出 JSON 的 stdout 最后一行遵循 Response Contract：

    {"ok": true, "code": 0, "duration_ms": 0, "data": {...}, "next_action": {...}}

`data.bases` 是 `{账户ID: {...}}`。命中多条生效策略时**按生效时间（greyStartTime）取最新的一条**
作基数（`base_source=PLATFORM_LATEST`，`chosen_strategy_id` 记录用了哪条），
不再逐账户问用户：实测 139 个要动的账户里 51 个基数不唯一，逐个问会把常驼
循环卡死；最后下发的策略代表当前生效意图。查不到任何生效策略则为
`NO_BASE`，由 decide.py 按基数 1.0 新建。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from decide import OWNED_PREFIX, decide_write_mode  # noqa: E402
from typing import Any

BASE_URL = "https://ops.example.corp/ops/rest"
QUERY_PATH = f"{BASE_URL}/boostControl/query"

# type=3 是顶价（加系数）策略；status=4 是生效中
TYPE_CEILING_PRICE = 3
STATUS_EFFECTIVE = 4
# 只看广告主顶价：scope=1 仅 adCluster；scope=2 媒体维度、scope=3 双维度
# 的系数不是本流程要调的基数，混进来会把 base 取到别人的维度策略上
SCOPE_ADVERTISER = 1
INDUSTRY_VERSION = "6.6"
PAGE_SIZE = 100
MAX_PAGES = 50


def _ok(data: dict[str, Any], msg: str = "") -> int:
    print(json.dumps(
        {"ok": True, "code": 0, "duration_ms": 0, "msg": msg, "data": data,
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


def parse_account_ids(text: str) -> list[str]:
    out: list[str] = []
    for chunk in text.replace("\n", ",").replace(";", ",").replace("，", ",").split(","):
        acc = chunk.strip()
        if acc and acc not in out:
            out.append(acc)
    return out


def _account_from_row(row: dict[str, Any]) -> str | None:
    keys = ("account_id", "账户id", "账户ID", "accountid", "账户")
    lowered = {(k or "").strip().lower(): v for k, v in row.items()}
    for key in keys:
        val = lowered.get(key.lower())
        if val and str(val).strip():
            return str(val).strip()
    return None


def accounts_from_csv(path: Path) -> list[str]:
    out: list[str] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            acc = _account_from_row(row)
            if acc and acc not in out:
                out.append(acc)
    return out


MERGE_COLUMN = "current_coefficient"


def fill_bases_into_csv(path: Path, bases: dict[str, Any], out_path: Path) -> int:
    """把查到的基数回填进 CSV 的 current_coefficient 列，供 decide.py 直接消费。

    规则与 decide.py 对齐：有基数的账户填平台当前值；NO_BASE 的账户清空该列
    （decide 会按 default-base=1.0 新建），绝不能留上一轮的旧值；不在 bases 里
    的账户保持原值不动。返回实际回填行数。
    """
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        if MERGE_COLUMN not in fieldnames:
            fieldnames.append(MERGE_COLUMN)
        rows = [dict(row) for row in reader]
    updated = 0
    for row in rows:
        acc = _account_from_row(row)
        if acc is None:
            continue
        info = bases.get(acc)
        if info is None:
            continue
        row[MERGE_COLUMN] = "" if info.get("base") is None else str(info["base"])
        updated += 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return updated


def split_account_field(raw: Any) -> list[str]:
    """把 adCluster.accountId 拆成精确的 ID 列表。

    必须精确切分再比较：直接用字符串 `in` 判断会让 114368154 误命中
    1143681546。
    """
    if not raw:
        return []
    text = str(raw)
    return [x.strip() for x in text.replace("\n", ",").replace("，", ",").split(",") if x.strip()]


def fetch_strategies(
    session: Any, status: int | None = None, name: str | None = None
) -> list[dict[str, Any]]:
    """分页拉策略，可按 status 或 name 过滤。

    分页只能信 `totalPage` / `totalCount`：实测该接口第 1 页就返回
    `hasMore=false`，但 `totalPage=5`。拿 hasMore 停条会只拉到首页 100 条，
    后面的策略全部静默丢失——账户本来有基数却被当成无基数，直接
    拉错系数。
    """
    items: list[dict[str, Any]] = []
    total_page: int | None = None
    for page in range(1, MAX_PAGES + 1):
        body: dict[str, Any] = {
            "pager": {"pageNum": page, "pageSize": PAGE_SIZE},
            "industryVersion": INDUSTRY_VERSION,
        }
        if status is not None:
            body["status"] = status
        if name is not None:
            body["name"] = name
        resp = session.request("POST", QUERY_PATH, json=body, timeout=60)
        if resp.status_code in (401, 302):
            raise PermissionError("AUTH_EXPIRED")
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
            # 拿不到总页数时的保底：未满页即末页
            break
    else:
        raise RuntimeError(
            f"分页超过 {MAX_PAGES} 页仍未结束，拒绡返回可能不完整的基数索引"
        )
    return items


def _recency_key(entry: dict[str, Any]) -> tuple[int, int, int, int]:
    """排序键：越新越大。

    优先 `greyStartTime`（策略生效时间，口径「最新生效时间」），其次
    `createTime`（新建时间），再次 `updateTime`，最后自增主键兜底。
    """
    def _num(val: Any) -> int:
        return val if isinstance(val, int) else 0

    return (
        _num(entry.get("grey_start_time")),
        _num(entry.get("create_time")),
        _num(entry.get("update_time")),
        _num(entry.get("strategy_id")),
    )


def build_index(strategies: list[dict[str, Any]], accounts: list[str]) -> dict[str, Any]:
    wanted = set(accounts)
    owned_hits: dict[str, list[dict[str, Any]]] = {acc: [] for acc in accounts}
    other_hits: dict[str, list[dict[str, Any]]] = {acc: [] for acc in accounts}
    for item in strategies:
        if item.get("type") != TYPE_CEILING_PRICE:
            continue
        if item.get("scope") != SCOPE_ADVERTISER:
            continue
        cluster = item.get("adCluster") or {}
        ids = split_account_field(cluster.get("accountId"))
        matched = wanted.intersection(ids)
        if not matched:
            continue
        entry = {
            "strategy_id": item.get("id"),
            "name": item.get("name"),
            "coefficient": item.get("bonusRate"),
            "status": item.get("status"),
            "account_count": len(ids),
            "account_ids": ids,
            "create_time": item.get("createTime"),
            "update_time": item.get("updateTime"),
            "grey_start_time": item.get("greyStartTime"),
        }
        is_owned = str(item.get("name") or "").startswith(OWNED_PREFIX)
        bucket = owned_hits if is_owned else other_hits
        for acc in matched:
            bucket[acc].append(entry)

    bases: dict[str, Any] = {}
    resolved_latest: list[str] = []
    recovered_pending: list[str] = []
    for acc in accounts:
        # 本流程建的策略优先，无论状态（生效/待提交/待审核）。这样 CREATE 后
        # 未提审、或提审后尚未被定时工作台生效的策略，下一轮也会被认到并原地改，
        # 不会重复 CREATE 撞「名称已存在」。
        owned = sorted(owned_hits[acc], key=_recency_key, reverse=True)
        solo_owned = [e for e in owned if not any(x != acc for x in e.get("account_ids") or [])]
        if solo_owned:
            e = solo_owned[0]
            is_effective = e.get("status") == STATUS_EFFECTIVE
            if is_effective:
                base_source = "PLATFORM_UNIQUE" if len(solo_owned) == 1 else "PLATFORM_LATEST"
                if len(solo_owned) > 1:
                    resolved_latest.append(acc)
            else:
                base_source = "PLATFORM_PENDING"
                recovered_pending.append(acc)
            bases[acc] = {
                "base": e["coefficient"],
                "base_source": base_source,
                "chosen_strategy_id": e["strategy_id"],
                "target_strategy_id": e["strategy_id"],
                "candidates": solo_owned,
                "needs_user_choice": False,
                "write_mode": "UPDATE",
                "write_reason": "本流程建的策略（只含本账户），原地改系数",
                "other_account_count": 0,
                "other_accounts_sample": [],
            }
            continue

        # 没有本流程的、只含本账户的策略。按时间倒序看别人建的生效策略——
        # 实测一个账户被多条生效策略覆盖是常态，取最新的一条作基数，
        # 最后下发的策略代表当前生效意图。这里通常只能 CREATE（不碰别人的）。
        others = sorted(other_hits[acc], key=_recency_key, reverse=True)
        if others:
            if len(others) == 1:
                bases[acc] = {
                    "base": others[0]["coefficient"], "base_source": "PLATFORM_UNIQUE",
                    "candidates": others, "needs_user_choice": False,
                    "chosen_strategy_id": others[0]["strategy_id"],
                    **decide_write_mode(others[0], acc),
                }
            else:
                resolved_latest.append(acc)
                bases[acc] = {
                    "base": others[0]["coefficient"],
                    "base_source": "PLATFORM_LATEST",
                    "chosen_strategy_id": others[0]["strategy_id"],
                    "candidates": others,
                    "needs_user_choice": False,
                    **decide_write_mode(others[0], acc),
                }
        else:
            bases[acc] = {
                "base": None, "base_source": "NO_BASE",
                "candidates": [], "needs_user_choice": False,
                "write_mode": "CREATE",
                "write_reason": "该账户无生效策略、也无本流程建的待提交/待审核策略，按默认基数新建",
            }

    update_accounts = [a for a, b in bases.items() if b.get("write_mode") == "UPDATE"]

    return {
        "bases": bases,
        "conflict_accounts": [],
        "resolved_by_latest": resolved_latest,
        "recovered_pending": recovered_pending,
        "update_accounts": update_accounts,
        "create_accounts": [a for a in accounts if a not in update_accounts],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="批量拉取账户当前 CPM 顶价基数")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--accounts", help="账户ID，逗号分隔")
    group.add_argument("--accounts-from", help="从 CSV 读账户ID（取 account_id 列）")
    group.add_argument(
        "--merge-csv",
        help="从该 CSV 读账户ID，并把查到的基数回填进 current_coefficient 列，"
        "输出 *_with_base.csv 供 decide.py 直接消费（一条命令替代手工回填）",
    )
    parser.add_argument(
        "--merge-output",
        default=None,
        help="--merge-csv 的输出文件路径，默认 <输入名>_with_base.csv",
    )
    parser.add_argument("--output", default=None, help="把完整结果写到该 JSON 文件")
    args = parser.parse_args()

    try:
        if args.accounts:
            accounts = parse_account_ids(args.accounts)
        elif args.merge_csv:
            path = Path(args.merge_csv)
            if not path.exists():
                return _fail("INPUT_NOT_FOUND", f"找不到输入文件 {path}", code=1)
            accounts = accounts_from_csv(path)
        else:
            path = Path(args.accounts_from)
            if not path.exists():
                return _fail("INPUT_NOT_FOUND", f"找不到输入文件 {path}", code=1)
            accounts = accounts_from_csv(path)
        if not accounts:
            return _fail("INVALID_ARGUMENT", "没有解析到任何账户ID", code=1)

        from auth_helper.sso_login_client import SmartAuthSession

        session = SmartAuthSession()
        effective = fetch_strategies(session, status=STATUS_EFFECTIVE)
        # 兜底：按名字前缀拉本流程建的策略（含待提交/待审核），识别 CREATE 后未提审的。
        # 不能按 status=0 全量拉：平台有 7000+ 条待提交策略，分页超限；用 name 前缀直接命中。
        owned = fetch_strategies(session, name=OWNED_PREFIX)
        seen = {s.get("id") for s in effective}
        strategies = effective + [
            s for s in owned
            if s.get("id") not in seen and s.get("status") != STATUS_EFFECTIVE
        ]
        result = build_index(strategies, accounts)
        result["requested_accounts"] = len(accounts)
        result["scanned_strategies"] = len(strategies)

        if args.output:
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            result["output"] = str(out)

        if args.merge_csv:
            csv_path = Path(args.merge_csv)
            out_csv = Path(args.merge_output) if args.merge_output else \
                csv_path.with_name(csv_path.stem + "_with_base.csv")
            filled = fill_bases_into_csv(csv_path, result["bases"], out_csv)
            result["merged_csv"] = str(out_csv)
            result["merged_accounts"] = filled

        msg = f"扫描 {len(strategies)} 条策略（生效/待提交/待审核），覆盖请求的 {len(accounts)} 个账户"
        if result["resolved_by_latest"]:
            msg += (
                f"；{len(result['resolved_by_latest'])} 个账户命中多条生效策略，"
                "已按最新策略取基数"
            )
        if result["recovered_pending"]:
            msg += f"；{len(result['recovered_pending'])} 个账户认回待提交/待审核策略，将原地改并提审"
        n_update = len(result["update_accounts"])
        msg += f"；{n_update} 个可原地改系数，{len(accounts) - n_update} 个需新建"
        if args.merge_csv:
            msg += f"；已回填 {filled} 行基数到 {out_csv.name}"
        return _ok(result, msg)
    except PermissionError as exc:
        code = str(exc)
        hint = "内网 Auth 已失效，请重新完成认证后重试" if code == "AUTH_EXPIRED" \
            else "当前账号无该接口权限"
        return _fail(code, hint, code=2)
    except KeyboardInterrupt:
        return _fail("INTERRUPTED", "用户中断", code=130)
    except Exception as exc:  # noqa: BLE001
        print(f"[fetch_bases] {type(exc).__name__}: {exc}", file=sys.stderr)
        return _fail("UNEXPECTED_ERROR", f"{type(exc).__name__}: {exc}", code=1)


if __name__ == "__main__":
    sys.exit(main())
