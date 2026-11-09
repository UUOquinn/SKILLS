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
# url = "https://app.example.corp/kuaishou/prod/+simple/"
# publish = false
# ///
"""顶价扶持策略实时监控：读策略 → OLAP/离线取数 → 规则判定 → 撤回/降系数。

用法：
  单策略（内联 JSON）：
    uv run <skill_directory>/scripts/monitor.py --config '<json>'
  多策略（配置文件）：
    uv run <skill_directory>/scripts/monitor.py --config-file tmp/monitor.json

config 结构：
  {
    "dataset": DS_REALTIME,            // DS_REALTIME 实时（默认）；DS_OFFLINE 离线 T-1
    "referer": "<olap看板地址>",  // 仅 DS_REALTIME OLAP 路径需要
    "strategies": [
      {"id": 27123, "metric": "浅度超成本率", "op": ">=", "threshold": 0.20,
       "withdraw": false, "reduce_coefficient": 0.10, "window": "today"}
    ]
  }

指标（metric）可用中文名或数字 fieldId；前提是该指标存在于所选数据集。
默认三指标：联盟总消耗 / 浅度超成本率 / IAA广告变现ROI。
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from auth_helper.sso_login_client import SmartAuthSession

FLOW_URL = "https://ops.example.corp/ops/rest/boostControl"
OLAP_URL = "https://app.example.corp/api/v1/data/query"
KWAIBI_URL = "https://bi.example.corp/api/v1/data/query"
TZ = timezone(timedelta(hours=8))

TOP_PRICE_TYPES = {3: "顶价", 5: "CPA顶价"}
# get 接口对非顶价策略的 typeDesc 可能返回空串，这里提供兜底名
TYPE_NAMES = {1: "绿通", 3: "顶价", 4: "打折", 5: "CPA顶价", 10: "索引加白"}
SCOPE_NAMES = {1: "广告主顶价", 2: "媒体顶价", 3: "广告主*媒体顶价", 0: "未指定"}

# 策略字段 -> (维度名, DS_REALTIME fieldId, DS_OFFLINE fieldId)
DIMENSION_MAP = {
    "accountId": ("账户ID", 2212690, 1329545),
    "campaignType": ("营销目标", 2215345, 1329622),
    "ocpxActionType": ("转化目标", 2215378, 1329660),
    "deepConversionType": ("深度目标", 2215324, 1329592),
    "adStyle": ("广告场景", 2212699, 1329561),
    "appId": ("联盟媒体ID", 2313479, 2313961),
    "uid": ("开发者id", 2212697, 1329558),
    "posId": ("广告位", 2212707, 1329667),
}

# 指标名 -> (DS_REALTIME fieldId, DS_OFFLINE fieldId)。DS_OFFLINE 无 IAA 变现 ROI。
METRIC_MAP = {
    "联盟总消耗": (2215299, 1329556),
    "浅度超成本率": (2215353, 1330672),
    "IAA广告变现ROI": (7098032, None),
    "平均CPM顶价系数": (2983666, None),
    "平均CPA顶价系数": (2983659, None),
    "ROI出价系数_浅度": (7098033, None),
}

# 比值类指标（用消耗加权聚合，不能简单求和/平均）
RATIO_METRICS = {"浅度超成本率", "IAA广告变现ROI", "平均CPM顶价系数", "平均CPA顶价系数", "ROI出价系数_浅度"}

DROP_FIELDS = {
    "_apiPrefix", "_strategyType", "status", "statusDesc", "creatorName",
    "createTime", "updateTime", "isDel", "lastStatus", "greyStartTime",
    "expireSoon", "snapshot", "toolBar", "displayQuickPushBtn",
    "displayProhibitionPeriodQuickPushBtn", "displayPushBtn", "ruleTypes",
    "rangeTime", "reason", "strategyDetails",
}

OPS = {">", "<", ">=", "<=", "=", "==", "!="}


def emit(payload: dict[str, Any], exit_code: int = 0) -> None:
    payload.setdefault("duration_ms", 0)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(exit_code)


def _fail(error: str, msg: str, code: int = 1, retryable: bool = False,
          started: float = 0.0) -> None:
    emit({"ok": False, "code": code, "error": error, "msg": msg,
          "next_action": {"type": "STOP", "retryable": retryable},
          "duration_ms": int((time.monotonic() - started) * 1000)})


def raw_value(cell: Any) -> float:
    if isinstance(cell, dict):
        cell = cell.get("r", cell.get("v"))
    try:
        return float(cell or 0)
    except (TypeError, ValueError):
        return 0.0


def cell_text(cell: Any) -> str:
    if isinstance(cell, dict):
        return str(cell.get("v") or "")
    return str(cell or "")


def load_config(args: argparse.Namespace) -> dict[str, Any]:
    if args.config_file:
        cfg = json.loads(Path(args.config_file).read_text(encoding="utf-8"))
    elif args.config:
        cfg = json.loads(args.config)
    else:
        raise ValueError("必须提供 --config 或 --config-file")
    if not isinstance(cfg, dict) or not cfg.get("strategies"):
        raise ValueError("config 必须包含 strategies 数组")
    return cfg


class FlowClient:
    """boostControl 只读 + 撤回/降系数写操作，统一走 SmartAuthSession。"""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = SmartAuthSession()
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://ops.example.corp",
            "Referer": "https://ops.example.corp/flow",
        }

    def _req(self, method: str, path: str, *, params=None, body=None):
        try:
            resp = self.session.request(
                method, f"{FLOW_URL}/{path}", params=params, json=body,
                headers=self.headers, timeout=self.timeout, allow_redirects=False,
            )
        except Exception as exc:
            raise RuntimeError(f"NETWORK_ERROR: {exc}")
        if resp.status_code in (301, 302, 307, 308, 401):
            raise PermissionError("AUTH_EXPIRED")
        if resp.status_code == 403:
            raise PermissionError("AUTH_FAILED")
        if resp.status_code >= 500:
            raise RuntimeError(f"API_5XX: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise RuntimeError(f"API_{resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError(f"INVALID_RESPONSE: {resp.text[:200]}")

    def get(self, sid: int) -> dict[str, Any]:
        env = self._req("GET", "get", params={"id": sid})
        detail = env.get("data", env) if isinstance(env, dict) else env
        if not isinstance(detail, dict) or "id" not in detail:
            raise RuntimeError(f"NOT_FOUND: 策略 {sid} 不存在")
        return detail

    def withdraw(self, sid: int) -> dict[str, Any]:
        return self._req("POST", "withdraw", body={"id": sid})

    def reduce_coefficient(self, detail: dict[str, Any], delta: float,
                           floor: float = 1.0) -> dict[str, Any]:
        body = self._merge_body(detail)
        old = float(detail.get("bonusRate") or 0)
        body["bonusRate"] = round(max(old - delta, floor), 4)
        body["id"] = detail.get("id")
        return self._req("POST", "submitEdit", body=body)

    def raise_coefficient(self, detail: dict[str, Any], delta: float) -> dict[str, Any]:
        body = self._merge_body(detail)
        old = float(detail.get("bonusRate") or 0)
        body["bonusRate"] = round(old + delta, 4)
        body["id"] = detail.get("id")
        return self._req("POST", "submitEdit", body=body)

    @staticmethod
    def _merge_body(detail: dict[str, Any]) -> dict[str, Any]:
        """submitEdit 请求体：去掉只读/展示字段，只留可写字段。"""
        body = copy.deepcopy(detail)
        for key in list(body):
            if key in DROP_FIELDS or key.startswith("_"):
                body.pop(key, None)
        for cluster_key in ("adCluster", "mediaCluster"):
            cluster = body.get(cluster_key)
            if isinstance(cluster, dict) and isinstance(cluster.get("maxNum"), dict):
                cluster.pop("maxNum", None)
        ad = body.get("adCluster")
        if isinstance(ad, dict):
            ad.setdefault("industryVersion", "6.6")
        if not str(body.get("background") or "").strip():
            body["background"] = str(body.get("name") or "顶价系数调整")
        return body


def parse_dimensions(detail: dict[str, Any]) -> dict[str, Any]:
    """从策略 adCluster/mediaCluster 提取具体维度值，用于过滤监控范围。
    平台数据分隔符不可靠（尾逗号、逗号+空格），这里统一规范化为逗号连接。"""
    dims: dict[str, str] = {}
    for cluster_key in ("adCluster", "mediaCluster"):
        cluster = detail.get(cluster_key) or {}
        for field, (label, _129, _855) in DIMENSION_MAP.items():
            if field not in cluster:
                continue
            raw = str(cluster.get(field) or "").strip()
            vals = [v.strip() for v in raw.split(",") if v.strip()]
            vals = [v for v in vals if v.lower() not in ("all", "全部", "0")]
            if vals:
                dims[field] = ",".join(vals)
    return dims


def resolve_metric(name: str, dataset: int) -> tuple[int, str]:
    if name in METRIC_MAP:
        fid = METRIC_MAP[name][0 if dataset == DS_REALTIME else 1]
        if fid is None:
            raise ValueError(f"指标「{name}」在数据集 {dataset} 不存在")
        return fid, name
    if str(name).isdigit():
        return int(name), str(name)
    raise ValueError(f"不支持的指标「{name}」，请用三个默认指标名或 DS_REALTIME fieldId")


def window_ms(window: str) -> tuple[int, int]:
    now = datetime.now(TZ)
    if window == "today":
        # 实时集 DS_REALTIME：end 必须对齐整点。非整点 end 会触发服务端「新增列不支持查询」报错
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(minute=0, second=0, microsecond=0)
        if end <= start:
            raise ValueError("当天还没有完整小时数据，请稍后再试或用 last_hour")
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    if window == "yesterday":
        y = now - timedelta(days=1)
        start = y.replace(hour=0, minute=0, second=0, microsecond=0)
        end = y.replace(hour=23, minute=59, second=59, microsecond=999000)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    if window == "last_hour":
        # 上一个完整小时（小时对齐）：实时集 DS_REALTIME 按小时粒度取数，分小时监控用这个
        end = now.replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(hours=1)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    raise ValueError(f"window 仅支持 today / yesterday / last_hour，收到 {window}")


def olap_query(template_path: Path, dataset: int, dims: dict[str, str],
               start_ms: int, end_ms: int, referer: str) -> dict[str, Any]:
    template = json.loads(template_path.read_text(encoding="utf-8"))
    body = copy.deepcopy(template)
    if body.get("dataSetId") != dataset:
        raise RuntimeError(f"模板 dataSetId={body.get('dataSetId')} 与请求 {dataset} 不一致")
    body["queryTime"] = int(datetime.now(TZ).timestamp() * 1000)

    # 重写日期 filter（毫秒时间戳），并移除模板自带的非时间 filter（如短剧的转化目标）
    patched = 0
    keep_filters = []
    for flt in body.get("filters") or []:
        fv = flt.get("filterValue")
        if isinstance(fv, dict) and "startTime" in fv and "endTime" in fv:
            fv["startTime"] = start_ms
            fv["endTime"] = end_ms
            keep_filters.append(flt)
            patched += 1
        elif flt.get("filterType") == "datetime":
            keep_filters.append(flt)
    if patched == 0:
        raise RuntimeError("模板缺少日期 filter，拒绝执行以避免读抓包旧数据")

    # 追加策略维度 filter（账户ID 等由模板列本地过滤，其余维度走服务端 filter）
    for field, val in dims.items():
        if field == "accountId":
            continue  # 账户ID 已在模板行维度中，本地过滤
        name, fid129, _ = DIMENSION_MAP[field]
        keep_filters.append({
            "filterType": "string", "targetId": fid129, "urn": "", "name": name,
            "nickName": name, "type": "dimension", "valueType": "string",
            "filterValue": {"condition": "in", "value": val.split(","), "compareType": ""},
            "originalFilterValue": {"condition": "in", "value": val.split(",")},
            "selectType": "Multipul", "showType": "Dropdown", "disabled": False,
        })
    body["filters"] = keep_filters

    session = SmartAuthSession()
    resp = session.request(
        "POST", OLAP_URL, json=body,
        headers={"Accept": "application/json", "Kwaibi-View-Query-Type": "8",
                 "bi-request-module": "olap", "Referer": referer},
        timeout=180,
    )
    final_url = str(getattr(resp, "url", ""))
    ct = (resp.headers.get("Content-Type") or "").lower()
    if resp.status_code in (401, 302) or "login" in final_url.lower() or "sso" in final_url.lower() or "text/html" in ct:
        raise PermissionError("AUTH_EXPIRED")
    if resp.status_code == 403:
        raise PermissionError("AUTH_FAILED")
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data") or {}
    rows = data.get("dataList") or []
    columns = [c.get("name") for c in data.get("columns") or []]
    if not columns or not rows:
        raise RuntimeError(payload.get("error_msg") or "OLAP 返回空结果")
    return {"columns": columns, "rows": rows}


def kwaibi_query(dataset: int, metric_fid: int, metric_name: str, dims: dict[str, str],
                 start_ms: int, end_ms: int, group_key: str) -> dict[str, Any]:
    """group_key 是聚合维度（DIMENSION_MAP 键，如 accountId/appId）。
    媒体顶价策略没有 accountId，按 appId 分组拿媒体级汇总；否则返回全量账户行会
    被 rowLimit 截断（1000 行），聚合口径会错。"""
    glabel, g129, g855 = DIMENSION_MAP[group_key]
    gid = g129 if dataset == DS_REALTIME else g855
    fields = [
        {"id": gid, "name": glabel, "type": "dimension", "dataSetId": dataset},
        {"id": metric_fid, "name": metric_name, "type": "metric", "dataSetId": dataset},
    ]
    # 比值指标必须同步取联盟总消耗做加权（否则退化为算术平均，口径不对）
    if metric_name in RATIO_METRICS:
        cost_fid = METRIC_MAP["联盟总消耗"][0 if dataset == DS_REALTIME else 1]
        fields.append({"id": cost_fid, "name": "联盟总消耗", "type": "metric",
                       "dataSetId": dataset})
    # 注意：BI 的 {field,operator,value} 过滤格式会被服务端静默忽略，
    # 必须用 OLAP 风格 filter（filterType/targetId/filterValue），否则监控范围会扩大到全量账户。
    filters = []
    for field, val in dims.items():
        label, fid129, fid855 = DIMENSION_MAP[field]
        fid = fid129 if dataset == DS_REALTIME else fid855
        vals = [v for v in val.split(",") if v]
        filters.append({
            "filterType": "string", "targetId": fid, "urn": "", "name": label,
            "nickName": label, "type": "dimension", "valueType": "string",
            "filterValue": {"condition": "in", "value": vals, "compareType": ""},
            "originalFilterValue": {"condition": "in", "value": vals},
            "selectType": "Multipul", "showType": "Dropdown", "disabled": False,
        })
    body = {
        "dataSetId": dataset, "type": 1, "fields": fields, "filters": filters,
        "timeFilter": {"fieldName": "__time", "startTime": start_ms, "endTime": end_ms},
        "limit": 1000, "rowLimit": 1000,
    }
    session = SmartAuthSession()
    resp = session.request("POST", KWAIBI_URL, json=body,
                           headers={"Accept": "application/json"}, timeout=120)
    ct = (resp.headers.get("Content-Type") or "").lower()
    if resp.status_code in (401, 302) or "text/html" in ct:
        raise PermissionError("AUTH_EXPIRED")
    if resp.status_code == 403:
        raise PermissionError("AUTH_FAILED")
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data") or {}
    rows = data.get("dataList") or data.get("rows") or []
    columns = [c.get("name") for c in (data.get("columns") or [])]
    if not columns or not rows:
        err = str(payload.get("error_msg") or "")
        # error_msg="canDiff" 是正常返回字段（同期对比提示），真正的空结果就是没数据
        if err in ("", "canDiff"):
            raise RuntimeError("BI 返回空结果（策略账户在窗口内可能无数据）")
        raise RuntimeError(f"BI 查询失败: {err}")
    return {"columns": columns, "rows": rows}


def aggregate(columns: list[str], rows: list[list[Any]], metric_name: str,
              account_filter: set[str] | None, dim_col: str = "账户ID") -> dict[str, Any]:
    """按监控维度（账户或媒体）聚合，返回整体值与每维度明细。比值指标用消耗加权。"""
    if dim_col not in columns or metric_name not in columns:
        raise RuntimeError(f"响应缺少列：需要 {dim_col} 和 {metric_name}，实际 {columns}")
    i_dim = columns.index(dim_col)
    i_metric = columns.index(metric_name)
    i_cost = columns.index("联盟总消耗") if "联盟总消耗" in columns else None
    is_ratio = metric_name in RATIO_METRICS

    buckets: dict[str, dict[str, float]] = {}
    for row in rows:
        dimv = cell_text(row[i_dim]).strip()
        if not dimv:
            continue
        if account_filter is not None and dimv not in account_filter:
            continue
        m = raw_value(row[i_metric])
        cost = raw_value(row[i_cost]) if i_cost is not None else 0.0
        slot = buckets.setdefault(dimv, {"m": 0.0, "cost": 0.0, "sum": 0.0, "n": 0})
        slot["cost"] += cost
        if is_ratio:
            slot["m"] += cost * m
        else:
            slot["m"] += m
        slot["sum"] += m
        slot["n"] += 1

    items = []
    for dimv, slot in sorted(buckets.items()):
        if is_ratio:
            val = (slot["m"] / slot["cost"]) if slot["cost"] else (slot["sum"] / slot["n"] if slot["n"] else None)
        else:
            val = slot["m"]
        items.append({"dim": dimv, "metric": val, "cost": round(slot["cost"], 3)})

    if not items:
        raise RuntimeError("过滤后无任何数据（策略维度值可能不在数据集中或窗口内无数据）")

    total_cost = sum(a["cost"] for a in items)
    if is_ratio:
        weighted = sum((a["metric"] or 0) * a["cost"] for a in items)
        overall = (weighted / total_cost) if total_cost else (sum(a["metric"] or 0 for a in items) / len(items))
    else:
        overall = sum(a["metric"] or 0 for a in items)
    return {"overall": overall, "total_cost": round(total_cost, 3), "items": items}


def evaluate(value: float, op: str, threshold: float) -> bool:
    if op in (">",):
        return value > threshold
    if op in ("<",):
        return value < threshold
    if op in (">=",):
        return value >= threshold
    if op in ("<=",):
        return value <= threshold
    if op in ("=", "=="):
        return value == threshold
    if op in ("!=",):
        return value != threshold
    raise ValueError(f"不支持的比较符 {op}")


def reduce_action(client: FlowClient, detail: dict[str, Any], delta: float, floor: float,
                  dry_run: bool) -> dict[str, str] | None:
    """降系数动作；到封底（或封底高于现值）时返回 None 表示降无可降。"""
    old = float(detail.get("bonusRate") or 0)
    new = round(max(old - delta, floor), 4)
    if new >= old:
        return None
    action_id = build_action_id(detail, "reduce", old=old, new=new)
    if dry_run:
        return {"action_id": action_id, "op": "reduce",
                "desc": f"[dry-run] 降低系数 {delta}（{old} → {new}）"}
    client.reduce_coefficient(detail, delta, floor)
    return {"action_id": action_id, "op": "reduce",
            "desc": f"已降低系数 {delta}（{old} → {new}）"}


def raise_action(client: FlowClient, detail: dict[str, Any], delta: float,
                 dry_run: bool) -> dict[str, str] | None:
    """提系数动作（无上限，delta 需 > 0）。"""
    old = float(detail.get("bonusRate") or 0)
    new = round(old + delta, 4)
    if new <= old:
        return None
    action_id = build_action_id(detail, "raise", old=old, new=new)
    if dry_run:
        return {"action_id": action_id, "op": "raise",
                "desc": f"[dry-run] 提高系数 {delta}（{old} → {new}）"}
    client.raise_coefficient(detail, delta)
    return {"action_id": action_id, "op": "raise",
            "desc": f"已提高系数 {delta}（{old} → {new}）"}


def do_actions(client: FlowClient, detail: dict[str, Any], strategy: dict[str, Any],
               dry_run: bool) -> list[dict[str, str]]:
    """执行撤回/降系数，返回带 action_id 的动作清单。"""
    done: list[dict[str, str]] = []
    if strategy.get("withdraw"):
        action_id = build_action_id(detail, "withdraw")
        if dry_run:
            done.append({"action_id": action_id, "op": "withdraw",
                         "desc": f"[dry-run] 撤回策略 {detail['id']}"})
        else:
            client.withdraw(detail["id"])
            done.append({"action_id": action_id, "op": "withdraw",
                         "desc": f"已撤回策略 {detail['id']}"})
    delta = float(strategy.get("reduce_coefficient") or 0)
    if delta > 0:
        floor = float(strategy.get("floor", 1.0))
        act = reduce_action(client, detail, delta, floor, dry_run)
        if act is not None:
            done.append(act)
    return done


def evaluate_streak(client: FlowClient, detail: dict[str, Any], streak_cfg: dict[str, Any],
                    state: dict[str, Any], overall: float,
                    dry_run: bool) -> dict[str, Any]:
    """连续段规则：down 连续 N 小时超线降系数；up 为滚动窗口内累计达标次数提系数。
    动作后重新计数。state 由调用方持久化（同一策略同 key），本函数只改内存里的 dict。
    窗口无数据时调用方不调用本函数，计数保持不变。"""
    down = streak_cfg.get("down") or {}
    up = streak_cfg.get("up") or {}
    down_hours = int(down.get("hours", 4))
    down_op = str(down.get("op", ">"))
    down_th = float(down.get("threshold", 0.30))
    down_delta = float(down.get("delta", 0.10))
    down_floor = float(down.get("floor", 1.0))
    up_window = int(up.get("hours", 24))   # 滚动窗口小时数
    up_count = int(up.get("count", 5))     # 窗口内达标次数阈值
    up_op = str(up.get("op", "<="))
    up_th = float(up.get("threshold", 0.25))
    up_delta = float(up.get("delta", 0.1))

    down_hit = bool(down) and evaluate(overall, down_op, down_th)
    up_hit = bool(up) and evaluate(overall, up_op, up_th)

    d_streak = int(state.get("down_streak") or 0)
    if down_hit:
        d_streak += 1
    else:
        d_streak = 0

    # 提价段：滚动 24 小时窗口内累计达标小时数（不要求连续），同小时去重
    hour_start = int(datetime.now(TZ).replace(minute=0, second=0, microsecond=0).timestamp() * 1000)
    window_start = hour_start - up_window * 3600 * 1000
    up_hits = sorted({int(t) for t in (state.get("up_hits") or [])
                      if isinstance(t, (int, float)) and int(t) >= window_start})
    if up_hit:
        up_hits = sorted(set(up_hits + [hour_start]))
    u_count = len(up_hits)
    # 展示用：记录本轮判定后的计数（动作后重置不影响展示）
    reported_d, reported_u = d_streak, u_count

    actions: list[dict[str, str]] = []
    at_floor = False
    prev_floor_alerted = bool(state.get("floor_alerted"))
    if down_hit and d_streak >= down_hours:
        act = reduce_action(client, detail, down_delta, down_floor, dry_run)
        if act is not None:
            actions.append(act)
            d_streak = 0
            up_hits = []  # 动作后重新累计，防提/降来回震荡
        else:
            # 已到封底，保持连续计数（封顶在下限值），每轮继续提示人工介入
            at_floor = True
            d_streak = max(d_streak, down_hours)
    floor_first = at_floor and not prev_floor_alerted
    if up_hit and u_count >= up_count:
        act = raise_action(client, detail, up_delta, dry_run)
        if act is not None:
            actions.append(act)
            up_hits = []
            d_streak = 0

    state["down_streak"] = d_streak
    state["up_hits"] = up_hits
    state["floor_alerted"] = at_floor
    state["last_rate"] = round(float(overall), 6)
    state["updated_at"] = datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S%z")

    return {
        "down_hit": down_hit, "up_hit": up_hit, "at_floor": at_floor,
        "floor_first": floor_first,
        "down_streak": reported_d, "down_target": down_hours if down else 0,
        "up_streak": reported_u, "up_target": up_count if up else 0,
        "actions": actions,
    }


def build_action_id(detail: dict[str, Any], op: str, *, old: float | None = None,
                    new: float | None = None) -> str:
    """稳定 action_id：由规范化目标与动作参数派生，用于幂等回查。"""
    sid = detail.get("id")
    if op == "withdraw":
        return f"withdraw:{sid}"
    if op in ("reduce", "raise"):
        return f"{op}:{sid}:{old}->{new}"
    raise ValueError(f"不支持的动作类型 {op}")


def check_action(client: FlowClient, action_id: str) -> dict[str, Any]:
    """回查动作是否已执行。向权威数据源（boostControl get）核对。"""
    parts = action_id.split(":")
    if len(parts) < 2:
        return {"action_id": action_id, "known": False, "reason": "非法 action_id"}
    op, sid = parts[0], int(parts[1])
    try:
        detail = client.get(sid)
    except Exception as exc:
        return {"action_id": action_id, "known": False, "reason": str(exc)}
    if op == "withdraw":
        # 已撤回 = 不再处于生效中（status=4）。未执行撤回时一般仍是 status=4。
        applied = detail.get("status") != 4
        return {"action_id": action_id, "known": True, "applied": applied,
                "current_status": detail.get("status"),
                "current_statusDesc": detail.get("statusDesc")}
    if op in ("reduce", "raise"):
        # reduce/raise:<sid>:<old>-><new>，已执行 = 当前 bonusRate 已等于目标 new
        try:
            new = float(parts[-1].split("->")[-1])
        except (IndexError, ValueError):
            return {"action_id": action_id, "known": False, "reason": "action_id 格式错误"}
        cur = float(detail.get("bonusRate") or 0)
        return {"action_id": action_id, "known": True, "applied": abs(cur - new) < 1e-6,
                "current_bonusRate": cur, "target_bonusRate": new}
    return {"action_id": action_id, "known": False, "reason": f"未知动作 {op}"}


def process_one(strategy: dict[str, Any], cfg: dict[str, Any], dataset: int,
                referer: str, template_path: Path, client: FlowClient,
                dry_run: bool, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """处理单个策略：读→取数→判定→动作。可独立重试。"""
    sid = strategy.get("id")
    if sid is None:
        return {"id": None, "status": "INVALID", "error": "缺少策略 id"}
    detail = client.get(sid)
    stype = detail.get("type")
    if stype not in TOP_PRICE_TYPES:
        return {"id": sid, "name": detail.get("name"), "status": "SKIP_NOT_TOP_PRICE",
                "typeDesc": detail.get("typeDesc") or TYPE_NAMES.get(stype, stype)}

    metric_name = str(strategy.get("metric", "浅度超成本率"))
    metric_fid, metric_name = resolve_metric(metric_name, dataset)
    streak_cfg = strategy.get("streak")
    is_streak = isinstance(streak_cfg, dict) and (streak_cfg.get("down") or streak_cfg.get("up"))
    op = str(strategy.get("op", ">="))
    if not is_streak and op not in OPS:
        raise ValueError(f"不支持的比较符 {op}")
    threshold = float(strategy["threshold"]) if not is_streak else 0.0
    window = str(strategy.get("window", "today" if dataset == DS_REALTIME else "yesterday"))
    start_ms, end_ms = window_ms(window)
    dims = parse_dimensions(detail)
    # 监控维度：有 accountId 按账户分组；否则按媒体（appId）分组拿媒体级汇总。
    # 纯媒体顶价若按账户分组会撞 rowLimit 截断，聚合口径会错。
    account_filter = None
    if "accountId" in dims:
        group_key = "accountId"
        account_filter = {a for a in dims["accountId"].split(",")}
    elif "appId" in dims:
        group_key = "appId"
    else:
        group_key = "accountId"
    glabel, _, _ = DIMENSION_MAP[group_key]

    fetched = kwaibi_query(dataset, metric_fid, metric_name, dims, start_ms, end_ms, group_key)

    agg = aggregate(fetched["columns"], fetched["rows"], metric_name, account_filter, dim_col=glabel)
    streak_info: dict[str, Any] | None = None
    if is_streak:
        # 连续段模式：连续 N 小时满足条件才动作；计数持久化在 state 文件
        if state is None:
            state = {}
        entry = state.setdefault(str(sid), {})
        # dry-run 在副本上计算不落盘；正式轮次直接原地更新 entry（evaluate_streak 内部写 state）
        work = dict(entry) if dry_run else entry
        st = evaluate_streak(client, detail, streak_cfg, work, agg["overall"], dry_run)
        triggered = bool(st["actions"])
        actions = st["actions"]
        streak_info = {k: st[k] for k in ("down_hit", "down_streak", "down_target",
                                          "up_hit", "up_streak", "up_target", "at_floor",
                                          "floor_first")}
    else:
        triggered = evaluate(agg["overall"], op, threshold)
        actions: list[dict[str, str]] = []
        if triggered:
            try:
                actions = do_actions(client, detail, strategy, dry_run)
            except Exception as exc:
                actions = [{"action_id": "", "op": "error", "desc": f"动作执行失败: {exc}"}]

    result = {
        "id": sid, "name": detail.get("name"),
        "typeDesc": detail.get("typeDesc") or TYPE_NAMES.get(stype, str(stype)),
        "scopeName": SCOPE_NAMES.get(detail.get("scope"), str(detail.get("scope"))),
        "statusDesc": detail.get("statusDesc"),
        "group_dim": glabel,
        "metric": metric_name, "overall": agg["overall"], "total_cost": agg["total_cost"],
        "op": str(strategy.get("op", ">=")), "threshold": threshold,
        "triggered": triggered, "actions": actions,
        "dim_count": len(agg["items"]), "items": agg["items"][:20],
        "status": "TRIGGERED" if triggered else "OK",
    }
    if streak_info is not None:
        result["streak"] = streak_info
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="顶价扶持策略实时监控")
    parser.add_argument("--config", help="内联 JSON 配置")
    parser.add_argument("--config-file", help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只取数判规，不执行写操作")
    parser.add_argument("--item", type=int, help="只处理单个策略 ID（单项补跑/重试）")
    parser.add_argument("--concurrency", type=int, default=1, help="并发度（默认 1，串行）")
    parser.add_argument("--batch-size", type=int, default=0, help="每批处理数量（0=全部）")
    parser.add_argument("--max-runtime", type=int, default=0, help="最大运行秒数（0=不限）")
    parser.add_argument("--authoritative-source", default="boostControl",
                        help="幂等回查的权威数据源（固定 boostControl）")
    parser.add_argument("--check-action", default=None, help="回查指定 action_id 是否已执行")
    parser.add_argument("--state", default=".topbid-monitor-streak-state.json",
                        help="连续段计数状态文件（默认 cwd 下）")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    started = time.monotonic()

    # --check-action 幂等回查，不需要 config，先处理
    if args.check_action:
        client = FlowClient(args.timeout)
        try:
            res = check_action(client, args.check_action)
            emit({"ok": True, "code": 0, "msg": "回查完成", "data": res,
                  "next_action": {"type": "CONTINUE", "retryable": False},
                  "duration_ms": int((time.monotonic() - started) * 1000)})
            return 0
        except PermissionError as exc:
            emit({"ok": False, "code": 2, "error": str(exc),
                  "msg": "Auth 登录态失效或无接口权限",
                  "next_action": {"type": "ASK_USER", "retryable": True},
                  "duration_ms": int((time.monotonic() - started) * 1000)})
            return 2
        except Exception as exc:
            _fail("CHECK_ACTION_FAILED", str(exc), code=1, started=started)
            return 1

    try:
        cfg = load_config(args)
    except Exception as exc:
        _fail("INVALID_CONFIG", str(exc), code=1, started=started)
        return 1

    dataset = int(cfg.get("dataset", DS_REALTIME))
    referer = cfg.get("referer") or (
        "https://app.example.corp/main?enter_from=tianji&shareId=64385633&dataSetId=DS_REALTIME"
    )
    template_path = Path(__file__).resolve().parent.parent / "references" / "olap_query_template_realtime.json"

    client = FlowClient(args.timeout)

    strategies = cfg["strategies"]

    # 前置校验：配置错误要一次性报清楚，不要跑到每个策略里变 FAILED
    if not isinstance(strategies, list) or not strategies:
        _fail("INVALID_CONFIG", "strategies 必须是非空数组", code=1, started=started)
        return 1
    for i, s in enumerate(strategies):
        tag = f"strategies[{i}]"
        if s.get("id") is None:
            _fail("INVALID_CONFIG", f"{tag} 缺少 id", code=1, started=started)
            return 1
        streak_cfg = s.get("streak")
        has_streak = isinstance(streak_cfg, dict) and (streak_cfg.get("down") or streak_cfg.get("up"))
        if has_streak:
            for k in ("down", "up"):
                part = streak_cfg.get(k) or {}
                if not part:
                    continue
                if not isinstance(part.get("threshold"), (int, float)):
                    _fail("INVALID_CONFIG", f"{tag}（策略 {s['id']}）streak.{k} 缺少数字 threshold",
                          code=1, started=started)
                    return 1
                if str(part.get("op", ">" if k == "down" else "<")) not in OPS:
                    _fail("INVALID_CONFIG", f"{tag}（策略 {s['id']}）streak.{k}.op 不支持",
                          code=1, started=started)
                    return 1
                if int(part.get("hours", 0)) <= 0 or float(part.get("delta", 0)) <= 0:
                    _fail("INVALID_CONFIG", f"{tag}（策略 {s['id']}）streak.{k} 需要正数 hours 和 delta",
                          code=1, started=started)
                    return 1
                if k == "up" and int(part.get("count", 0)) <= 0:
                    _fail("INVALID_CONFIG", f"{tag}（策略 {s['id']}）streak.up 需要正数 count（窗口内达标次数阈值）",
                          code=1, started=started)
                    return 1
        else:
            if "threshold" not in s or not isinstance(s["threshold"], (int, float)):
                _fail("INVALID_CONFIG", f"{tag}（策略 {s['id']}）缺少数字 threshold",
                      code=1, started=started)
                return 1
            if str(s.get("op", ">=")) not in OPS:
                _fail("INVALID_CONFIG",
                      f"{tag}（策略 {s['id']}）op 不支持，可选 {sorted(OPS)}",
                      code=1, started=started)
                return 1
        try:
            resolve_metric(str(s.get("metric", "浅度超成本率")), dataset)
            window_ms(str(s.get("window", "today" if dataset == DS_REALTIME else "yesterday")))
        except Exception as exc:
            _fail("INVALID_CONFIG", f"{tag}（策略 {s['id']}）: {exc}",
                  code=1, started=started)
            return 1

    # 连续段计数状态：跨轮持久化（同 cwd 共享）。文件损坏时从零开始。
    state_path = Path(args.state)
    state: dict[str, Any] = {}
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("strategies"), dict):
                state = loaded
        except (ValueError, OSError):
            state = {}
    if "strategies" not in state:
        state["strategies"] = {}

    if args.item is not None:
        strategies = [s for s in strategies if s.get("id") == args.item]
        if not strategies:
            _fail("NOT_FOUND", f"配置里没有策略 {args.item}", code=1, started=started)
            return 1
    if args.batch_size > 0:
        strategies = strategies[: args.batch_size]

    results: list[dict[str, Any]] = []
    deadline = (time.monotonic() + args.max_runtime) if args.max_runtime > 0 else None
    for strategy in strategies:
        if deadline and time.monotonic() > deadline:
            break
        try:
            results.append(process_one(strategy, cfg, dataset, referer, template_path,
                                       client, args.dry_run, state["strategies"]))
        except PermissionError as exc:
            emit({"ok": False, "code": 2, "error": str(exc),
                  "msg": "Auth 登录态失效或无接口权限",
                  "next_action": {"type": "ASK_USER", "retryable": True},
                  "duration_ms": int((time.monotonic() - started) * 1000)})
            return 2
        except Exception as exc:
            results.append({"id": strategy.get("id"), "status": "FAILED",
                            "error": f"{type(exc).__name__}: {exc}"})

    # 人读摘要（stderr/前面，不破坏最后一行 JSON）
    for r in results:
        if r.get("status") in ("FAILED", "READ_FAILED", "QUERY_FAILED"):
            print(f"策略 {r.get('id')} {r.get('status')}: {r.get('error')}", file=sys.stderr)
        elif r.get("status") == "SKIP_NOT_TOP_PRICE":
            print(f"策略 {r['id']} 非顶价策略（{r.get('typeDesc')}），跳过")
        elif r.get("status") == "INVALID":
            print(f"配置项缺少 id，跳过", file=sys.stderr)
        else:
            act = "; ".join(a.get("desc", "") for a in r["actions"]) or "无动作"
            if r.get("streak"):
                st = r["streak"]
                extra = (f" | 超线连续 {st['down_streak']}/{st['down_target']}h，"
                         f"窗口内达标 {st['up_streak']}/{st['up_target']} 次")
                if st.get("at_floor"):
                    extra += "（已到封底）"
            else:
                extra = (f" {r['op']}{r['threshold']} → "
                         f"{'触发' if r['triggered'] else '未触发'}")
            print(f"策略 {r['id']} [{r['scopeName']}] {r['metric']}={r['overall']:.4f}"
                  f"{extra} | {act}")

    # 连续段计数状态落盘（含本轮已处理的策略；失败/无数据的策略保持原状）
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except OSError as exc:
        print(f"警告：状态文件写入失败 {exc}", file=sys.stderr)

    emit({"ok": True, "code": 0, "msg": "监控完成", "data": {"results": results, "dry_run": args.dry_run},
          "next_action": {"type": "CONTINUE", "retryable": False},
          "duration_ms": int((time.monotonic() - started) * 1000)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
