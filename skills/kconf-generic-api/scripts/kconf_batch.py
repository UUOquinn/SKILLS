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
"""kconf_batch.py — 对多个 KConf key 批量执行同类操作（编排模式）。

契约（固化，不可运行时更改）：
- 并发度=2
- 单批最大 50 个 key
- 整批最长 10 分钟，超时直接失败，不做检查点续跑
- 敏感操作（update/review-submit/gray-next/gray-cancel/rollback）派生稳定 action_id，
  写前 --check-action 回查避免重复提交，写后回查确认生效
- 只读操作（get/history-list/history-detail/monitor-ksn/monitor-machine）无需 action_id 保护
"""
import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ks_aimate.sso_login_client import SmartSSOSession, SSOAuthRequiredError

BASE = "https://kconf.example.corp"
CONCURRENCY = 2
MAX_ITEMS = 50
TIMEOUT_SECONDS = 600
START_MS = int(time.time() * 1000)
SENSITIVE_ACTIONS = {"update", "review-submit", "gray-next", "gray-cancel", "rollback"}
READONLY_ACTIONS = {"get", "history-list", "history-detail", "monitor-ksn", "monitor-machine"}


def emit(payload, code):
    payload.setdefault("duration_ms", int(time.time() * 1000) - START_MS)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


def read_lines_or_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except FileNotFoundError:
        emit({"ok": False, "code": 1, "error": "FILE_NOT_FOUND", "msg": f"文件不存在: {path}", "next_action": {"type": "ASK_USER"}}, 1)
    if raw.startswith("["):
        return json.loads(raw)
    return [line.strip() for line in raw.splitlines() if line.strip()]


def read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        emit({"ok": False, "code": 1, "error": "FILE_NOT_FOUND", "msg": f"文件不存在: {path}", "next_action": {"type": "ASK_USER"}}, 1)


def make_action_id(key, action, params):
    payload = json.dumps({"key": key, "action": action, "params": params}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class KConfBatchClient:
    """轻量内联版 KConfClient（避免跨脚本 import 路径问题）。"""

    def __init__(self):
        self.session = SmartSSOSession()

    def _call(self, method, path, params=None, json_body=None):
        resp = self.session.request(method, f"{BASE}{path}", params=params, json=json_body, timeout=30)
        if resp.status_code >= 500:
            raise RuntimeError(f"API_5XX:{resp.status_code}:{resp.text[:300]}")
        if resp.status_code >= 400:
            raise RuntimeError(f"API_4XX:{resp.status_code}:{resp.text[:300]}")
        body = resp.json()
        if body.get("result") not in (1, None):
            raise RuntimeError(f"KCONF_RESULT_NOT_1:{body.get('message', body)}")
        return body.get("data", body)

    def get(self, key):
        return self._call("GET", "/api/w/config/get", params={"key": key})

    def find_stage(self, data, stage):
        for sub in data.get("subConfigs", []):
            if sub.get("stage") == stage:
                return sub
        raise RuntimeError(f"API_4XX:未找到 stage={stage}")

    def history_list(self, key, page=1, size=20):
        return self._call("GET", "/api/w/config/history/list", params={"key": key, "page": page, "size": size})

    def history_detail(self, key, version):
        return self._call("GET", "/api/config/history/gets", params={"key": key, "version": version})

    def monitor_ksn(self, key):
        return self._call("GET", "/api/w/config/monitor/ksn", params={"key": key, "ksnInput": ""})

    def monitor_machine(self, key):
        return self._call("GET", "/api/w/config/monitor/machine", params={"key": key})

    def direct_change_check(self, key):
        return self._call("GET", "/api/w/workflow/directChangeCheck", params={"key": key})

    def update(self, key, stage, content, comment):
        data = self.get(key)
        sub = self.find_stage(data, stage)
        body = {
            "key": key, "content": content, "comment": comment, "stage": stage,
            "snapshotId": sub["curConfig"]["snapshotId"], "configVersion": data["meta"]["version"], "ksn": None,
            "grayParams": {"name": "", "hosts": [], "services": [], "kwsServices": [], "podNames": []},
            "customName": "", "emergency": "false", "grayTemplate": {}, "grayStrategy": "add",
        }
        check = self.direct_change_check(key)
        if check.get("canDirectChange"):
            result = self._call("POST", "/api/w/config/subconfig/update", json_body=body)
        else:
            body["action"] = "update"; body["assignees"] = ""; body["assessmentId"] = None
            result = self._call("POST", "/api/w/review/submit", json_body=body)
        verify = self.find_stage(self.get(key), stage)
        return {"submitted": result, "verify": verify}

    def review_submit(self, key, stage, content, comment, assignees):
        data = self.get(key)
        sub = self.find_stage(data, stage)
        body = {
            "key": key, "content": content, "comment": comment, "stage": stage,
            "snapshotId": sub["curConfig"]["snapshotId"], "configVersion": data["meta"]["version"],
            "action": "update", "assignees": assignees, "ksn": None,
            "grayParams": {}, "customName": "", "grayTemplate": {}, "grayStrategy": "add", "assessmentId": None,
        }
        result = self._call("POST", "/api/w/review/submit", json_body=body)
        return {"submitted": result}

    def gray_next(self, key, stage):
        sub = self.find_stage(self.get(key), stage)
        result = self._call("POST", "/api/w/config/subconfig/gray/next", json_body={"key": key, "snapshotId": sub["curConfig"]["snapshotId"], "stage": stage, "ksn": None})
        verify = self.find_stage(self.get(key), stage)
        return {"submitted": result, "verify": verify}

    def gray_cancel(self, key, stage):
        sub = self.find_stage(self.get(key), stage)
        result = self._call("POST", "/api/w/config/subconfig/gray/cancel", json_body={"key": key, "snapshotId": sub["curConfig"]["snapshotId"], "stage": stage, "ksn": None})
        verify = self.find_stage(self.get(key), stage)
        return {"submitted": result, "verify": verify}

    def rollback(self, key, version):
        result = self._call("POST", "/api/config/rollback", json_body={"key": key, "version": version})
        verify = self.get(key)
        return {"submitted": result, "verify": verify}


def process_one(client, key, action, params, check_only_action_id=None):
    """单个 key 的单次操作；返回结果字典，不抛出未捕获异常。"""
    action_id = None
    try:
        if action in SENSITIVE_ACTIONS:
            action_id = make_action_id(key, action, params)
            if check_only_action_id and action_id == check_only_action_id:
                return {"key": key, "status": "success", "action_id": action_id, "note": "action_id matched --check-action query"}

        if action == "get":
            stage = params.get("stage")
            data = client.get(key)
            if stage:
                data = client.find_stage(data, stage)
            result = data
        elif action == "history-list":
            result = client.history_list(key, params.get("page", 1), params.get("size", 20))
        elif action == "history-detail":
            result = client.history_detail(key, params["version"])
        elif action == "monitor-ksn":
            result = client.monitor_ksn(key)
        elif action == "monitor-machine":
            result = client.monitor_machine(key)
        elif action == "update":
            result = client.update(key, params.get("stage", "production"), params["content"], params.get("comment", "batch update"))
        elif action == "review-submit":
            result = client.review_submit(key, params.get("stage", "production"), params["content"], params.get("comment", "batch review"), params.get("assignees", ""))
        elif action == "gray-next":
            result = client.gray_next(key, params.get("stage", "production"))
        elif action == "gray-cancel":
            result = client.gray_cancel(key, params.get("stage", "production"))
        elif action == "rollback":
            result = client.rollback(key, params["version"])
        else:
            return {"key": key, "status": "failed", "error": "API_4XX", "msg": f"未知 action: {action}"}

        return {"key": key, "status": "success", "action_id": action_id, "result": result}
    except Exception as exc:
        msg = str(exc)
        err = "API_5XX" if "API_5XX" in msg else ("API_4XX" if "API_4XX" in msg or "KCONF_RESULT_NOT_1" in msg else "NETWORK_ERROR")
        return {"key": key, "status": "failed", "action_id": action_id, "error": err, "msg": msg}


def main():
    if not os.environ.get("MYFLICKER_REMOTE_AGENT_METHOD"):
        emit({
            "ok": False, "code": 2, "error": "REMOTE_AGENT_REQUIRED",
            "msg": "该脚本是长耗时批量任务，请使用 call_remote_agent 执行",
            "next_action": {"type": "RETRY_WITH_CALL_REMOTE_AGENT", "retryable": True, "max_retries": 1},
        }, 2)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run")
    run_p.add_argument("--action", required=True, choices=sorted(SENSITIVE_ACTIONS | READONLY_ACTIONS))
    run_p.add_argument("--keys-file", required=True)
    run_p.add_argument("--params-file", default=None, help="JSON: 单个通用参数对象，或 {key: params} 映射；不传则用空参数")
    run_p.add_argument("--concurrency", type=int, default=CONCURRENCY)
    run_p.add_argument("--batch-size", type=int, default=MAX_ITEMS, help="单批最大 key 数上限")
    run_p.add_argument("--max-runtime", type=int, default=TIMEOUT_SECONDS, help="整批最长运行时间（秒），超时直接失败")
    run_p.add_argument("--authoritative-source", default="kconf-get", help="写后回查的权威数据源，默认重新 GET /api/w/config/get 回查")
    run_p.add_argument("--check-action", default=None, help="跳过已执行过的该 action_id（幂等去重）")
    run_p.add_argument("--item", default=None, help="process_one 单项入口：只对这个 key 补跑，不进入批量并发")

    check_p = sub.add_parser("check-action")
    check_p.add_argument("--key", required=True)
    check_p.add_argument("--action", required=True)
    check_p.add_argument("--params-file", required=True)

    args = parser.parse_args()

    if args.cmd == "check-action":
        params = read_json_file(args.params_file)
        action_id = make_action_id(args.key, args.action, params)
        emit({"ok": True, "code": 0, "data": {"action_id": action_id}, "next_action": {"type": "CONTINUE"}}, 0)
        return

    keys = read_lines_or_json(args.keys_file)
    if args.item:
        keys = [args.item]
    if len(keys) > args.batch_size:
        emit({"ok": False, "code": 1, "error": "BATCH_SIZE_EXCEEDED", "msg": f"单批 key 数 {len(keys)} 超过上限 {args.batch_size}，请拆批", "next_action": {"type": "STOP"}}, 1)

    concurrency = min(args.concurrency, CONCURRENCY)  # 契约固化，不允许运行时调大

    params_map = {}
    if args.params_file:
        raw_params = read_json_file(args.params_file)
        if all(k in keys for k in raw_params.keys()) and isinstance(next(iter(raw_params.values()), None), dict):
            params_map = raw_params
        else:
            params_map = {k: raw_params for k in keys}
    else:
        params_map = {k: {} for k in keys}

    client = KConfBatchClient()
    results = []
    start = time.time()
    deadline = start + args.max_runtime

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(process_one, client, k, args.action, params_map.get(k, {}), args.check_action): k for k in keys}
            for fut in as_completed(futures, timeout=args.max_runtime):
                results.append(fut.result())
                if time.time() > deadline:
                    raise TimeoutError()
    except SSOAuthRequiredError:
        emit({"ok": False, "code": 2, "error": "SSO_EXPIRED", "msg": "SSO 登录态已失效，请重新登录内网后重试", "next_action": {"type": "ASK_USER"}}, 2)
    except TimeoutError:
        done_keys = {r["key"] for r in results}
        pending = [k for k in keys if k not in done_keys]
        emit({
            "ok": False, "code": 1, "error": "BATCH_TIMEOUT",
            "msg": f"整批运行超过 {args.max_runtime} 秒，直接判定失败，不做续跑",
            "data": {"completed": results, "pending_keys": pending, "authoritative_source": args.authoritative_source},
            "next_action": {"type": "STOP"},
        }, 1)

    order_index = {k: i for i, k in enumerate(keys)}
    results.sort(key=lambda r: order_index.get(r["key"], 0))
    failed = [r for r in results if r["status"] == "failed"]
    emit({
        "ok": len(failed) == 0, "code": 0 if len(failed) == 0 else 1,
        "data": {"total": len(keys), "success": len(results) - len(failed), "failed": len(failed), "results": results},
        "next_action": {"type": "CONTINUE" if len(failed) == 0 else "STOP"},
    }, 0 if len(failed) == 0 else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        emit({"ok": False, "code": 130, "error": "SIGINT", "msg": "用户中断", "next_action": {"type": "STOP"}}, 130)
    except Exception as exc:
        emit({"ok": False, "code": 1, "error": "NETWORK_ERROR", "msg": str(exc), "next_action": {"type": "STOP"}}, 1)
