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
"""读取 boostControl 顶价策略，输出类型/scope/监控维度（只读）。

用法：
  uv run <skill_directory>/scripts/inspect_strategy.py --ids 27123,27124

输出 stdout 最后一行是业务 JSON，前面可有人读摘要。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from auth_helper.sso_login_client import SmartAuthSession

BASE_URL = "https://ops.example.corp/ops/rest/boostControl"
TOP_PRICE_TYPES = {3: "顶价", 5: "CPA顶价"}
# get 接口对非顶价策略的 typeDesc 可能返回空串，这里提供兜底名
TYPE_NAMES = {1: "绿通", 3: "顶价", 4: "打折", 5: "CPA顶价", 10: "索引加白"}
SCOPE_NAMES = {1: "广告主顶价", 2: "媒体顶价", 3: "广告主*媒体顶价", 0: "未指定(默认)"}

# 策略字段 -> OLAP 维度名（用于告诉用户监控会按哪些维度过滤/拆分）
DIM_ORDER = [
    ("adCluster", "accountId", "账户ID(广告主)"),
    ("adCluster", "campaignType", "营销目标(推广目的)"),
    ("adCluster", "ocpxActionType", "浅度优化目标(转化目标)"),
    ("adCluster", "deepConversionType", "深度优化目标"),
    ("adCluster", "secondIndustryV6", "二级行业"),
    ("adCluster", "product", "产品"),
    ("mediaCluster", "appId", "联盟媒体ID"),
    ("mediaCluster", "uid", "开发者id(媒体用户)"),
    ("mediaCluster", "posId", "广告位"),
    ("mediaCluster", "adStyle", "广告样式"),
]


def emit(payload: dict[str, Any], exit_code: int = 0) -> None:
    payload.setdefault("duration_ms", 0)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(exit_code)


def parse_ids(raw: str) -> list[int]:
    out: list[int] = []
    for chunk in raw.replace("，", ",").replace(";", ",").split(","):
        token = chunk.strip()
        if not token:
            continue
        if not token.isdigit():
            raise ValueError(f"非法策略 ID：{token}")
        if int(token) not in out:
            out.append(int(token))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="读取 boostControl 顶价策略维度")
    parser.add_argument("--ids", required=True, help="逗号分隔的策略 ID")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    started = time.monotonic()
    try:
        ids = parse_ids(args.ids)
    except ValueError as exc:
        emit({"ok": False, "code": 1, "error": "INVALID_ARGUMENT", "msg": str(exc),
              "next_action": {"type": "STOP", "retryable": False},
              "duration_ms": int((time.monotonic() - started) * 1000)})
        return 1

    session = SmartAuthSession()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://ops.example.corp",
        "Referer": "https://ops.example.corp/flow",
    }

    results: list[dict[str, Any]] = []
    for sid in ids:
        try:
            resp = session.request(
                "GET", f"{BASE_URL}/get", params={"id": sid},
                headers=headers, timeout=args.timeout, allow_redirects=False,
            )
        except Exception as exc:
            results.append({"id": sid, "error": "NETWORK_ERROR", "msg": str(exc)})
            continue
        if resp.status_code in (301, 302, 307, 308, 401):
            emit({"ok": False, "code": 2, "error": "AUTH_EXPIRED",
                  "msg": "Auth 登录态失效，请在内网浏览器访问任意 example.corp 页面完成认证后重试",
                  "next_action": {"type": "ASK_USER", "retryable": True},
                  "duration_ms": int((time.monotonic() - started) * 1000)})
            return 2
        if resp.status_code == 403:
            emit({"ok": False, "code": 2, "error": "AUTH_FAILED",
                  "msg": "当前账号无 boostControl 接口权限",
                  "next_action": {"type": "ASK_USER", "retryable": False},
                  "duration_ms": int((time.monotonic() - started) * 1000)})
            return 2
        if resp.status_code >= 400:
            results.append({"id": sid, "error": f"API_{resp.status_code}",
                            "msg": resp.text[:200]})
            continue
        try:
            data = resp.json()
        except ValueError:
            results.append({"id": sid, "error": "INVALID_RESPONSE",
                            "msg": resp.text[:200]})
            continue
        envelope = data if isinstance(data, dict) else {}
        detail = envelope.get("data", envelope)
        if not isinstance(detail, dict) or "id" not in detail:
            results.append({"id": sid, "error": "NOT_FOUND", "msg": "未找到该策略"})
            continue

        stype = detail.get("type")
        scope = detail.get("scope")
        dims = []
        for cluster_key, field, label in DIM_ORDER:
            cluster = detail.get(cluster_key) or {}
            val = str(cluster.get(field) or "").strip()
            if val and val.lower() not in ("all", "全部"):
                dims.append({"cluster": cluster_key, "field": field,
                             "label": label, "values": val})
        results.append({
            "id": sid,
            "name": detail.get("name"),
            "type": stype,
            "typeDesc": detail.get("typeDesc") or TYPE_NAMES.get(stype, str(stype)),
            "scope": scope,
            "scopeName": SCOPE_NAMES.get(scope, f"其他({scope})"),
            "is_top_price": stype in TOP_PRICE_TYPES,
            "bonusRate": detail.get("bonusRate"),
            "status": detail.get("status"),
            "statusDesc": detail.get("statusDesc"),
            "dimensions": dims,
        })

    # 人读摘要（非 JSON，不破坏最后一行契约）
    for r in results:
        if "error" in r:
            print(f"策略 {r['id']} 读取失败: {r['msg']}", file=sys.stderr)
            continue
        dim_text = "; ".join(f"{d['label']}={d['values']}" for d in r["dimensions"]) or "无"
        print(f"策略 {r['id']} [{r['typeDesc']}|{r['scopeName']}] bonusRate={r['bonusRate']} | {dim_text}")

    emit({"ok": True, "code": 0, "msg": "读取完成", "data": {"strategies": results},
          "next_action": {"type": "CONTINUE", "retryable": False},
          "duration_ms": int((time.monotonic() - started) * 1000)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
