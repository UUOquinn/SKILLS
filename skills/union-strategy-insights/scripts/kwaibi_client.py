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
KwaiBI REST API client.

Uses SmartSSOSession for transparent SSO auth.
Supports: dataset detail, data/query (structured SQL-free queries).

Usage:
  uv run scripts/kwaibi_client.py dataset-detail --id 85587
  uv run scripts/kwaibi_client.py query --data-set 85587 --fields '[{"id":123,"name":"xx","type":"dimension"}]' --filters '[{"field":{},"operator":"IN","value":["v1"]}]' --start 1719936000000 --end 1720022399999
  uv run scripts/kwaibi_client.py query --data-set 85587 --body-file body.json
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ks_aimate.sso_login_client import SmartSSOSession

BASE = "https://bi-platform.example.corp"

# ── dataset field id tables ────────────────────────────────────────
# Key: datasetId. Value: {logicalName: (fieldId, fieldName, type)}
# IDs verified against /api/v1/dataset/detail for each dataset. See tools-kwaibi.md.
DATASET_FIELDS = {
    85587: {
        "industry": (1329593, "新媒体行业名称", "dimension"),
        "sub_industry": (1329627, "新媒体子行业名称", "dimension"),
        "product": (1329640, "产品名称", "dimension"),
        "cost": (1329556, "联盟总消耗", "metric"),
        "uid": (1329558, "开发者id", "dimension"),
        "app_id": (2313961, "联盟媒体ID", "dimension"),
        "pos_id": (1329667, "广告位", "dimension"),
    },
    129496: {
        "industry": (2212701, "新媒体行业名称", "dimension"),
        "sub_industry": (2212693, "新媒体子行业名称", "dimension"),
        "product": (2215361, "产品名称", "dimension"),
        "cost": (2215299, "联盟总消耗", "metric"),
        "uid": (2212697, "开发者id", "dimension"),
        "app_id": (2313479, "联盟媒体ID", "dimension"),
        "pos_id": (2212707, "广告位", "dimension"),
        "scene": (2212699, "广告场景", "dimension"),
        "coop": (2212691, "合作模式", "dimension"),
        "mtype": (2212696, "媒体类型_内外部", "dimension"),
        "deliver": (2215354, "投放类型", "dimension"),
        "opclass": (2633690, "联盟运营分类", "dimension"),
    },
}

TRAFFIC_GROUPS = ("scene", "coop", "mtype", "deliver", "opclass")
OLAP_URNS = {
    "uid": "ks:dimension:ad_v2/uid",
    "app_id": "ks:dimension:ad_v2/app_id",
    "pos_id": "ks:dimension:ad_v2/pos_id",
    "scene": "ks:dimension:ad_v2/ad_style",
    "coop": "ks:dimension:ad_v2/cooperation_mode",
    "mtype": "ks:dimension:ad_v2/medium_inner_out_type",
    "deliver": "ks:dimension:ad_v2/placement_type",
    "opclass": "ks:dimension:ad_v2/business_type",
    "cost": "ks:metric:ad_v2/union_total_cost_amt",
}
OLAP_TEMPLATE = Path(__file__).resolve().parent.parent / "references" / "olap_traffic_template.json"
OLAP_REFERER = "https://internal.example.corp/main?shareId=66611514&dataSetId=129496"
OLAP_FIELD_PROTO = {
    "dataSetId": 129496, "id": 2212690, "fieldId": "", "name": "账户ID",
    "nickName": "账户ID", "showName": "账户ID", "type": "dimension",
    "valueType": "string", "ordinalColumnType": "dimension", "ordinalAggregatorType": "",
    "urn": "ks:dimension:ad_v2/account_id", "functionType": "", "returnType": "",
    "functionExpression": "", "isExtend": False, "hotField": True,
    "isSystemField": False, "useNewDefaultFormat": True, "lodMetricFlag": False,
    "nativeSqlMetricFlag": False, "gaiaCaseWhenMetricFlag": False, "isCount": False,
    "groupPercentType": "", "fieldCellStyle": None, "axisId": "rowDimension",
    "sorter": {"fieldId": 2212690, "order": "", "type": "SIMPLE"},
    "showFormat": "", "fieldComment": "", "indicatorAttr": "",
    "abnormalValueConfig": "hidden",
    "attributes": {"fieldDetailUrl": "", "agg": "false", "metricInsightRules": "[]",
                   "metricInsight": "false", "twiceCalc": "false"},
}

# logical dimension names -> KwaiBI filter field names (see tools-kwaibi.md §4.2)
FILTER_KEYS = {
    "uid": "uid",
    "app_id": "app_id",
    "pos_id": "pos_id",
}

# ── time helpers ───────────────────────────────────────────────────


def today_end_ms() -> int:
    """Millis to end of today (Asia/Shanghai)."""
    tz = timezone(timedelta(hours=8))
    now = datetime.now(tz)
    eod = now.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(eod.timestamp() * 1000)


def days_ago_ms(n: int, end_of_day: bool = False) -> int:
    """Millis to n days ago at 00:00:00 (or 23:59:59)."""
    tz = timezone(timedelta(hours=8))
    dt = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=n)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(dt.timestamp() * 1000)


def olap_field(key: str) -> dict:
    field_id, name, field_type = DATASET_FIELDS[129496][key]
    urn = OLAP_URNS[key]
    field = copy.deepcopy(OLAP_FIELD_PROTO)
    field.update(id=field_id, fieldId=str(uuid.uuid4()), name=name,
                 nickName=name, showName=name, type=field_type,
                 ordinalColumnType=field_type, urn=urn,
                 valueType="number" if field_type == "metric" else "string")
    field["sorter"]["fieldId"] = field_id
    category = "METRIC" if field_type == "metric" else "DIMENSION"
    field["attributes"]["fieldDetailUrl"] = (
        f"https://internal.example.corp/v2/#/assets/detail?category={category}&urn={urn}")
    return field


def _is_null_group(value) -> bool:
    """判断聚合维度的取值是否为 null / 空分类（上游把 null 当字符串返回）。"""
    if value is None:
        return True
    text = str(value).strip()
    return text == "" or text.lower().startswith("null")


class KwaiBIClient:
    def __init__(self):
        self.session = SmartSSOSession()

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{BASE}{path}"
        resp = self.session.request("GET", url, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        url = f"{BASE}{path}"
        headers = {
            "Accept": "application/json",
        }
        resp = self.session.request("POST", url, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def dataset_detail(self, dataset_id: int) -> dict:
        """GET /api/v1/dataset/detail?id= - fetch field metadata."""
        return self._get("/api/v1/dataset/detail", {"id": dataset_id})

    def data_query(self, body: dict) -> dict:
        """POST /api/v1/data/query - structured query."""
        return self._post("/api/v1/data/query", body)

    def query_alias(self, data_set_id: int, dim: str, ids: list,
                    group_by: str = "industry", start: int = None, end: int = None,
                    days: int = 7, limit: int = 200) -> dict:
        """Query cost grouped by industry/product for a set of IDs on one dimension.

        dim: uid | app_id | pos_id (one dimension per call)
        group_by: industry | sub_industry | product
        ids: list of ID strings (same dimension, <=50)
        """
        table = DATASET_FIELDS.get(data_set_id)
        if not table:
            raise ValueError(f"数据集 {data_set_id} 无内置字段表，请先用 dataset-detail 查字段 id 后用 query")
        if dim not in FILTER_KEYS:
            raise ValueError(f"不支持的 ID 维度 {dim}，仅支持 uid/app_id/pos_id")
        if group_by in TRAFFIC_GROUPS and data_set_id == 129496:
            return self.query_traffic_olap(dim, ids, group_by, start, end, days, limit)
        if group_by not in ("industry", "sub_industry", "product"):
            raise ValueError(f"数据集 {data_set_id} 不支持聚合维度 {group_by}")

        gid, gname, _ = table[group_by]
        cid, cname, _ = table["cost"]
        fid, fname, _ = table[FILTER_KEYS[dim]]

        start_time = start if start is not None else days_ago_ms(days)
        end_time = end if end is not None else today_end_ms()

        body = {
            "dataSetId": data_set_id,
            "type": 1,
            "fields": [
                {"id": gid, "name": gname, "type": "dimension", "dataSetId": data_set_id},
                {"id": cid, "name": cname, "type": "metric", "dataSetId": data_set_id},
            ],
            "filters": [
                {"field": {"id": fid, "name": fname, "type": "dimension",
                           "dataSetId": data_set_id},
                 "operator": "IN", "value": [str(i) for i in ids]},
            ],
            "timeFilter": {"fieldName": "__time", "startTime": start_time,
                           "endTime": end_time},
            "limit": limit,
            "rowLimit": limit,
        }
        resp = self.data_query(body)
        data = resp.get("data") or {}
        rows = data.get("dataList") or data.get("rows") or []
        columns = [c.get("name") for c in (data.get("columns") or [])]
        results = []
        for row in rows:
            group_val = row[0] if len(row) > 0 else None
            if _is_null_group(group_val):
                continue
            results.append({
                group_by: group_val,
                "cost": row[1] if len(row) > 1 else None,
            })
        results.sort(key=lambda r: (r.get("cost") is None, -(r.get("cost") or 0)))
        return {"dataSetId": data_set_id, "dimension": dim, "ids": ids,
                "groupBy": group_by, "columns": columns,
                "startTime": start_time, "endTime": end_time,
                "rowCount": data.get("rowCount"), "rows": results}

    def query_traffic_olap(self, dim: str, ids: list, group_by: str,
                           start: int | None, end: int | None, days: int,
                           limit: int) -> dict:
        """129496 流量侧维度消耗：复用抓包模板，通过 KwaiBI OLAP 模式查询。"""
        if dim not in FILTER_KEYS:
            raise ValueError(f"不支持的 ID 维度 {dim}，仅支持 uid/app_id/pos_id")
        start_time = start if start is not None else days_ago_ms(days)
        end_time = end if end is not None else today_end_ms()
        if start_time >= end_time:
            raise ValueError("--start 必须早于 --end")
        body = json.loads(OLAP_TEMPLATE.read_text(encoding="utf-8"))
        body["fields"] = [olap_field(group_by), olap_field("cost")]
        body["rowLimit"] = limit
        body["limit"] = limit
        body["queryTime"] = int(datetime.now(timezone(timedelta(hours=8))).timestamp() * 1000)
        time_filter = body["filters"][0]
        time_filter["filterValue"]["startTime"] = start_time
        time_filter["filterValue"]["endTime"] = end_time
        time_filter["originalFilterValue"]["isDynamic"] = False
        fid, fname, _ = DATASET_FIELDS[129496][dim]
        body["filters"].append({
            "filterType": "string", "targetId": fid, "urn": OLAP_URNS[dim],
            "name": fname, "nickName": fname, "type": "dimension",
            "valueType": "string",
            "filterValue": {"condition": "in", "value": [str(i) for i in ids],
                            "compareType": ""},
            "originalFilterValue": {"condition": "in", "value": [str(i) for i in ids]},
            "selectType": "Multipul", "showType": "Dropdown", "disabled": False,
        })
        response = self.session.request(
            "POST", f"{BASE}/api/v1/data/query", json=body,
            headers={"Accept": "application/json", "Kwaibi-View-Query-Type": "8",
                     "kwaibi-request-module": "olap", "Referer": OLAP_REFERER},
            timeout=180,
        )
        if response.status_code in (401, 302) or "text/html" in (response.headers.get("Content-Type") or "").lower():
            raise PermissionError("SSO_EXPIRED")
        if response.status_code == 403:
            raise PermissionError("AUTH_FAILED")
        response.raise_for_status()
        payload = response.json()
        if payload.get("result") == 0 and payload.get("error_msg") not in (None, "", "canDiff"):
            raise RuntimeError(f"OLAP 查询失败: {payload['error_msg']}")
        data = payload.get("data") or {}
        columns = [c.get("name") for c in (data.get("columns") or [])]
        _, group_name, _ = DATASET_FIELDS[129496][group_by]
        if columns[:2] != [group_name, "联盟总消耗"]:
            raise RuntimeError(f"OLAP 查询失败或列不匹配: {payload.get('error_msg') or columns}")
        totals = {}
        for row in data.get("dataList") or []:
            cells = [c.get("r", c.get("v")) if isinstance(c, dict) else c for c in row]
            if len(cells) < 2 or _is_null_group(cells[0]) or cells[0] in ("$ALL$", "合计", "汇总"):
                continue
            try:
                cost = float(cells[1])
            except (TypeError, ValueError):
                continue
            key = str(cells[0])
            totals[key] = totals.get(key, 0.0) + cost
        rows = [{group_by: key, "cost": cost} for key, cost in
                sorted(totals.items(), key=lambda item: -item[1])]
        return {"dataSetId": 129496, "source": "kwaibi_olap_mode",
                "dimension": dim, "ids": ids, "groupBy": group_by,
                "columns": columns, "startTime": start_time, "endTime": end_time,
                "rowCount": len(rows), "rows": rows}


# ── CLI ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="KwaiBI API client")
    sub = parser.add_subparsers(dest="command", required=True)

    # dataset-detail
    p_dd = sub.add_parser("dataset-detail", help="Get dataset field metadata")
    p_dd.add_argument("--id", type=int, required=True, dest="dataset_id")
    p_dd.add_argument("--full", action="store_true", dest="dd_full",
                      help="输出完整原始字段定义（默认只输出 id/name/type/valueType/urn）")

    # query
    p_q = sub.add_parser("query", help="Structured data query")
    p_q.add_argument("--data-set", type=int, required=True, dest="data_set_id")
    p_q.add_argument("--body-file", help="Full JSON body file (overrides others)")
    p_q.add_argument("--fields", help="JSON array of field objects")
    p_q.add_argument("--filters", help="JSON array of filter objects (default [])")
    p_q.add_argument("--start", type=int, help="Start time (epoch ms)")
    p_q.add_argument("--end", type=int, dest="end_time", help="End time (epoch ms)")
    p_q.add_argument("--days", type=int, default=7, help="Default time range in days (if --start not set)")
    p_q.add_argument("--limit", type=int, default=100)
    p_q.add_argument("--row-limit", type=int, default=100)

    # query-alias: cost by industry/product for a dimension of ids
    p_qa = sub.add_parser("query-alias",
                          help="Cost grouped by industry/product for given IDs")
    p_qa.add_argument("--data-set", type=int, default=85587, dest="data_set_id",
                      help="Dataset id (default 85587 offline; 129496 realtime)")
    p_qa.add_argument("--dim", required=True, dest="qa_dim",
                      help="ID dimension: uid | app_id | pos_id (one per call)")
    p_qa.add_argument("--ids", dest="qa_ids",
                      help="Comma-separated IDs of the SAME dimension (batch, <=50)")
    p_qa.add_argument("--item", dest="qa_item",
                      help="Retry a single ID only (equivalent to --ids <id>)")
    p_qa.add_argument("--group-by", dest="qa_group", default="industry",
                      choices=["industry", "sub_industry", "product", *TRAFFIC_GROUPS],
                      help="Aggregation dimension; traffic groups require --data-set 129496")
    p_qa.add_argument("--start", type=int, help="Start time (epoch ms)")
    p_qa.add_argument("--end", type=int, dest="qa_end", help="End time (epoch ms)")
    p_qa.add_argument("--days", type=int, default=7, help="Days back if --start omitted")

    args = parser.parse_args()
    client = KwaiBIClient()
    started = time.monotonic()

    try:
        if args.command == "dataset-detail":
            result = client.dataset_detail(args.dataset_id)
            if not args.dd_full:
                fields = (result.get("data") or {}).get("fields") or []
                result = {
                    "dataSetId": args.dataset_id,
                    "fieldCount": len(fields),
                    "fields": [
                        {
                            "id": f.get("id"),
                            "name": f.get("nickName") or f.get("name"),
                            "type": f.get("type"),
                            "valueType": f.get("valueType"),
                            "urn": f.get("urn"),
                        }
                        for f in fields
                    ],
                }

        elif args.command == "query":
            if args.body_file:
                with open(args.body_file, "r") as f:
                    body = json.load(f)
            else:
                fields = json.loads(args.fields) if args.fields else []
                filters = json.loads(args.filters) if args.filters else []

                start_time = args.start
                end_time = args.end_time
                if start_time is None:
                    start_time = days_ago_ms(args.days)
                if end_time is None:
                    end_time = today_end_ms()

                body = {
                    "dataSetId": args.data_set_id,
                    "type": 1,
                    "fields": fields,
                    "filters": filters,
                    "timeFilter": {
                        "fieldName": "__time",
                        "startTime": start_time,
                        "endTime": end_time,
                    },
                    "limit": args.limit,
                    "rowLimit": args.row_limit,
                }
            result = client.data_query(body)

        elif args.command == "query-alias":
            ids = [x.strip() for x in (args.qa_ids or "").split(",") if x.strip()]
            if args.qa_item:
                ids = [args.qa_item.strip()]
            if not ids:
                raise ValueError("请提供至少一个 ID（--ids 或 --item）")
            if len(ids) > 50:
                print(json.dumps({"ok": False, "code": 2, "error": "BATCH_TOO_LARGE",
                                  "msg": f"单次最多 50 个 ID（当前 {len(ids)}），请分批查询",
                                  "next_action": {"type": "ASK_USER", "retryable": False},
                                  "duration_ms": int((time.monotonic() - started) * 1000)},
                                 ensure_ascii=False))
                sys.exit(2)
            result = client.query_alias(args.data_set_id, args.qa_dim, ids,
                                        args.qa_group, args.start, args.qa_end,
                                        args.days)

        else:
            print(json.dumps({"ok": False, "code": 1, "error": "UNKNOWN_COMMAND",
                              "msg": f"未知命令: {args.command}",
                              "duration_ms": int((time.monotonic() - started) * 1000)},
                             ensure_ascii=False))
            sys.exit(1)

        print(json.dumps({"ok": True, "code": 0, "data": result,
                          "next_action": {"type": "CONTINUE", "retryable": False},
                          "duration_ms": int((time.monotonic() - started) * 1000)},
                         ensure_ascii=False, default=str))

    except Exception as e:
        msg = str(e)
        code = 1
        if "SSO_EXPIRED" in msg or "login" in msg.lower():
            error_code = "SSO_EXPIRED"
            next_action = {"type": "ASK_USER", "retryable": True}
            code = 2
        elif "AUTH_FAILED" in msg or "401" in msg or "403" in msg:
            error_code = "AUTH_FAILED"
            next_action = {"type": "ASK_USER", "retryable": False}
            code = 2
        elif "timed out" in msg.lower() or "timeout" in msg.lower():
            error_code = "API_TIMEOUT"
            next_action = {"type": "RETRY_ONCE_THEN_STOP", "retryable": True}
        elif "OLAP 查询" in msg:
            error_code = "OLAP_QUERY_FAILED"
            next_action = {"type": "STOP", "retryable": False}
        elif msg[:3].startswith("4"):
            error_code = "API_4XX"
            next_action = {"type": "STOP", "retryable": False}
        else:
            error_code = "API_ERROR"
            next_action = {"type": "STOP", "retryable": False}

        print(json.dumps({"ok": False, "code": code, "error": error_code, "msg": msg,
                          "next_action": next_action,
                          "duration_ms": int((time.monotonic() - started) * 1000)},
                         ensure_ascii=False))
        sys.exit(code)


if __name__ == "__main__":
    main()