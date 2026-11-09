#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests>=2.31.0,<3",
#   "ks-aimate>=1.0.30",
# ]
#
# [tool.uv.sources]
# "ks-aimate" = { index = "kuaishou" }
#
# [[tool.uv.index]]
# name = "kuaishou"
# url = "https://internal.example.corp/kuaishou/prod/+simple/"
# publish = false
# ///
"""
Operation (ops-platform) REST API client.

Uses SmartSSOSession for transparent SSO auth — no manual cookie juggling.
Supports: get (with prefix detection), query, mergeEdit/V2, quickPushAll, withdraw,
approve query/changeStatus, listUser.

Lightweight batch: --ids for multiple get queries.

Usage:
  uv run scripts/operation_client.py get --id 12345
  uv run scripts/operation_client.py get --ids 12345,67890
  uv run scripts/operation_client.py query --app-id 570400002
  uv run scripts/operation_client.py merge-edit --prefix orientControl --file body.json
  uv run scripts/operation_client.py quick-push --prefix orientControl --id 12345 --type 0
  uv run scripts/operation_client.py list-user
"""

import argparse
import json
import sys
import time
from typing import Optional

from ks_aimate.sso_login_client import SmartSSOSession

BASE = "https://ops-platform.example.corp/ops/rest"

# Detection order: try each prefix via get, first non-empty-shell wins
PREFIX_ORDER = [
    "orientControl",
    "flowControl",
    "darkControl",
    "mediaControl",
    "innerControl",
    "shareRatioControl",
]

# Shield filter dimensions: normalized input -> query/mediaCluster field
DIM_ALIAS = {"appid": "appId", "uid": "uid", "posid": "posId"}
DIM_MAP = {"appId": "appId", "uid": "uid", "posId": "posId"}

# Shield industry fields in adCluster (searched in order)
INDUSTRY_FIELDS = [
    "secondIndustryV6", "firstIndustryV6", "firstIndustry",
    "industryV6", "industry", "shieldIndustry",
]

# Shield product fields in adCluster (searched in order)
PRODUCT_FIELDS = ["defineProduct", "product", "shieldProduct", "shieldProductName"]

# Non-shield types (定投 / 人群 / 预算), no mediaCluster; skip in search
NON_SHIELD_TYPES = {1, 3, 6, 8, 9}
SHIELD_TYPE_LABELS = {2: "媒体屏蔽（老版）", 5: "标签屏蔽", 7: "并集屏蔽（新屏蔽）", 10: "交集屏蔽"}


class OperationClient:
    def __init__(self):
        # SmartSSOSession handles SSO login / token refresh transparently.
        # Do NOT pass cookies manually; the SDK manages them internally.
        self.session = SmartSSOSession()

    # ── low-level helpers ──────────────────────────────────────────

    def _get(self, url: str, params: dict = None) -> dict:
        resp = self.session.request("GET", url, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, url: str, body: dict = None, **kwargs) -> dict:
        resp = self.session.request("POST", url, json=body, **kwargs)
        resp.raise_for_status()
        return resp.json()

    # ── prefix detection ───────────────────────────────────────────

    def detect_prefix(self, strategy_id: str) -> Optional[str]:
        """Try each prefix in order; return the first with non-empty data."""
        for prefix in PREFIX_ORDER:
            try:
                data = self._get(f"{BASE}/{prefix}/get", {"id": strategy_id})
            except Exception:
                continue
            inner = data.get("data")
            if not inner or not isinstance(inner, dict):
                continue
            if inner.get("id") or inner.get("ruleId") or inner.get("name"):
                return prefix
        return None

    # ── public API ─────────────────────────────────────────────────

    def get(self, strategy_id: str, prefix: str = None) -> dict:
        """GET /{prefix}/get?id= — fetch strategy detail. Auto-detect prefix if omitted."""
        if prefix:
            return self._get(f"{BASE}/{prefix}/get", {"id": strategy_id})
        # auto-detect
        detected = self.detect_prefix(strategy_id)
        if not detected:
            return {"ok": False, "code": 1, "error": "PREFIX_NOT_FOUND",
                    "msg": f"策略 {strategy_id} 未命中任何已知前缀"}
        result = self._get(f"{BASE}/{detected}/get", {"id": strategy_id})
        result["_prefix"] = detected
        return result

    def get_batch(self, ids: list, prefix: str = None) -> list:
        """Fetch multiple strategies. Returns list of {id, _prefix, data}."""
        results = []
        for sid in ids:
            try:
                r = self.get(sid, prefix=prefix)
                results.append({"id": sid, "_prefix": r.get("_prefix"), "data": r.get("data")})
            except Exception as e:
                results.append({"id": sid, "_prefix": None, "error": str(e)})
        return results

    def query(self, body: dict) -> dict:
        """POST /orientControl/query — list strategies."""
        return self._post(f"{BASE}/orientControl/query", body)

    # ── shield search: find strategies covering a media/app id ──────

    def _extract_dim_values(self, cluster: dict, dim: str) -> set:
        """Extract distinct values of `dim` from a mediaCluster (comma/; separated)."""
        raw = cluster.get(dim) or ""
        parts = [p.strip() for p in str(raw).replace(";", ",").split(",") if p.strip()]
        return set(parts)

    def _extract_shield_dim(self, item: dict) -> Optional[str]:
        """Return the shield dimension (appId/uid/posId) of a strategy, or None."""
        shield_media_type = (item.get("shieldMediaType") or "").strip()
        if shield_media_type in DIM_MAP:
            return shield_media_type
        mc = item.get("mediaCluster") or {}
        present = [d for d in DIM_MAP if mc.get(d)]
        return present[0] if len(present) == 1 else None

    def _extract_industry_product(self, item: dict) -> dict:
        """Extract industry codes and product names from a shield strategy."""
        ac = item.get("adCluster") or {}
        industries, products = set(), set()
        for f in INDUSTRY_FIELDS:
            raw = ac.get(f)
            if raw:
                for p in str(raw).replace(";", ",").split(","):
                    p = p.strip()
                    if p and p.isdigit():
                        industries.add(p)
        for f in PRODUCT_FIELDS:
            raw = ac.get(f)
            if raw:
                for p in str(raw).split(","):
                    p = p.strip()
                    if p:
                        products.add(p)
        return {"industryCodes": sorted(industries), "products": sorted(products)}

    def _fetch_all(self, prefix: str, page_size: int = 500,
                   extra: Optional[dict] = None) -> list:
        """Fetch all pages from {prefix}/query. Returns list of items."""
        all_items, page = [], 1
        while True:
            body = {"pager": {"pageNum": page, "pageSize": page_size},
                    "industryVersion": "6.6"}
            if extra:
                body.update(extra)
            resp = self._post(f"{BASE}/{prefix}/query", body)
            data = resp.get("data") or {}
            items = data.get("data") or []
            all_items.extend(items)
            total = int(data.get("totalCount") or 0)
            total_page = int(data.get("totalPage") or 0)
            if not items or len(all_items) >= total or page >= (total_page or 1):
                break
            page += 1
        return all_items

    def search_shield(self, dim: str, ids: list) -> dict:
        """Find shield strategies covering the given ids on dimension `dim`.

        Strategy: server-side filter on orientControl (supported keys: appId/uid/posId),
        then full-scan mediaControl (query ignores filters; only ~356 items) and
        local-match on mediaCluster. Results are deduped per strategy.
        """
        dim = dim.lower()
        if dim not in DIM_ALIAS:
            raise ValueError(f"不支持的维度 {dim}，仅支持 appId/uid/posId")
        dim = DIM_ALIAS[dim]
        idset = {str(i).strip() for i in ids if str(i).strip()}
        matched = {}

        # orientControl: server-side filter per id
        for one_id in idset:
            body = {"industryVersion": "6.6", DIM_MAP[dim]: one_id}
            items = self._fetch_all("orientControl", extra=body)
            for it in items:
                sid = it.get("id")
                if sid is None or sid in matched:
                    continue
                itype = it.get("type")
                if itype in NON_SHIELD_TYPES:
                    continue
                mc = it.get("mediaCluster") or {}
                if not mc.get(DIM_MAP[dim]):
                    continue
                matched[sid] = {"item": it, "hitIds": {one_id}}

        # mediaControl: full scan + local match
        for it in self._fetch_all("mediaControl"):
            sid = it.get("id")
            if sid is None or sid in matched:
                continue
            itype = it.get("type")
            if itype in NON_SHIELD_TYPES:
                continue
            mc = it.get("mediaCluster") or {}
            vals = self._extract_dim_values(mc, DIM_MAP[dim])
            hits = vals & idset
            if hits:
                matched[sid] = {"item": it, "hitIds": hits}

        results = []
        for sid, entry in matched.items():
            it = entry["item"]
            ac = it.get("adCluster") or {}
            ind_prod = self._extract_industry_product(it)
            results.append({
                "id": sid,
                "name": it.get("name"),
                "type": it.get("type"),
                "typeLabel": SHIELD_TYPE_LABELS.get(it.get("type"),
                                                   f"type={it.get('type')}"),
                "status": it.get("status"),
                "statusDesc": it.get("statusDesc"),
                "beginTime": it.get("beginTime"),
                "endTime": it.get("endTime"),
                "shieldUserType": it.get("shieldUserType") or ac.get("shieldUserType"),
                "industryCodes": ind_prod["industryCodes"],
                "products": ind_prod["products"],
                "dimension": self._extract_shield_dim(it),
                "matchedIds": sorted(entry["hitIds"]),
            })
        results.sort(key=lambda r: (r["status"] != 4, r["id"]))
        return {"dimension": dim, "total": len(results), "results": results}

    def merge_edit(self, prefix: str, body: dict) -> dict:
        """POST /{prefix}/mergeEdit or mergeEditV2."""
        if prefix == "orientControl":
            url = f"{BASE}/{prefix}/mergeEditV2"
        else:
            url = f"{BASE}/{prefix}/mergeEdit"
        return self._post(url, body)

    def quick_push(self, prefix: str, strategy_id: str, push_type: int = 0,
                   confirm: bool = False) -> dict:
        """POST /{prefix}/quickPushAll."""
        params = {"id": strategy_id, "type": push_type}
        if confirm:
            params["confirm"] = "true"
        resp = self.session.request("POST", f"{BASE}/{prefix}/quickPushAll",
                                    params=params, json={})
        resp.raise_for_status()
        return resp.json()

    def withdraw(self, strategy_id: str, prefix: str = "orientControl") -> dict:
        """POST /{prefix}/withdraw."""
        return self._post(f"{BASE}/{prefix}/withdraw", {"id": strategy_id})

    def approve_query(self, body: dict) -> dict:
        """POST /approve/query."""
        return self._post(f"{BASE}/approve/query", body)

    def approve_change_status(self, body: dict) -> dict:
        """POST /approve/changeStatus."""
        return self._post(f"{BASE}/approve/changeStatus", body)

    def list_user(self) -> dict:
        """GET /common/listUser."""
        return self._get(f"{BASE}/common/listUser")


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Operation API client")
    sub = parser.add_subparsers(dest="command", required=True)

    # get
    p_get = sub.add_parser("get", help="Fetch strategy detail")
    p_get.add_argument("--id", dest="strategy_id", help="Single strategy ID")
    p_get.add_argument("--ids", help="Comma-separated strategy IDs for batch")
    p_get.add_argument("--prefix", help="Force prefix (skip auto-detect)")

    # query
    p_query = sub.add_parser("query", help="List strategies (POST orientControl/query)")
    p_query.add_argument("--app-id")
    p_query.add_argument("--uid")
    p_query.add_argument("--pos-id")
    p_query.add_argument("--status", type=int)
    p_query.add_argument("--page", type=int, default=1)
    p_query.add_argument("--page-size", type=int, default=50)
    p_query.add_argument("--industry-version", default="6.6")
    p_query.add_argument("--raw-body", help="Raw JSON body (overrides other query args)")

    # search: find shield strategies covering appId/uid/posId
    p_search = sub.add_parser(
        "search", help="Find shield strategies covering given media/app ids")
    p_search.add_argument("--dim", required=True, type=str.lower,
                          help="ID dimension: appId/uid/posId (one per call)")
    p_search.add_argument("--ids", required=True, dest="search_ids",
                          help="Comma-separated IDs of the SAME dimension (batch, <=50)")
    p_search.add_argument("--item", dest="search_item",
                          help="Retry a single ID only (equivalent to --ids <id>)")

    # merge-edit
    p_me = sub.add_parser("merge-edit", help="Edit and submit for approval")
    p_me.add_argument("--prefix", required=True)
    p_me.add_argument("--file", required=True, help="JSON file with edit body")

    # merge-edit-easy (just extend endTime)
    p_mee = sub.add_parser("extend", help="Extend strategy endTime and submit")
    p_mee.add_argument("--id", required=True, dest="strategy_id")
    p_mee.add_argument("--end-time", required=True, type=int, help="New endTime (epoch ms)")
    p_mee.add_argument("--prefix", help="Force prefix")

    # quick-push
    p_qp = sub.add_parser("quick-push", help="Quick push all")
    p_qp.add_argument("--prefix", required=True)
    p_qp.add_argument("--id", required=True, dest="strategy_id")
    p_qp.add_argument("--type", dest="push_type", type=int, default=0)
    p_qp.add_argument("--confirm", action="store_true")

    # withdraw
    p_w = sub.add_parser("withdraw", help="Withdraw strategy")
    p_w.add_argument("--id", required=True, dest="strategy_id")
    p_w.add_argument("--prefix", default="orientControl")

    # approve
    p_aq = sub.add_parser("approve-query", help="Query approval records")
    p_aq.add_argument("--file", required=True, help="JSON file with query body")

    p_acs = sub.add_parser("approve-change", help="Change approval status")
    p_acs.add_argument("--file", required=True, help="JSON file with change body")

    # list-user
    sub.add_parser("list-user", help="List users")

    args = parser.parse_args()

    client = OperationClient()
    started = time.monotonic()

    try:
        if args.command == "get":
            if args.ids:
                ids = [x.strip() for x in args.ids.split(",") if x.strip()]
                result = client.get_batch(ids, prefix=args.prefix)
            elif args.strategy_id:
                result = client.get(args.strategy_id, prefix=args.prefix)
            else:
                print(json.dumps({"ok": False, "code": 1, "error": "MISSING_ID",
                                  "msg": "需要 --id 或 --ids",
                                  "duration_ms": int((time.monotonic() - started) * 1000)},
                                 ensure_ascii=False))
                sys.exit(1)

        elif args.command == "query":
            if args.raw_body:
                body = json.loads(args.raw_body)
            else:
                body = {
                    "pager": {"pageNum": args.page, "pageSize": args.page_size},
                    "industryVersion": args.industry_version,
                }
                if args.status is not None:
                    body["status"] = args.status
                if args.app_id:
                    body["appId"] = args.app_id
                if args.uid:
                    body["uid"] = args.uid
                if args.pos_id:
                    body["posId"] = args.pos_id
            result = client.query(body)

        elif args.command == "search":
            ids = [x.strip() for x in args.search_ids.split(",") if x.strip()]
            if args.search_item:
                ids = [args.search_item.strip()]
            if len(ids) > 50:
                print(json.dumps({"ok": False, "code": 2, "error": "BATCH_TOO_LARGE",
                                  "msg": f"单次最多 50 个 ID（当前 {len(ids)}），请分批查询",
                                  "next_action": {"type": "ASK_USER", "retryable": False},
                                  "duration_ms": int((time.monotonic() - started) * 1000)},
                                 ensure_ascii=False))
                sys.exit(2)
            result = client.search_shield(args.dim, ids)

        elif args.command == "merge-edit":
            with open(args.file, "r") as f:
                body = json.load(f)
            result = client.merge_edit(args.prefix, body)

        elif args.command == "extend":
            # 1) get current strategy
            detail = client.get(args.strategy_id, prefix=args.prefix)
            prefix = args.prefix or detail.get("_prefix")
            data = detail.get("data", {})
            if not data or not prefix:
                print(json.dumps({"ok": False, "code": 1, "error": "STRATEGY_NOT_FOUND",
                                  "msg": f"策略 {args.strategy_id} 未找到", "data": detail,
                                  "duration_ms": int((time.monotonic() - started) * 1000)},
                                 ensure_ascii=False))
                sys.exit(1)

            # 2) build minimal edit body
            body = {
                "id": data.get("id"),
                "name": data.get("name"),
                "type": data.get("type"),
                "background": data.get("background") or data.get("name", ""),
                "beginTime": data.get("beginTime"),
                "endTime": args.end_time,
                "adCluster": data.get("adCluster", {}),
                "mediaCluster": data.get("mediaCluster", {}),
            }
            body["adCluster"]["industryVersion"] = "6.6"
            # strip readonly shell fields
            for cluster in ("adCluster", "mediaCluster"):
                if cluster in body:
                    body[cluster].pop("maxNum", None)
            # add shield fields for orientControl
            if prefix == "orientControl":
                for key in ("shieldType", "shieldMediaType", "shieldUserType"):
                    if key in data:
                        body[key] = data[key]

            result = client.merge_edit(prefix, body)

        elif args.command == "quick-push":
            result = client.quick_push(args.prefix, args.strategy_id,
                                       args.push_type, args.confirm)

        elif args.command == "withdraw":
            result = client.withdraw(args.strategy_id, args.prefix)

        elif args.command == "approve-query":
            with open(args.file, "r") as f:
                body = json.load(f)
            result = client.approve_query(body)

        elif args.command == "approve-change":
            with open(args.file, "r") as f:
                body = json.load(f)
            result = client.approve_change_status(body)

        elif args.command == "list-user":
            result = client.list_user()

        else:
            print(json.dumps({"ok": False, "code": 1, "error": "UNKNOWN_COMMAND",
                              "msg": f"未知命令: {args.command}",
                              "duration_ms": int((time.monotonic() - started) * 1000)},
                             ensure_ascii=False))
            sys.exit(1)

        print(json.dumps({"ok": True, "code": 0, "data": result,
                          "duration_ms": int((time.monotonic() - started) * 1000)},
                         ensure_ascii=False, default=str))

    except Exception as e:
        msg = str(e)
        # classify errors
        if "不支持的维度" in msg:
            error_code = "DIMENSION_INVALID"
            next_action = {"type": "STOP", "retryable": False}
        elif "SSO" in msg.upper() or "login" in msg.lower():
            error_code = "SSO_EXPIRED"
            next_action = {"type": "ASK_USER", "retryable": True}
        elif "timed out" in msg.lower() or "timeout" in msg.lower():
            error_code = "API_TIMEOUT"
            next_action = {"type": "RETRY_ONCE_THEN_STOP", "retryable": True}
        elif "401" in msg or "403" in msg:
            error_code = "AUTH_FAILED"
            next_action = {"type": "ASK_USER", "retryable": False}
        elif "4" in msg[:3] if len(msg) >= 3 else False:
            error_code = "API_4XX"
            next_action = {"type": "STOP", "retryable": False}
        else:
            error_code = "API_ERROR"
            next_action = {"type": "STOP", "retryable": False}

        print(json.dumps({"ok": False, "code": 1, "error": error_code, "msg": msg,
                          "next_action": next_action,
                          "duration_ms": int((time.monotonic() - started) * 1000)},
                         ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()