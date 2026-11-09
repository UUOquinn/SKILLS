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

"""策略延期脚本：抽 ID → GET 详情 → endTime +1月 → 直连策略平台 API 提审

Normal 模式：--item <id>      单条处理
Batch  模式：--ids  <id,...>  串行编排，带检查点与汇总
"""

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from typing import Optional, Tuple

from ks_aimate.sso_login_client import SmartSSOSession

# ── 策略平台基址 ───────────────────────────────────────────────────────────────
OPERATION_BASE = "https://ops-platform.example.corp/ops/rest"

API_PREFIXES = [
    "orientControl",
    "flowControl",
    "darkControl",
    "mediaControl",
    "innerControl",
    "shareRatioControl",
]

# 定向策略用 mergeEditV2，其余用 mergeEdit
MERGE_EDIT_V2_PREFIXES = {"orientControl"}

# 定向提交白名单字段
ORIENT_SUBMIT_FIELDS = {
    "id", "name", "type", "background", "shieldType", "shieldMediaType",
    "shieldUserType", "beginTime", "endTime", "adCluster", "mediaCluster",
}

# 行业版本号：提交时必须写入 adCluster.industryVersion，否则服务端返回
# 412「行业版本系统升级，请刷新页面重试」。GET 详情不返回此字段，也没有
# 单独的版本查询接口，只能本地维护。平台行业版本升级时需同步修此常量。
INDUSTRY_VERSION = "6.6"


def unwrap_versioned(value):
    """删除策略平台 GET 返回中无法回传的抽象版本化字段（如 adCluster.maxNum）。

    GET 接口会将服务端 Kconf<T> 抽象类型序列化为：
        {"versioned": {"version": 0, "data": <真实值>}, "inited": true}
    实测确认：无论回传裸值还是原结构，mergeEdit/mergeEditV2 提交接口都无法反序
    列列化 `com.kuaishou.kconf.client.Kconf` 这个抽象类型（报 InvalidDefinitionException /
    500）。因此策略层面无法修复，唯一安全做法是提交时将该字段整个删除，依靠服务端
    mergeEdit 对未传字段保留原值的行为（未传 = 不改，非提交为空）。
    识别特征：字典并且同时含有 "versioned" 与 "inited" 两个 key 时，返回 特定占位
    ``_DROP_FIELD_`` 表示需要删除字段（由上层 dict/list 遍历处理实际删除）。
    """
    if isinstance(value, dict):
        if "versioned" in value and "inited" in value:
            return "_DROP_FIELD_"
        cleaned = {}
        for k, v in value.items():
            nv = unwrap_versioned(v)
            if nv == "_DROP_FIELD_":
                continue  # 删除无法回传的字段
            cleaned[k] = nv
        return cleaned
    if isinstance(value, list):
        return [unwrap_versioned(v) for v in value if unwrap_versioned(v) != "_DROP_FIELD_"]
    return value


# ── 输出契约 ──────────────────────────────────────────────────────────────────

_START_MS = int(time.time() * 1000)


def _out(ok: bool, code: int, data=None, error=None, msg=None,
         next_action_type="CONTINUE"):
    payload = {"ok": ok, "code": code,
               "next_action": {"type": next_action_type, "retryable": False},
               "duration_ms": int(time.time() * 1000) - _START_MS}
    if data is not None:
        payload["data"] = data
    if error:
        payload["error"] = error
    if msg:
        payload["msg"] = msg
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


# ── 时间工具 ──────────────────────────────────────────────────────────────────

def add_one_month(ts_ms: int) -> int:
    import calendar
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    month = dt.month + 1
    year = dt.year
    if month > 12:
        month = 1
        year += 1
    max_day = calendar.monthrange(year, month)[1]
    new_dt = dt.replace(year=year, month=month, day=min(dt.day, max_day))
    return int(new_dt.timestamp() * 1000)


def fmt_ts(ms: Optional[int]) -> str:
    if not ms:
        return "—"
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return str(ms)


# ── stable action_id ──────────────────────────────────────────────────────────

def make_action_id(strategy_id: int, new_end_ms: int) -> str:
    raw = f"postpone:{strategy_id}:{new_end_ms}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ── 提交体净化 ────────────────────────────────────────────────────────────────

def sanitize_body(detail: dict, api_prefix: str, new_end: int) -> dict:
    body = dict(detail)
    body["endTime"] = new_end
    for key in ("status", "createTime", "toolBar", "updateTime", "creatorId"):
        body.pop(key, None)
    if api_prefix in MERGE_EDIT_V2_PREFIXES:
        body = {k: v for k, v in body.items() if k in ORIENT_SUBMIT_FIELDS}
    # 删除 adCluster/mediaCluster 下无法回传的 Kconf 抽象类型字段（如 maxNum）
    body = unwrap_versioned(body)
    # 行业版本号：不带则 412
    if isinstance(body.get("adCluster"), dict):
        body["adCluster"]["industryVersion"] = INDUSTRY_VERSION
    # background 为空时补上，避免必填校验失败
    if not body.get("background"):
        body["background"] = body.get("name") or "延期续期"
    return body


# ── 策略平台客户端（直连）────────────────────────────────────────────────────

class OperationClient:
    def __init__(self):
        self.session = SmartSSOSession()

    def _get(self, url: str, params: dict) -> dict:
        resp = self.session.request("GET", url, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, url: str, body: dict) -> dict:
        resp = self.session.request("POST", url, json=body)
        resp.raise_for_status()
        return resp.json()

    def get_strategy(self, strategy_id: int) -> Optional[Tuple[dict, str]]:
        """多类型探测，返回 (detail, api_prefix) 或 None。"""
        for prefix in API_PREFIXES:
            try:
                resp = self._get(f"{OPERATION_BASE}/{prefix}/get",
                                 {"id": strategy_id})
                if resp.get("status") == 200 and resp.get("data"):
                    return resp["data"], prefix
            except Exception:
                continue
        return None

    def submit_strategy(self, api_prefix: str, body: dict) -> dict:
        action = "mergeEditV2" if api_prefix in MERGE_EDIT_V2_PREFIXES else "mergeEdit"
        return self._post(f"{OPERATION_BASE}/{api_prefix}/{action}", body)

    def check_action(self, strategy_id: int) -> dict:
        """回查策略当前状态（用于 --check-action 幂等验证）。"""
        result = self.get_strategy(strategy_id)
        if result is None:
            return {"found": False, "reason": "STRATEGY_NOT_FOUND"}
        detail, prefix = result
        return {
            "found": True,
            "api_prefix": prefix,
            "current_end": fmt_ts(detail.get("endTime")),
            "status": detail.get("status"),
        }


# ── 单条处理 ──────────────────────────────────────────────────────────────────

def process_one(client: OperationClient, strategy_id: int, dry_run: bool) -> dict:
    now_ms = int(time.time() * 1000)

    result = client.get_strategy(strategy_id)
    if result is None:
        return {"id": strategy_id, "status": "失败",
                "reason": "STRATEGY_NOT_FOUND", "action_id": None}

    detail, api_prefix = result
    begin_time = detail.get("beginTime", 0)
    end_time = detail.get("endTime", 0)
    name = detail.get("name", "")

    base = max(end_time, now_ms)
    new_end = add_one_month(base)
    action_id = make_action_id(strategy_id, new_end)

    if new_end <= begin_time:
        return {
            "id": strategy_id, "name": name, "api_prefix": api_prefix,
            "old_end": fmt_ts(end_time), "new_end": fmt_ts(new_end),
            "status": "跳过", "reason": "新结束时间 <= 开始时间",
            "action_id": action_id,
        }

    if dry_run:
        return {
            "id": strategy_id, "name": name, "api_prefix": api_prefix,
            "old_end": fmt_ts(end_time), "new_end": fmt_ts(new_end),
            "status": "DRY_RUN（未提交）", "reason": "",
            "action_id": action_id,
        }

    body = sanitize_body(detail, api_prefix, new_end)

    for attempt in range(2):
        try:
            resp = client.submit_strategy(api_prefix, body)
            biz_status = resp.get("status") or resp.get("code")
            if biz_status in (200, 0):
                return {
                    "id": strategy_id, "name": name, "api_prefix": api_prefix,
                    "old_end": fmt_ts(end_time), "new_end": fmt_ts(new_end),
                    "status": "成功", "reason": "", "action_id": action_id,
                }
            if biz_status == 412:
                return {
                    "id": strategy_id, "name": name, "api_prefix": api_prefix,
                    "old_end": fmt_ts(end_time), "new_end": fmt_ts(new_end),
                    "status": "失败",
                    "reason": resp.get("message", "行业版本校验失败 412（API 无法绕过）"),
                    "action_id": action_id,
                }
            if attempt == 0:
                continue
            return {
                "id": strategy_id, "name": name, "api_prefix": api_prefix,
                "old_end": fmt_ts(end_time), "new_end": fmt_ts(new_end),
                "status": "失败", "reason": str(resp), "action_id": action_id,
            }
        except Exception as e:
            if attempt == 1:
                return {
                    "id": strategy_id, "name": name, "api_prefix": api_prefix,
                    "old_end": fmt_ts(end_time), "new_end": fmt_ts(new_end),
                    "status": "失败", "reason": str(e), "action_id": action_id,
                }

    return {"id": strategy_id, "status": "失败", "reason": "未知错误",
            "action_id": action_id}


# ── 结果表格渲染（模板写死在脚本内）───────────────────────────────────────────

def render_summary_table(results):
    """把延期结果渲染成固定模板表格（制表符分隔）。

    模板：
        策略\t原结束\t新结束
        <id> <名称>\t<原结束>\t<新结束>
    非成功条目在「新结束」后追加「状态：原因」备注，便于排查。
    """
    lines = ["策略\t原结束\t新结束"]
    for r in results:
        sid = r.get("id", "")
        name = (r.get("name") or "").strip()
        label = f"{sid} {name}".strip()
        old_end = r.get("old_end") or "—"
        new_end = r.get("new_end") or "—"
        status = r.get("status") or ""
        if status == "成功":
            note = ""
        else:
            reason = (r.get("reason") or "").strip()
            note = f"\t{status}" + (f"：{reason}" if reason else "")
        lines.append(f"{label}\t{old_end}\t{new_end}{note}")
    return "\n".join(lines)


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="策略延期脚本（直连策略平台 API）")
    parser.add_argument("--ids", help="逗号分隔的策略 ID 列表（Batch 模式）")
    parser.add_argument("--item", type=int, help="单条处理（Normal 模式）")
    parser.add_argument("--dry-run", action="store_true", help="只查询不提交")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="并发度，默认 1（串行）")
    parser.add_argument("--batch-size", type=int, default=10,
                        help="每批次处理数量，默认 10")
    parser.add_argument("--max-runtime", type=int, default=300,
                        help="最大运行时间（秒），默认 300")
    parser.add_argument("--authoritative-source", default="ops-platform",
                        help="权威数据源，默认 ops-platform")
    parser.add_argument("--check-action", metavar="ACTION_ID",
                        help="回查指定 action_id 是否已执行")
    args = parser.parse_args()

    client = OperationClient()

    # --check-action 回查模式
    if args.check_action:
        sid = args.item or (
            int(args.ids.split(",")[0].strip()) if args.ids else None)
        if not sid:
            _out(False, 1, error="INVALID_IDS",
                 msg="--check-action 需要同时提供 --item 或 --ids",
                 next_action_type="STOP")
        result = client.check_action(sid)
        _out(True, 0, data={"check_result": result,
                             "action_id": args.check_action})

    # 构建 ID 列表
    if args.item:
        id_list = [args.item]
    elif args.ids:
        id_list = [int(x.strip()) for x in args.ids.split(",")
                   if x.strip().isdigit()]
    else:
        _out(False, 1, error="INVALID_IDS",
             msg="请提供 --ids 或 --item 参数", next_action_type="STOP")

    if not id_list:
        _out(False, 1, error="INVALID_IDS",
             msg="未解析到有效策略 ID", next_action_type="STOP")

    if len(id_list) > 50:
        _out(False, 1, error="EXCEEDS_MAX_ITEMS",
             msg=f"ID 数量 {len(id_list)} 超过上限 50，请拆分后分批提交",
             next_action_type="STOP")

    start = time.time()
    results = []
    for sid in id_list:
        if time.time() - start > args.max_runtime:
            results.append({"id": sid, "status": "跳过",
                             "reason": "超过 max_runtime"})
            continue
        r = process_one(client, sid, args.dry_run)
        results.append(r)

    # 固定模板表格输出（人类可读，便于 AI 直接转发，无需自行拼装）
    print(render_summary_table(results))
    print()

    _out(True, 0, data={
        "results": results,
        "authoritative_source": args.authoritative_source,
        "batch_size": args.batch_size,
    })


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _out(False, 130, error="SIGINT", msg="用户中断", next_action_type="STOP")
    except Exception as e:
        import traceback
        print(traceback.format_exc(), file=sys.stderr)
        _out(False, 1, error="UNEXPECTED_ERROR", msg=str(e),
             next_action_type="STOP")
