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

"""联盟审核 CLI：联盟诊断工作台 /api/strategy/audit/* 接口封装。

stdout 最后一行输出标准业务 JSON；进程退出码与 JSON code 一致。
"""

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from auth_helper.sso_login_client import SmartAuthSession

BASE_URL = "https://ops.example.corp/ops/rest"

STATUS_DESC = {
    1: "待审核", 2: "待发布", 3: "审核中",
    4: "生效中", 5: "已撤回", 6: "已结束", 7: "草稿",
}

ACTION_CFG = {
    "check_pass": {"code": 6, "label": "审核通过"},
    "check_fail": {"code": 3, "label": "审核驳回"},
    "publish_pass": {"code": 2, "label": "同意发布"},
    "publish_fail": {"code": 7, "label": "拒绝发布"},
    "ban_pass": {"code": 13, "label": "封禁期通过"},
    "ban_fail": {"code": 14, "label": "封禁期驳回"},
}

CREATOR_ACTION_TO_STATUS_KEY = {
    "check_pass": "check_pass",
    "publish_pass": "publish_pass",
    "check_fail": "check_fail",
    "publish_fail": "publish_fail",
}


class AuditClient:
    def __init__(self):
        self.session = SmartAuthSession()
        self.base = BASE_URL

    def _request(self, method, path, body=None):
        url = self.base + path
        if method == "GET":
            resp = self.session.request("GET", url, params=body or {})
        else:
            resp = self.session.request("POST", url, json=body or {})
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            raise RuntimeError("响应不是 JSON: %s" % resp.text[:200])



# 审核单/详情探测的唯一顺序，遵循联盟审核状态机约定。
PREFIXES = ["targetingControl", "boostControl", "shadowControl", "placementControl", "internalControl", "shareControl"]
# 立即推全接口只允许这三类策略，其他前缀即使有详情也不可写推全。
PUSH_PREFIXES = {"targetingControl", "shadowControl", "boostControl"}
WORKABLE_AUDIT_STATUSES = (1, 6, 12)

def unwrap(payload):
    if not isinstance(payload, dict):
        raise RuntimeError("响应格式异常: %r" % (payload,))
    # ops-platform 格式: {status: 200/10051, message: "...", data: ...}
    if "status" in payload and payload["status"] != 200:
        raise RuntimeError(str(payload.get("message") or "未知错误"))
    if "data" in payload:
        return payload["data"]
    return payload

def detect_prefix_and_get(client, sid):
    """遍历 4 个命名空间探测策略详情；全部未命中时显式返回 (None, None)，
    不留隐式 None，避免调用处两值解包炸出不可读的 TypeError。"""
    for prefix in PREFIXES:
        try:
            resp = client.session.request("GET", f"{client.base}/{prefix}/get", params={"id": sid})
            if resp.status_code == 200:
                body = resp.json()
                if body.get("status") == 200 and body.get("data") and "id" in body["data"]:
                    return prefix, body["data"]
        except Exception:
            pass
    return None, None


def query_one(client, sid):
    prefix, data = detect_prefix_and_get(client, sid)
    if data is None:
        return {
            "id": sid, "name": None, "status": None, "statusDesc": "未找到",
            "creatorId": None, "fields": {}, "ok": False,
            "message": "策略 #%s 查询失败：%s 均未命中，请确认 ID 是否正确、是否已被删除、或是否为联盟广告策略" % (sid, "/".join(PREFIXES)), 
        }
    status = data.get("status")
    fields = {k: v for k, v in data.items() if v is not None}
    fields["_prefix"] = prefix
    return {
        "id": sid, "name": data.get("name") or data.get("ruleName"), "status": status,
        "statusDesc": data.get("statusDesc") or STATUS_DESC.get(status, "未知"),
        "creatorId": data.get("creatorId") or data.get("creatorName"), "fields": fields, "ok": True, "message": "查询成功",
    }


# 审核单内嵌的策略 control 块。`boostControl.typeDesc`（打折判据）只在这些块里出现：
# `/{prefix}/get` 返回的策略详情不含 typeDesc，只有裸 type，无法据此判定。
CONTROL_KEYS = ("boostControl", "targetingControl", "shadowControl", "placementControl", "internalControl", "shareControl")

# 单会话内缓存「策略 ID -> 审核单列表」，避免同一策略在判定与展示时重复请求审核单。
_APPROVE_CACHE = {}


def fetch_approve_records(client, sid):
    """拉取策略的审核单列表（一次请求，会话内缓存）。

    `/approve/query` 的响应里每条审核单内嵌一个 control 块，形如：
        records[].boostControl = {"id": <策略ID>, "name": ..., "type": 4, "typeDesc": "打折"}

    两个易错点：
      1. 块内 `id` 是**策略 ID**；审核单自身 ID 是 `records[].id`（形如 49043），两者不同。
      2. **同一数字 ID 可同时存在于多个命名空间**——实测策略 15091 在 boostControl
         （顶价）、targetingControl（新屏蔽）、shadowControl（暗投控比）下各有同名策略。
         因此 `typeDesc` 必须与它所在的命名空间一起理解，不能跨命名空间混用。

    取不到审核单返回空列表，调用方须按「未知」处理，不得当作放行依据。
    """
    if sid in _APPROVE_CACHE:
        return _APPROVE_CACHE[sid]
    records = []
    try:
        resp = client.session.request(
            "POST", f"{client.base}/approve/query",
            json={"ruleId": sid, "pager": {"pageNum": 1, "pageSize": 10}})
        body = resp.json()
        if body.get("status") == 200:
            records = (body.get("data") or {}).get("data") or []
    except Exception:
        records = []
    _APPROVE_CACHE[sid] = records
    return records


def fetch_control_block(client, sid, only_key=None):
    """取审核单内嵌的 control 块，返回首个 `control.id == sid` 的块。

    `only_key` 可限定只在某个命名空间（如 boostControl）内查找。
    找不到（或该策略尚未提交审核）时返回的 typeDesc 为 None，调用方按「未知」处理。
    """
    empty = {"typeDesc": None, "controlKey": None, "controlType": None,
             "approveId": None, "approveStatus": None, "ruleName": None}
    keys = (only_key,) if only_key else CONTROL_KEYS
    for rec in fetch_approve_records(client, sid):
        for key in keys:
            block = rec.get(key)
            if not isinstance(block, dict) or not block.get("typeDesc"):
                continue
            try:
                if int(block.get("id")) != int(sid):
                    continue
            except (TypeError, ValueError):
                continue
            return {
                "typeDesc": block.get("typeDesc"), "controlKey": key,
                "controlType": block.get("type"), "approveId": rec.get("id"),
                "approveStatus": rec.get("status"),
                "ruleName": rec.get("ruleName") or block.get("name"),
            }
    return empty


def strategy_type_desc(snapshot, client):
    """策略的 boostControl.typeDesc（拿不到则为 None），用于结果模板展示。"""
    block = fetch_control_block(client, snapshot.get("id"), only_key="boostControl")
    if block.get("typeDesc") is None:
        # boostControl 下没有该 id（如纯屏蔽类策略），退回任意命名空间供展示。
        block = fetch_control_block(client, snapshot.get("id"))
    return block.get("typeDesc")


def is_deferred_invalid(snapshot):
    """status=5 仅在详情明确带有延期/延期提审信息时按可强制审核候选处理。"""
    if snapshot.get("status") != 5:
        return False
    text = json.dumps(snapshot.get("fields") or {}, ensure_ascii=False).lower()
    return any(word in text for word in ("延期", "defer", "extend", "postpone"))


def is_discount_strategy(snapshot, client):
    """唯一判据：boostControl.typeDesc == 打折（精确相等），禁止按名称/备注模糊猜测。

    必须回查审核单：策略详情接口（`/{prefix}/get`）不返回 typeDesc，只按详情判断
    会永远得到 False，导致打折策略被放行。

    只读 boostControl 命名空间——用户规则明确限定 `boostControl.typeDesc`，
    不从 targetingControl/shadowControl 等其它块推断。

    为硬排除安全，扫描该策略**所有**审核单的 boostControl 块：任一为「打折」即命中。
    拿不到审核单时返回 False 并置 `_discountUnknown` 标记，调用方按「未知」处理。
    """
    sid = snapshot.get("id")
    for rec in fetch_approve_records(client, sid):
        block = rec.get("boostControl")
        if not isinstance(block, dict) or not block.get("typeDesc"):
            continue
        try:
            if int(block.get("id")) != int(sid):
                continue
        except (TypeError, ValueError):
            continue
        if block.get("typeDesc") == "打折":
            snapshot["_boostControlBlock"] = {
                "typeDesc": block.get("typeDesc"), "controlKey": "boostControl",
                "controlType": block.get("type"), "approveId": rec.get("id"),
                "approveStatus": rec.get("status"),
                "ruleName": rec.get("ruleName") or block.get("name"),
            }
            snapshot["_discountUnknown"] = False
            return True
    snapshot["_discountUnknown"] = True
    return False


def skipped_discount_item(sid, snapshot, auditor=None):
    block = snapshot.get("_boostControlBlock") or {}
    return {
        "id": sid,
        "ok": True,
        "skipped": True,
        "skipReason": "DISCOUNT_FLOWCONTROL_TYPEDESC",
        "message": "已跳过：boostControl.typeDesc=打折，禁止审核",
        "auditor": auditor or None,
        "strategyTypeDesc": block.get("typeDesc") or "打折",
        "strategy": snapshot,
    }



def query_approve_records(client, sid=None, status=None, approve_id=None, creator_id=None):
    """审核单定位的唯一入口；只发送用户定义的 query 字段。"""
    body = {"pager": {"pageNum": 1, "pageSize": 50}}
    if sid is not None:
        body["ruleId"] = int(sid)
    if status is not None:
        body["status"] = int(status)
    if approve_id is not None:
        body["id"] = int(approve_id)
    if creator_id is not None:
        body["creatorId"] = int(creator_id)
    resp = client.session.request("POST", f"{client.base}/approve/query", json=body)
    data = resp.json()
    if data.get("status") != 200:
        return []
    return (data.get("data") or {}).get("data") or []


def locate_approve_record(client, sid):
    """仅定位 status=1/6/12 三种允许流程，按最新单据选取。"""
    for status in WORKABLE_AUDIT_STATUSES:
        records = query_approve_records(client, sid=sid, status=status)
        if records:
            return sorted(records, key=lambda r: (r.get("updateTime") or 0, r.get("createTime") or 0), reverse=True)[0]
    return None


def _reason(reason):
    return reason or "联盟审核自动流转"


def change_status(client, approve_id, status, reason, confirm=False):
    body = {"id": int(approve_id), "status": int(status), "reason": _reason(reason)}
    params = None
    if confirm:
        body["confirm"] = True
        params = {"confirm": "true"}
    resp = client.session.request("POST", f"{client.base}/approve/changeStatus", params=params, json=body)
    data = resp.json()
    return data, (data.get("message") or "状态流转失败")


def _needs_ban_confirm(message):
    text = str(message or "")
    return "封禁期" in text and ("是否确认" in text or "需要进行人工审核" in text)


def _published_idempotent(message):
    text = str(message or "")
    return ("无法应用于状态[发布成功]" in text or
            ("无法应用于状态[审核成功]" in text and "发布成功" in text))


def _toolbar_value(source, field):
    if not isinstance(source, dict):
        return None
    if isinstance(source.get(field), bool):
        return source[field]
    toolbar = source.get("toolBar")
    if isinstance(toolbar, dict) and isinstance(toolbar.get(field), bool):
        return toolbar[field]
    if isinstance(toolbar, list):
        for entry in toolbar:
            if isinstance(entry, dict) and isinstance(entry.get(field), bool):
                return entry[field]
    return None


def read_push_gate(client, sid, approve_id, prohibition=False):
    """读取推全门闩；get 无按钮时仅用当前审核单内嵌 toolBar 补读。"""
    field = "displayProhibitionPeriodQuickPushBtn" if prohibition else "displayQuickPushBtn"
    prefix, detail = detect_prefix_and_get(client, sid)
    enabled = _toolbar_value(detail, field)
    if enabled is None:
        records = query_approve_records(client, approve_id=approve_id)
        record = records[0] if records else {}
        enabled = _toolbar_value(record, field)
        if enabled is None:
            for key in CONTROL_KEYS:
                enabled = _toolbar_value(record.get(key), field)
                if enabled is not None:
                    break
    return prefix, bool(enabled)


def quick_push(client, prefix, sid, push_type, confirm=False):
    if prefix not in PUSH_PREFIXES:
        return {"status": 412, "message": "该策略模块不支持立即推全"}
    params = {"id": int(sid), "type": int(push_type)}
    if confirm:
        params["confirm"] = "true"
    resp = client.session.request("POST", f"{client.base}/{prefix}/quickPushAll", params=params, json={})
    return resp.json()


def push_with_gate(client, sid, approve_id, prohibition, steps):
    """普通 type=0 / 封禁期 type=2 推全；门闩关闭是跳过，不算失败。"""
    prefix, enabled = read_push_gate(client, sid, approve_id, prohibition=prohibition)
    gate_name = "displayProhibitionPeriodQuickPushBtn" if prohibition else "displayQuickPushBtn"
    if not enabled:
        time.sleep(0.4)
        prefix, enabled = read_push_gate(client, sid, approve_id, prohibition=prohibition)
    if not enabled:
        steps.append({"step": "quick_push_skipped", "gate": gate_name})
        return {"ok": True, "pushStatus": "skipped_gate_closed", "message": "推全按钮未开放，已跳过推全"}
    if prefix not in PUSH_PREFIXES:
        return {"ok": True, "pushStatus": "skipped_unsupported_prefix", "message": "该模块不支持立即推全，已跳过"}
    push_type = 2 if prohibition else 0
    data = quick_push(client, prefix, sid, push_type)
    msg = data.get("message") or "推全失败"
    steps.append({"step": "quick_push", "type": push_type, "accepted": data.get("status") == 200})
    if data.get("status") == 200:
        return {"ok": True, "pushStatus": "accepted", "message": "立即推全已受理"}
    if _needs_ban_confirm(msg):
        data = quick_push(client, prefix, sid, push_type, confirm=True)
        msg = data.get("message") or "推全确认失败"
        steps.append({"step": "quick_push_confirm", "type": push_type, "accepted": data.get("status") == 200})
        if data.get("status") == 200:
            return {"ok": True, "pushStatus": "accepted", "message": "确认后立即推全已受理"}
        if not prohibition and "提交审核" in msg:
            queued = quick_push(client, prefix, sid, 1, confirm=True)
            steps.append({"step": "quick_push_submit_ban_queue", "type": 1, "accepted": queued.get("status") == 200})
            if queued.get("status") == 200:
                return {"ok": True, "pushStatus": "ban_queue_submitted", "message": "已提交封禁期审核队列"}
            msg = queued.get("message") or msg
    return {"ok": False, "pushStatus": "failed", "message": msg}


def flow_one(client, sid, mode, auditor, reason=None):
    """唯一允许的审核状态机：1→6→2→推全；6 补发布推全；12→13→封禁推全。"""
    t0 = time.time()
    record = locate_approve_record(client, sid)
    if not record:
        return {"id": sid, "ok": True, "skipped": True,
                "skipReason": "NO_ALLOWED_AUDIT_RECORD",
                "message": "未找到 status=1/6/12 的审核单，按边界不执行任何操作"}
    approve_id = record.get("id")
    state = record.get("status")
    steps = [{"step": "locate_approve", "approveId": approve_id, "status": state}]
    base = {"id": sid, "approveId": approve_id, "approveStatus": state,
            "approveRuleName": record.get("ruleName"), "steps": steps}

    if state == 1:
        data, msg = change_status(client, approve_id, 6, reason)
        steps.append({"step": "check_pass", "accepted": data.get("status") == 200})
        if data.get("status") != 200:
            return dict(base, ok=False, auditOk=False, message=msg, stuckAt="check_pass",
                        elapsedSec=round(time.time() - t0, 1))
        audit_ok = True
    elif state == 6:
        audit_ok = True
    elif state == 12:
        data, msg = change_status(client, approve_id, 13, reason)
        steps.append({"step": "ban_pass", "accepted": data.get("status") == 200})
        if data.get("status") != 200:
            return dict(base, ok=False, auditOk=False, message=msg, stuckAt="ban_pass",
                        elapsedSec=round(time.time() - t0, 1))
        push = push_with_gate(client, sid, approve_id, prohibition=True, steps=steps)
        return dict(base, ok=push["ok"], auditOk=True, publishOk=None,
                    pushStatus=push["pushStatus"], message=push["message"],
                    elapsedSec=round(time.time() - t0, 1))
    else:
        return dict(base, ok=True, skipped=True, skipReason="UNSUPPORTED_AUDIT_STATUS",
                    message="审核单状态不在 1/6/12，按边界不执行任何操作")

    # A2：仅当前待发布/刚审核通过的单进行发布；失败不跨轮补推。
    data, msg = change_status(client, approve_id, 2, reason)
    steps.append({"step": "publish_pass", "accepted": data.get("status") == 200})
    publish_ok = data.get("status") == 200
    if not publish_ok and _needs_ban_confirm(msg):
        data, msg = change_status(client, approve_id, 2, reason, confirm=True)
        steps.append({"step": "publish_pass_confirm", "accepted": data.get("status") == 200})
        publish_ok = data.get("status") == 200
        if not publish_ok and _needs_ban_confirm(msg):
            return dict(base, ok=True, auditOk=audit_ok, publishOk=False,
                        pushStatus="not_attempted", softEnded=True,
                        message="审核已通过；发布需封禁期人工确认，已软结束", elapsedSec=round(time.time() - t0, 1))
    if not publish_ok and not _published_idempotent(msg):
        return dict(base, ok=False, auditOk=audit_ok, publishOk=False, message=msg,
                    stuckAt="publish_pass", elapsedSec=round(time.time() - t0, 1))

    push = push_with_gate(client, sid, approve_id, prohibition=False, steps=steps)
    return dict(base, ok=push["ok"], auditOk=audit_ok, publishOk=True,
                pushStatus=push["pushStatus"], message=push["message"],
                elapsedSec=round(time.time() - t0, 1))


def change_one(client, sid, status_key, auditor):
    return {"id": sid, "ok": False, "message": "边界限制：不支持独立状态流转；仅可执行定义的审核流程"}


def push_one(client, sid, auditor):
    return {"id": sid, "ok": False, "message": "边界限制：不支持独立推全；推全只在定义审核流程内执行"}


def stable_action_id(sid, action):
    """稳定 action_id：由规范化策略 ID 与动作派生，用于重放前回查。"""
    raw = f"union-strategy-audit:{int(sid)}:{action}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def write_trace(args, result):
    """写操作后追加溯源记录（append-only JSONL）。

    记录谁发起的（operator，即 --auditor）与策略 ID（strategyIds）为核心字段；
    落盘失败只写 stderr，不阻断主流程。默认写到 <cwd>/audit-trace/
    audit-YYYY-MM-DD.jsonl，可用环境变量 UNION_AUDIT_TRACE_DIR 改目录。
    """
    try:
        tz = timezone(timedelta(hours=8), name="Asia/Shanghai")
        now = datetime.now(tz)
        data = result.get("data") or {}
        items = data.get("items") or data.get("executed") or []
        slim = []
        for it in items:
            s = {
                "id": it.get("id"),
                "ok": it.get("ok"),
                "skipped": it.get("skipped"),
                "skipReason": it.get("skipReason"),
                "message": it.get("message"),
                "auditOk": it.get("auditOk"),
                "publishOk": it.get("publishOk"),
                "pushStatus": it.get("pushStatus"),
                "strategyTypeDesc": it.get("strategyTypeDesc"),
                "actionId": it.get("actionId"),
            }
            slim.append({k: v for k, v in s.items() if v is not None})
        ids = [it.get("id") for it in items if it.get("id") is not None]
        if not ids and args.ids:
            ids = [int(tok) for tok in args.ids.replace("，", ",").split(",") if tok.strip()]
        line = {
            "v": 1,
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "tsMs": int(now.timestamp() * 1000),
            "tz": "Asia/Shanghai",
            "operator": getattr(args, "auditor", None) or None,  # 指令发起人（业务口径）
            "systemUser": os.environ.get("USER") or os.environ.get("USERNAME") or None,  # Auth 执行者
            "action": getattr(args, "action", None),
            "strategyIds": ids or None,
            "result": {
                "okCount": data.get("okCount"),
                "failCount": data.get("failCount"),
                "skipCount": data.get("skipCount"),
                "items": slim,
            },
            "error": result.get("error"),
            "msg": result.get("msg"),
        }
        trace_dir = os.environ.get("UNION_AUDIT_TRACE_DIR") or os.path.join(os.getcwd(), "audit-trace")
        os.makedirs(trace_dir, exist_ok=True)
        with open(os.path.join(trace_dir, f"audit-{now.strftime('%Y-%m-%d')}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as e:
        sys.stderr.write("[union-strategy-audit] 溯源日志未落盘: %s\n" % e)


def check_action(client, sid):
    """以策略详情为权威回查源，不在本地猜测历史副作用。"""
    return query_one(client, sid)


def batch_ids(client, ids, fn):
    items, ok_n, fail_n, skip_n = [], 0, 0, 0
    for i, sid in enumerate(ids, 1):
        print(">> [%d/%d] 处理策略 #%s ..." % (i, len(ids), sid), flush=True)
        try:
            item = fn(sid)
        except Exception as e:  # 单项失败不阻断其余
            item = {"id": sid, "ok": False, "message": str(e)}
        if item.get("skipped"):
            skip_n += 1
        elif item.get("ok"):
            ok_n += 1
        else:
            fail_n += 1
        items.append(item)
    return items, ok_n, fail_n, skip_n


def list_users(client):
    data = unwrap(client._request("GET", "/common/listUser"))
    users = data if isinstance(data, list) else (data.get("records") or data.get("list") or [])
    return users


def match_user(client, keyword):
    users = list_users(client)
    kw = (keyword or "").strip().lower()
    exact = [u for u in users if str(u.get("name", "")).lower() == kw]
    prefix = [u for u in users if str(u.get("name", "")).lower().startswith(kw)]
    fuzzy = [u for u in users if kw and kw in str(u.get("name", "")).lower()]
    pool = (exact or prefix or fuzzy)
    # 去重保序
    seen, uniq = set(), []
    for u in pool:
        if u["id"] not in seen:
            seen.add(u["id"])
            uniq.append(u)
    return uniq[:10], len(users)



def query_by_creator(client, creator_id, status=1):
    """按创建人和审核单状态定位；不可漏传 creatorId 以免误审他人待审单。"""
    return query_approve_records(client, creator_id=creator_id, status=status)


def run_creator_action(client, records, creator_action, auditor):
    """对审核人名下待审单批量执行动作；打折策略在任何写动作前跳过。"""
    ids = [r.get("ruleId") or r.get("id") for r in records]
    ids = [int(i) for i in ids if i is not None]
    items, ok_n, fail_n, skip_n = [], 0, 0, 0
    for i, sid in enumerate(ids, 1):
        print(">> [%d/%d] 策略 #%s ..." % (i, len(ids), sid), flush=True)
        try:
            snapshot = query_one(client, sid)
            if not snapshot.get("ok"):
                item = snapshot
            elif is_discount_strategy(snapshot, client):
                item = skipped_discount_item(sid, snapshot, auditor)
            elif creator_action == "full_audit":
                item = flow_one(client, sid, "normal", auditor, reason="联盟审核自动流转")
                item["strategy"] = snapshot
                item["strategyTypeDesc"] = strategy_type_desc(snapshot, client)
            else:
                item = {"id": sid, "ok": False,
                        "message": "边界限制：按创建人批量仅支持 full_audit 状态机，不执行单步动作"}
                item["strategy"] = snapshot
                item["strategyTypeDesc"] = strategy_type_desc(snapshot, client)
        except Exception as e:
            item = {"id": sid, "ok": False, "message": str(e)}
        if item.get("skipped"):
            skip_n += 1
        elif item.get("ok"):
            ok_n += 1
        else:
            fail_n += 1
        items.append(item)
    return items, ok_n, fail_n, skip_n


def build_brief(items):
    """群聊快速回执专用的轻量结果；避免上层为展示而遍历 strategy.fields 大对象。"""
    result = []
    for item in items:
        strategy = item.get("strategy") or {}
        fields = strategy.get("fields") or {}
        result.append({
            "id": item.get("id"),
            "ok": bool(item.get("ok")),
            "skipped": bool(item.get("skipped")),
            "skipReason": item.get("skipReason"),
            "name": strategy.get("name") or item.get("name"),
            "status": strategy.get("status") if strategy else item.get("status"),
            "statusDesc": strategy.get("statusDesc") or item.get("statusDesc"),
            "auditOk": item.get("auditOk"),
            "publishOk": item.get("publishOk"),
            "pushStatus": item.get("pushStatus"),
            "strategyTypeDesc": item.get("strategyTypeDesc"),
            "deferredInvalidCandidate": item.get("deferredInvalidCandidate"),
            "actionId": item.get("actionId"),
            "approveId": item.get("approveId") or (strategy.get("_boostControlBlock") or {}).get("approveId"),
            "elapsedSec": item.get("elapsedSec"),
            "message": item.get("message"),
            "prefix": fields.get("_prefix"),
        })
    return result


def main():
    started_at = time.time()

    def emit(payload):
        """统一出口：补齐 Response Contract 的 duration_ms 后输出并退出。"""
        payload.setdefault("duration_ms", int((time.time() - started_at) * 1000))
        print(json.dumps(payload, ensure_ascii=False))
        sys.exit(0 if payload.get("code") == 0 else int(payload.get("code") or 1))

    parser = argparse.ArgumentParser(description="联盟审核 CLI")
    parser.add_argument("--action", required=True,
                        choices=["query", "audit", "force_audit", "change", "push", "users", "match_user", "by_creator"])
    parser.add_argument("--ids", help="策略 id，逗号/空格分隔")
    parser.add_argument("--auditor", help="操作者姓名（仅记录）")
    parser.add_argument("--reason", help="状态流转原因；未传时使用「联盟审核自动流转」")
    parser.add_argument("--status-key", choices=list(ACTION_CFG.keys()),
                        help="单步状态流转动作（change 必填）")
    parser.add_argument("--keyword", help="审核人关键词（match_user/by_creator 用）")
    parser.add_argument("--creator-id", help="审核人 id（by_creator 用，可省则按 keyword 匹配）")
    parser.add_argument("--creator-action",
                        choices=["check_pass", "publish_pass", "check_fail", "publish_fail", "full_audit"],
                        help="by_creator 时对其待审单批量执行的动作")
    parser.add_argument("--item", type=int, help="单项补跑入口：只处理这一个策略 id，等价 --ids <item>")
    parser.add_argument("--check-action", help="回查 action_id；格式为由本脚本生成的稳定 action_id")
    parser.add_argument("--concurrency", type=int, default=1, choices=[1], help="并发度；线上状态写入固定串行，仅允许 1")
    parser.add_argument("--batch-size", type=int, default=50, help="批次大小，范围 1~50")
    parser.add_argument("--max-runtime", type=int, default=900, help="最大运行时间（秒），默认 900")
    parser.add_argument("--authoritative-source", default="audit_get", choices=["audit_get"], help="权威回查数据源")
    args = parser.parse_args()

    # 单项补跑入口：--item 等价于 --ids <item>，用于批量中单项失败后的重试
    if args.item is not None:
        args.ids = str(args.item)

    try:
        if not 1 <= args.batch_size <= 50:
            raise ValueError("--batch-size 必须在 1~50")
        client = AuditClient()

        if args.check_action:
            if not args.ids:
                raise ValueError("--check-action 需要同时提供 --ids")
            sid = int(args.ids.replace("，", ",").split(",")[0].strip())
            expected = stable_action_id(sid, args.action)
            if args.check_action != expected:
                raise ValueError("action_id 与策略 ID 或动作不匹配")
            snapshot = check_action(client, sid)
            result = {"ok": True, "code": 0,
                      "data": {"actionId": expected, "authoritativeSource": args.authoritative_source,
                               "strategy": snapshot},
                      "next_action": {"type": "CONTINUE"}}

        elif args.action == "users":
            users = list_users(client)
            print(">> 审核人总数: %d，展示前 50 条" % len(users), flush=True)
            result = {"ok": True, "code": 0,
                      "data": {"total": len(users),
                               "users": [{"id": u.get("id"), "name": u.get("name")} for u in users[:50]]},
                      "next_action": {"type": "CONTINUE"}}

        elif args.action == "match_user":
            if not args.keyword:
                raise ValueError("match_user 需要 --keyword")
            matched, total = match_user(client, args.keyword)
            result = {"ok": True, "code": 0,
                      "data": {"totalUsers": total,
                               "matched": [{"id": u.get("id"), "name": u.get("name")} for u in matched]},
                      "next_action": {"type": "CONTINUE"}}

        elif args.action == "by_creator":
            if not args.keyword and not args.creator_id:
                raise ValueError("by_creator 需要 --keyword 或 --creator-id")
            if args.creator_id:
                cid = int(args.creator_id)
                cname = "id=%s" % cid
            else:
                matched, _ = match_user(client, args.keyword)
                if not matched:
                    raise ValueError("未找到匹配「%s」的审核人，请用更精确的关键词" % args.keyword)
                cid = matched[0]["id"]
                cname = matched[0]["name"]
            print(">> 查询审核人 %s 的待审策略（status=1）..." % cname, flush=True)
            records = query_by_creator(client, cid, 1)
            ids = [r.get("ruleId") or r.get("id") for r in records]
            items, ok_n, fail_n, skip_n = run_creator_action(client, records, args.creator_action, args.auditor) \
                if args.creator_action else ([], 0, 0, 0)
            result = {"ok": True, "code": 0,
                      "data": {"creatorName": cname, "creatorId": cid, "total": len(records),
                               "pendingIds": [int(i) for i in ids if i is not None],
                               "executed": items, "okCount": ok_n, "failCount": fail_n,
                               "skipCount": skip_n},
                      "next_action": {"type": "CONTINUE"}}
            if args.creator_action:
                write_trace(args, result)

        else:
            if not args.ids:
                raise ValueError("该操作需要 --ids")
            ids = []
            for tok in args.ids.replace("，", ",").split(","):
                tok = tok.strip()
                if tok:
                    ids.append(int(tok))
            if not ids:
                raise ValueError("--ids 中未解析出策略 id")

            if args.action == "query":
                if len(ids) > 50:
                    raise ValueError("单次查询最多 50 个 id")
                items = []
                for s in ids:
                    # 单条隔离：任何一个 id 查询异常（网络/接口波动）不得拖垮整批查询。
                    try:
                        snap = query_one(client, s)
                        if snap.get("ok"):
                            td = strategy_type_desc(snap, client)
                            snap["strategyTypeDesc"] = td
                            snap["discountExcluded"] = (td == "打折")
                            snap["discountUnknown"] = (td is None)
                        else:
                            # 查无此 ID：不再回查审核单类型，直接按失败项展示。
                            snap["strategyTypeDesc"] = None
                            snap["discountExcluded"] = False
                            snap["discountUnknown"] = True
                    except Exception as e:
                        snap = {"id": s, "ok": False, "message": "查询异常：%s" % e,
                                "strategyTypeDesc": None, "discountExcluded": False, "discountUnknown": True}
                    items.append(snap)
                result = {"ok": True, "code": 0, "data": {"items": items},
                          "next_action": {"type": "CONTINUE"}}
            elif args.action in ("audit", "force_audit"):
                if len(ids) > 50:
                    raise ValueError("单次审核最多 50 个 id")

                def audit_with_snapshot(sid):
                    snapshot = query_one(client, sid)
                    # 身份未确认（查无此 ID）时直接停止，不发任何写请求——
                    # 不能在不确定这是什么策略的情况下执行审核/发布/推全。
                    if not snapshot.get("ok"):
                        return snapshot
                    # 打折策略是业务硬排除项：boostControl.typeDesc=打折时不发任何写请求。
                    if is_discount_strategy(snapshot, client):
                        return skipped_discount_item(sid, snapshot, args.auditor)
                    # status=5 的延期提审只标记，最终能否通过仍以服务端状态校验为准。
                    item = flow_one(client, sid, "normal", args.auditor, reason=args.reason)
                    item["actionId"] = stable_action_id(sid, args.action)
                    item["strategy"] = snapshot
                    item["strategyTypeDesc"] = strategy_type_desc(snapshot, client)
                    item["deferredInvalidCandidate"] = is_deferred_invalid(snapshot)
                    return item

                items, ok_n, fail_n, skip_n = batch_ids(client, ids, audit_with_snapshot)
                result = {"ok": True, "code": 0,
                          "data": {"items": items, "brief": build_brief(items),
                                   "okCount": ok_n, "failCount": fail_n,
                                   "skipCount": skip_n, "force": True,
                                   "executionMode": "serial_orchestrated",
                                   "maxItems": 50},
                          "next_action": {"type": "CONTINUE"}}
                write_trace(args, result)
            elif args.action == "change":
                if not args.status_key:
                    raise ValueError("change 需要 --status-key")
                def change_with_snapshot(sid):
                    snapshot = query_one(client, sid)
                    if not snapshot.get("ok"):
                        return snapshot
                    if is_discount_strategy(snapshot, client):
                        return skipped_discount_item(sid, snapshot, args.auditor)
                    item = change_one(client, sid, args.status_key, args.auditor)
                    item["strategy"] = snapshot
                    item["strategyTypeDesc"] = strategy_type_desc(snapshot, client)
                    return item

                items, ok_n, fail_n, skip_n = batch_ids(client, ids, change_with_snapshot)
                result = {"ok": True, "code": 0,
                          "data": {"items": items, "okCount": ok_n, "failCount": fail_n,
                                   "skipCount": skip_n},
                          "next_action": {"type": "CONTINUE"}}
                write_trace(args, result)
            else:  # push
                def push_with_snapshot(sid):
                    snapshot = query_one(client, sid)
                    if not snapshot.get("ok"):
                        return snapshot
                    if is_discount_strategy(snapshot, client):
                        return skipped_discount_item(sid, snapshot, args.auditor)
                    item = push_one(client, sid, args.auditor)
                    item["strategy"] = snapshot
                    item["strategyTypeDesc"] = strategy_type_desc(snapshot, client)
                    return item

                items, ok_n, fail_n, skip_n = batch_ids(client, ids, push_with_snapshot)
                result = {"ok": True, "code": 0,
                          "data": {"items": items, "okCount": ok_n, "failCount": fail_n,
                                   "skipCount": skip_n},
                          "next_action": {"type": "CONTINUE"}}
                write_trace(args, result)

        emit(result)

    except KeyboardInterrupt:
        emit({"ok": False, "code": 130, "error": "SIGINT", "msg": "用户中断",
              "next_action": {"type": "STOP", "retryable": False}})
    except Exception as e:
        msg = str(e)
        err_code = "API_TIMEOUT" if "timed out" in msg.lower() or "timeout" in msg.lower() else "API_ERROR"
        retryable = err_code == "API_TIMEOUT"
        emit({"ok": False, "code": 1, "error": err_code, "msg": msg,
              "next_action": {"type": "RETRY_ONCE_THEN_STOP" if retryable else "STOP",
                              "retryable": retryable}})


if __name__ == "__main__":
    main()
