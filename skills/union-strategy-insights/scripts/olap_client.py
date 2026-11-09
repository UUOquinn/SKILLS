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
"""129496 的 OLAP 模式查询客户端。

模板必须来自已验证的 OLAP 看板请求；本脚本只替换日期和可选的单一 ID 维度过滤，
不尝试拼造字段对象，避免过滤条件被服务端静默忽略。

接入点：请求发往 kwaibi.corp 的 /api/v1/data/query，携带 OLAP 查询模式头
（Kwaibi-View-Query-Type: 8 + kwaibi-request-module: olap），底层由 OLAP 引擎执行。
olap.corp 域名在当前 SSO 环境直连不可用（401），不要改回直连。
"""

import argparse
import copy
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ks_aimate.sso_login_client import SmartSSOSession

from kwaibi_client import _is_null_group, olap_field

OLAP_URL = "https://bi-platform.example.corp/api/v1/data/query"
TZ = timezone(timedelta(hours=8))
DATASET_ID = 129496

DIMENSION_FIELDS = {
    "uid": (2212697, "开发者id"),
    "app_id": (2313479, "联盟媒体ID"),
    "pos_id": (2212707, "广告位"),
}


def output(ok: bool, code: int, **kwargs: Any) -> None:
    body = {"ok": ok, "code": code, "duration_ms": kwargs.pop("duration_ms", 0), **kwargs}
    print(json.dumps(body, ensure_ascii=False, default=str))


def value(cell: Any) -> Any:
    if isinstance(cell, dict):
        return cell.get("r", cell.get("v"))
    return cell


def patch_datetime_filters(body: dict[str, Any], start: datetime, end: datetime) -> None:
    day_start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = end.replace(hour=23, minute=59, second=59, microsecond=999000)
    count = 0
    for flt in body.get("filters") or []:
        item = flt.get("filterValue")
        if isinstance(item, dict) and "startTime" in item and "endTime" in item:
            item["startTime"] = int(day_start.timestamp() * 1000)
            item["endTime"] = int(day_end.timestamp() * 1000)
            count += 1
    if count == 0:
        raise RuntimeError("模板缺少日期 filter，拒绝执行以避免读取抓包当天的旧数据")


def add_id_filter(body: dict[str, Any], dim: str, ids: list[str]) -> None:
    field_id, name = DIMENSION_FIELDS[dim]
    body.setdefault("filters", []).append({
        "filterType": "string",
        "targetId": field_id,
        "urn": "",
        "name": name,
        "nickName": name,
        "type": "dimension",
        "valueType": "string",
        "filterValue": {"condition": "in", "value": ids, "compareType": ""},
        "originalFilterValue": {"condition": "in", "value": ids},
        "selectType": "Multipul",
        "showType": "Dropdown",
        "disabled": False,
    })


def query(template_path: Path, start: datetime, end: datetime, dim: str | None, ids: list[str], referer: str, fields_keys: list[str] | None = None) -> dict[str, Any]:
    template = json.loads(template_path.read_text(encoding="utf-8"))
    body = copy.deepcopy(template)
    if body.get("dataSetId") != DATASET_ID:
        raise RuntimeError(f"模板 dataSetId 必须为 {DATASET_ID}")
    if fields_keys:
        body["fields"] = [olap_field(key) for key in fields_keys]
    body["queryTime"] = int(datetime.now(TZ).timestamp() * 1000)
    patch_datetime_filters(body, start, end)
    if dim:
        add_id_filter(body, dim, ids)

    response = SmartSSOSession().request(
        "POST", OLAP_URL, json=body,
        headers={
            "Accept": "application/json",
            "Kwaibi-View-Query-Type": "8",
            "kwaibi-request-module": "olap",
            "Referer": referer,
        },
        timeout=180,
    )
    content_type = (response.headers.get("Content-Type") or "").lower()
    if response.status_code in (401, 302) or "text/html" in content_type:
        raise PermissionError("SSO_EXPIRED")
    if response.status_code == 403:
        raise PermissionError("AUTH_FAILED")
    response.raise_for_status()
    payload = response.json()
    if payload.get("result") == 0 and payload.get("error_msg") not in (None, "", "canDiff"):
        raise RuntimeError(f"OLAP 查询失败: {payload['error_msg']}")
    data = payload.get("data") or {}
    columns = [c.get("name") for c in data.get("columns") or []]
    if not columns:
        err = payload.get("error_msg")
        detail = "" if not err or err == "canDiff" else f": {err}"
        raise RuntimeError(f"OLAP 未返回数据{detail}，检查模板 fields 是否为空或时间窗是否超限")
    rows = []
    for row in data.get("dataList") or []:
        cells = [value(cell) for cell in row]
        if not cells or _is_null_group(cells[0]) or cells[0] in ("$ALL$", "合计", "汇总"):
            continue
        if len(cells) > 1 and cells[1] in ("$ALL$", "合计", "汇总"):
            continue
        rows.append(cells)
    return {
        "source": "olap_129496",
        "columns": columns,
        "rows": rows,
        "rowCount": len(rows),
        "startTime": int(start.timestamp() * 1000),
        "endTime": int(end.timestamp() * 1000),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="129496 OLAP 模式查询（kwaibi.corp）")
    parser.add_argument("--template", required=True)
    parser.add_argument("--start", type=int, required=True, help="epoch ms")
    parser.add_argument("--end", type=int, required=True, help="epoch ms")
    parser.add_argument("--referer", required=True)
    parser.add_argument("--dim", choices=sorted(DIMENSION_FIELDS))
    parser.add_argument("--ids", help="同一维度的逗号分隔 ID，最多 50 个")
    parser.add_argument("--item", help="单 ID 重试")
    parser.add_argument("--fields", help="用内置字段表填充 fields，逗号分隔 key（如 scene,cost）；不传则沿用模板自带 fields")
    args = parser.parse_args()
    started = time.monotonic()
    try:
        ids = [x.strip() for x in (args.ids or "").split(",") if x.strip()]
        if args.item:
            ids = [args.item.strip()]
        if bool(args.dim) != bool(ids):
            raise ValueError("--dim 与 --ids 必须同时提供")
        if len(ids) > 50:
            output(False, 2, error="BATCH_TOO_LARGE", msg="单次最多 50 个同维度 ID", next_action={"type": "ASK_USER", "retryable": False})
            return 2
        fields_keys = [k.strip() for k in (args.fields or "").split(",") if k.strip()] or None
        start = datetime.fromtimestamp(args.start / 1000, TZ)
        end = datetime.fromtimestamp(args.end / 1000, TZ)
        if start >= end:
            raise ValueError("--start 必须早于 --end")
        result = query(Path(args.template), start, end, args.dim, ids, args.referer, fields_keys)
        output(True, 0, msg="OLAP 查询成功", data=result, next_action={"type": "CONTINUE", "retryable": False}, duration_ms=int((time.monotonic() - started) * 1000))
        return 0
    except PermissionError as exc:
        output(False, 2, error=str(exc), msg="OLAP 登录态失效或无权限", next_action={"type": "ASK_USER", "retryable": True}, duration_ms=int((time.monotonic() - started) * 1000))
        return 2
    except Exception as exc:
        output(False, 1, error="OLAP_QUERY_FAILED", msg=str(exc), next_action={"type": "STOP", "retryable": False}, duration_ms=int((time.monotonic() - started) * 1000))
        return 1


if __name__ == "__main__":
    sys.exit(main())
