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
"""kconf_client.py — ConfigService 配置中心通用单 key 操作（读/写/历史/监控/灰度/回滚）。

stdout 最后一行输出标准 JSON: {"ok", "code", "data"|"error"/"msg", "next_action"}
退出码: 0=成功, 1=失败, 2=权限/询问, 130=SIGINT
"""
import argparse
import json
import sys
import time
from auth_helper.sso_login_client import SmartAuthSession, SSOAuthRequiredError

BASE = "https://config.example.corp"
START_MS = int(time.time() * 1000)


def emit(payload, code):
    payload.setdefault("duration_ms", int(time.time() * 1000) - START_MS)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


class ConfigClient:
    def __init__(self):
        self.session = SmartAuthSession()

    def _call(self, method, path, params=None, json_body=None):
        try:
            resp = self.session.request(method, f"{BASE}{path}", params=params, json=json_body, timeout=30)
        except SSOAuthRequiredError:
            emit({"ok": False, "code": 2, "error": "AUTH_EXPIRED", "msg": "Auth 登录态已失效，请重新登录内网后重试", "next_action": {"type": "ASK_USER"}}, 2)
        except Exception as exc:  # network / timeout
            msg = str(exc)
            if "timed out" in msg.lower() or "timeout" in msg.lower():
                emit({"ok": False, "code": 1, "error": "API_TIMEOUT", "msg": f"请求超时: {path}", "next_action": {"type": "RETRY_ONCE_THEN_STOP"}}, 1)
            emit({"ok": False, "code": 1, "error": "NETWORK_ERROR", "msg": msg, "next_action": {"type": "STOP"}}, 1)

        if resp.status_code >= 500:
            emit({"ok": False, "code": 1, "error": "API_5XX", "msg": f"ConfigService 服务端错误 {resp.status_code}: {resp.text[:500]}", "next_action": {"type": "STOP"}}, 1)
        if resp.status_code >= 400:
            emit({"ok": False, "code": 1, "error": "API_4XX", "msg": f"ConfigService 请求错误 {resp.status_code}: {resp.text[:500]}", "next_action": {"type": "STOP"}}, 1)

        body = resp.json()
        result = body.get("result")
        if result == 101:
            emit({"ok": False, "code": 2, "error": "AUTH_EXPIRED", "msg": "ConfigService 返回「需要登录」，请完成内网登录后重试", "next_action": {"type": "ASK_USER"}}, 2)
        if result == 104:
            emit({"ok": False, "code": 1, "error": "RATE_LIMITED", "msg": "ConfigService 请求过于频繁，已触发频控，请稍后重试", "next_action": {"type": "RETRY_ONCE_THEN_STOP", "retryable": True}}, 1)
        if result not in (1, None):
            emit({"ok": False, "code": 1, "error": "KCONF_RESULT_NOT_1", "msg": f"[{result}] {body.get('message', str(body))}", "next_action": {"type": "STOP"}}, 1)
        return body.get("data", body)

    # ---- 只读 ----
    def get(self, key):
        return self._call("GET", "/api/w/config/get", params={"key": key})

    def get_legacy(self, key):
        return self._call("GET", "/api/config/get", params={"key": key})

    # ---- 页面壳 ----
    def is_login(self):
        return self._call("GET", "/api/islogin")

    def favorite_list(self):
        return self._call("GET", "/api/favorite/list")

    def visited_list(self):
        return self._call("GET", "/api/visited/list")

    def last_modified(self):
        return self._call("GET", "/api/config/lastModified")

    def pending_count(self):
        return self._call("GET", "/api/myPeding/countAll")

    def alertbox_info(self):
        return self._call("GET", "/api/alertbox/info")

    def history_list(self, key, page=1, size=20):
        return self._call("GET", "/api/w/config/history/list", params={"key": key, "page": page, "size": size})

    def history_detail(self, key, version):
        return self._call("GET", "/api/config/history/gets", params={"key": key, "version": version})

    def history_version(self, key, change_id=None):
        if change_id is None:
            change_id = self.get(key).get("meta", {}).get("changeId")
        return self._call("GET", "/api/config/history/version", params={"key": key, "changeId": change_id})

    def history_change_env(self, key):
        return self._call("GET", "/api/w/config/history/changeEnv", params={"key": key})

    def snapshot_get(self, snapshot_id):
        return self._call("GET", "/api/snapshot/get", params={"id": snapshot_id})

    def monitor_ksn(self, key):
        return self._call("GET", "/api/w/config/monitor/ksn", params={"key": key, "ksnInput": ""})

    def monitor_machine(self, key, only_abnormal_events=False):
        return self._call("GET", "/api/w/config/monitor/machine",
                          params={"key": key, "onlyAbnormalEvents": only_abnormal_events})

    def schema_get(self, key, ksn=""):
        return self._call("GET", "/api/monster/schema/get", params={"key": key, "ksn": ksn})

    def maintenance(self, key):
        return self._call("GET", "/api/workflow/maintenanceV2", params={"key": key})

    def service_level_count(self, key):
        return self._call("GET", "/api/w/config/use/getUsingServiceLevelCount", params={"key": key})

    def stability_result(self, inspection_task_id, key, env="PROD"):
        return self._call("GET", "/api/stability/changeInspection/result",
                          params={"inspectionTaskId": inspection_task_id, "env": env, "key": key})

    def direct_change_check(self, key):
        return self._call("GET", "/api/w/workflow/directChangeCheck", params={"key": key})

    def get_reviewers(self, key):
        # 实测：接口参数名为 path，值为完整 key path
        return self._call("GET", "/api/w/workflow/getReviewers", params={"path": key})

    def precheck(self, key, stage, gray_strategy="add"):
        # 实测：该接口全部参数走 query；grayParams 是 JSON 数组字符串（不是对象）
        return self._call("POST", "/api/w/config/subconfig/precheck", params={
            "key": key, "ksn": "", "stage": stage, "checkType": "fullCheck",
            "customName": "", "grayParams": "[]", "grayStrategy": gray_strategy,
        })

    # ---- 写操作 ----
    def find_stage(self, get_data, stage):
        for sub in get_data.get("subConfigs", []):
            if sub.get("stage") == stage:
                return sub
        emit({"ok": False, "code": 1, "error": "API_4XX", "msg": f"未找到 stage={stage} 的配置环境", "next_action": {"type": "STOP"}}, 1)

    def update(self, key, stage, content, comment, emergency=False):
        get_data = self.get(key)
        sub = self.find_stage(get_data, stage)
        snapshot_id = sub["curConfig"]["snapshotId"]
        config_version = get_data["meta"]["version"]

        check = self.direct_change_check(key)
        self.precheck(key, stage)

        body = {
            "key": key, "content": content, "comment": comment, "stage": stage,
            "snapshotId": snapshot_id, "configVersion": config_version, "ksn": None,
            "grayParams": {"name": "", "hosts": [], "services": [], "kwsServices": [], "podNames": []},
            "customName": "", "emergency": "true" if emergency else "false",
            "grayTemplate": {}, "grayStrategy": "add",
        }
        if check.get("canDirectChange"):
            result = self._call("POST", "/api/w/config/subconfig/update", json_body=body)
        else:
            body["action"] = "update"
            body["assignees"] = ""
            body["assessmentId"] = None
            result = self._call("POST", "/api/w/review/submit", json_body=body)

        verify = self.get(key)
        verify_sub = self.find_stage(verify, stage)
        verified = verify_sub["curConfig"]["data"] == content or verify.get("meta", {}).get("version", 0) > config_version
        if not verified:
            emit({"ok": False, "code": 1, "error": "VERIFY_MISMATCH", "msg": "写操作已提交，但回查未确认目标 stage 已按预期生效（可能仍在审批/灰度中）", "data": {"submitted": result, "verify": verify_sub}, "next_action": {"type": "STOP"}}, 1)
        return {"submitted": result, "verified": True, "verify": verify_sub}

    def review_submit(self, key, stage, content, comment, assignees):
        get_data = self.get(key)
        sub = self.find_stage(get_data, stage)
        body = {
            "key": key, "content": content, "comment": comment, "stage": stage,
            "snapshotId": sub["curConfig"]["snapshotId"], "configVersion": get_data["meta"]["version"],
            "action": "update", "assignees": assignees, "ksn": None,
            "grayParams": {}, "customName": "", "grayTemplate": {}, "grayStrategy": "add", "assessmentId": None,
        }
        return self._call("POST", "/api/w/review/submit", json_body=body)

    def gray_next(self, key, stage):
        get_data = self.get(key)
        sub = self.find_stage(get_data, stage)
        result = self._call("POST", "/api/w/config/subconfig/gray/next", json_body={
            "key": key, "snapshotId": sub["curConfig"]["snapshotId"], "stage": stage, "ksn": None,
        })
        verify = self.get(key)
        return {"submitted": result, "verify": self.find_stage(verify, stage)}

    def gray_cancel(self, key, stage):
        get_data = self.get(key)
        sub = self.find_stage(get_data, stage)
        result = self._call("POST", "/api/w/config/subconfig/gray/cancel", json_body={
            "key": key, "snapshotId": sub["curConfig"]["snapshotId"], "stage": stage, "ksn": None,
        })
        verify = self.get(key)
        return {"submitted": result, "verify": self.find_stage(verify, stage)}

    def rollback(self, key, version):
        result = self._call("POST", "/api/config/rollback", json_body={"key": key, "version": version})
        verify = self.get(key)
        return {"submitted": result, "verify": verify}

    def meta_update(self, key, meta_body):
        return self._call("POST", "/api/w/config/update", json_body=meta_body)

    def get_manage_data(self, key):
        return self._call("GET", "/api/w/config/getManageData", params={"key": key})

    def tailnumber_update(self, key, stage, content, comment):
        get_data = self.get(key)
        sub = self.find_stage(get_data, stage)
        body = {
            "key": key, "content": content, "comment": comment, "stage": stage,
            "snapshotId": sub["curConfig"]["snapshotId"], "configVersion": get_data["meta"]["version"],
            "ksn": None, "grayParams": {}, "customName": "", "emergency": "false",
            "grayTemplate": {}, "grayStrategy": "add",
        }
        return self._call("POST", "/api/w/config/subconfig/tailnumber/update", json_body=body)

    def review_start_flow(self, key, stage, body=None):
        if body is None:
            sub = self.find_stage(self.get(key), stage)
            body = {"key": key, "stage": stage, "snapshotId": sub["curConfig"]["snapshotId"], "ksn": None}
        return self._call("POST", "/api/w/review/startFlow", json_body=body)

    def review_gray_next_submit(self, key, stage, comment="", assignees="", body=None):
        if body is None:
            sub = self.find_stage(self.get(key), stage)
            body = {"key": key, "stage": stage, "snapshotId": sub["curConfig"]["snapshotId"],
                    "ksn": None, "comment": comment, "assignees": assignees}
        return self._call("POST", "/api/w/review/gray/next/submit", json_body=body)

    def review_delete_submit(self, key, stage, comment="", assignees="", body=None):
        if body is None:
            sub = self.find_stage(self.get(key), stage)
            body = {"key": key, "stage": stage, "snapshotId": sub["curConfig"]["snapshotId"],
                    "ksn": None, "comment": comment, "assignees": assignees}
        return self._call("POST", "/api/w/review/delete/submit", json_body=body)

    def permission_apply(self, key, reason="", body=None):
        if body is None:
            body = {"key": key, "reason": reason}
        return self._call("POST", "/api/permission/apply", json_body=body)

    def favorite_add(self, key):
        return self._call("POST", "/api/favorite/add", json_body={"key": key})

    def favorite_remove(self, key):
        return self._call("POST", "/api/favorite/remove", json_body={"key": key})

    # ---- 找配置（目录 / 搜索，2026-10-28 页面实测） ----
    # 注意：/api/w/dir/list 与 /api/w/dir/get 实测 404，目录一律走旧前缀 /api/dir/*
    def dir_list_path(self, dir_name=None):
        return self._call("GET", "/api/dir/listPath", params={"dir": dir_name} if dir_name else None)

    def dir_list(self, dir_name=None, page=1, size=20):
        return self._call("GET", "/api/dir/list", params={"dir": dir_name or "", "page": page, "size": size})

    def dir_get(self, dir_name):
        return self._call("GET", "/api/dir/get", params={"dir": dir_name})

    def dir_search(self, path_fragment):
        return self._call("GET", "/api/dir/search", params={"path": path_fragment})

    def dir_search_first(self, path_fragment):
        return self._call("GET", "/api/dir/searchFirstDir", params={"path": path_fragment})

    def config_lite_list(self, dir_name, page=1, size=20):
        return self._call("GET", "/api/config/liteList", params={"dir": dir_name, "page": page, "size": size})

    def config_list(self, dir_name, page=1, size=20):
        return self._call("GET", "/api/config/list", params={"dir": dir_name, "page": page, "size": size})

    def config_list_keys(self, dir_name):
        return self._call("GET", "/api/config/listKeys", params={"dir": dir_name})

    def search_config(self, keyword, size=20, config_status=None):
        if config_status is None:
            config_status = ["normal"]
        return self._call("POST", "/api/w/search/config", json_body={
            "keyWord": keyword, "from": 0, "size": size, "configStatus": config_status})

    def search_config_legacy(self, keyword):
        return self._call("GET", "/api/search/config", params={"key": keyword})


def read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        emit({"ok": False, "code": 1, "error": "FILE_NOT_FOUND", "msg": f"文件不存在: {path}", "next_action": {"type": "ASK_USER"}}, 1)
    except PermissionError:
        emit({"ok": False, "code": 1, "error": "PERMISSION_DENIED", "msg": f"无权限读取: {path}", "next_action": {"type": "STOP"}}, 1)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("get"); p.add_argument("--key", required=True); p.add_argument("--stage", default=None)
    p = sub.add_parser("history-list"); p.add_argument("--key", required=True); p.add_argument("--page", type=int, default=1); p.add_argument("--size", type=int, default=20)
    p = sub.add_parser("history-detail"); p.add_argument("--key", required=True); p.add_argument("--version", required=True)
    p = sub.add_parser("monitor-ksn"); p.add_argument("--key", required=True)
    p = sub.add_parser("monitor-machine"); p.add_argument("--key", required=True)
    p = sub.add_parser("update"); p.add_argument("--key", required=True); p.add_argument("--stage", default="production"); p.add_argument("--content-file", required=True); p.add_argument("--comment", required=True); p.add_argument("--emergency", action="store_true")
    p = sub.add_parser("review-submit"); p.add_argument("--key", required=True); p.add_argument("--stage", default="production"); p.add_argument("--content-file", required=True); p.add_argument("--comment", required=True); p.add_argument("--assignees", required=True)
    p = sub.add_parser("gray-next"); p.add_argument("--key", required=True); p.add_argument("--stage", default="production")
    p = sub.add_parser("gray-cancel"); p.add_argument("--key", required=True); p.add_argument("--stage", default="production")
    p = sub.add_parser("rollback"); p.add_argument("--key", required=True); p.add_argument("--version", required=True)
    p = sub.add_parser("meta-update"); p.add_argument("--key", required=True); p.add_argument("--meta-file", required=True)

    # 页面壳
    for cmd in ("islogin", "favorite-list", "visited-list", "last-modified", "pending-count", "alertbox"):
        sub.add_parser(cmd)
    # 只读
    p = sub.add_parser("get-legacy"); p.add_argument("--key", required=True)
    p = sub.add_parser("direct-change-check"); p.add_argument("--key", required=True)
    p = sub.add_parser("precheck"); p.add_argument("--key", required=True); p.add_argument("--stage", default="production"); p.add_argument("--gray-strategy", default="add")
    p = sub.add_parser("schema-get"); p.add_argument("--key", required=True); p.add_argument("--ksn", default="")
    p = sub.add_parser("maintenance"); p.add_argument("--key", required=True)
    p = sub.add_parser("service-level-count"); p.add_argument("--key", required=True)
    p = sub.add_parser("stability-result"); p.add_argument("--inspection-task-id", required=True); p.add_argument("--key", required=True); p.add_argument("--env", default="PROD")
    # 历史
    p = sub.add_parser("history-version"); p.add_argument("--key", required=True); p.add_argument("--change-id", type=int, default=None)
    p = sub.add_parser("history-change-env"); p.add_argument("--key", required=True)
    p = sub.add_parser("snapshot-get"); p.add_argument("--id", required=True)
    # 写
    p = sub.add_parser("get-reviewers"); p.add_argument("--key", required=True)
    p = sub.add_parser("tailnumber-update"); p.add_argument("--key", required=True); p.add_argument("--stage", default="production"); p.add_argument("--content-file", required=True); p.add_argument("--comment", required=True)
    for cmd in ("review-start-flow", "review-gray-next-submit", "review-delete-submit"):
        p = sub.add_parser(cmd); p.add_argument("--key", required=True); p.add_argument("--stage", default="production")
        p.add_argument("--comment", default=""); p.add_argument("--assignees", default=""); p.add_argument("--body-file", default=None)
    # 管理
    p = sub.add_parser("get-manage-data"); p.add_argument("--key", required=True)
    p = sub.add_parser("permission-apply"); p.add_argument("--key", required=True); p.add_argument("--reason", default=""); p.add_argument("--body-file", default=None)
    p = sub.add_parser("favorite-add"); p.add_argument("--key", required=True)
    p = sub.add_parser("favorite-remove"); p.add_argument("--key", required=True)
    # 找配置（目录 / 搜索）
    p = sub.add_parser("dir-path"); p.add_argument("--dir", default=None)
    p = sub.add_parser("dir-list"); p.add_argument("--dir", default=None); p.add_argument("--page", type=int, default=1); p.add_argument("--size", type=int, default=20)
    p = sub.add_parser("dir-get"); p.add_argument("--dir", required=True)
    p = sub.add_parser("dir-search"); p.add_argument("--path", required=True)
    p = sub.add_parser("dir-search-first"); p.add_argument("--path", required=True)
    p = sub.add_parser("config-lite-list"); p.add_argument("--dir", required=True); p.add_argument("--page", type=int, default=1); p.add_argument("--size", type=int, default=20)
    p = sub.add_parser("config-list"); p.add_argument("--dir", required=True); p.add_argument("--page", type=int, default=1); p.add_argument("--size", type=int, default=20)
    p = sub.add_parser("config-list-keys"); p.add_argument("--dir", required=True)
    p = sub.add_parser("search-config"); p.add_argument("--keyword", required=True); p.add_argument("--size", type=int, default=20); p.add_argument("--config-status", default=None)
    p = sub.add_parser("search-config-legacy"); p.add_argument("--keyword", required=True)

    args = parser.parse_args()
    client = ConfigClient()

    if args.cmd == "get":
        data = client.get(args.key)
        if args.stage:
            data = client.find_stage(data, args.stage)
        emit({"ok": True, "code": 0, "data": data, "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "history-list":
        emit({"ok": True, "code": 0, "data": client.history_list(args.key, args.page, args.size), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "history-detail":
        emit({"ok": True, "code": 0, "data": client.history_detail(args.key, args.version), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "monitor-ksn":
        emit({"ok": True, "code": 0, "data": client.monitor_ksn(args.key), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "monitor-machine":
        emit({"ok": True, "code": 0, "data": client.monitor_machine(args.key), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "update":
        content = read_file(args.content_file)
        emit({"ok": True, "code": 0, "data": client.update(args.key, args.stage, content, args.comment, args.emergency), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "review-submit":
        content = read_file(args.content_file)
        emit({"ok": True, "code": 0, "data": client.review_submit(args.key, args.stage, content, args.comment, args.assignees), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "gray-next":
        emit({"ok": True, "code": 0, "data": client.gray_next(args.key, args.stage), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "gray-cancel":
        emit({"ok": True, "code": 0, "data": client.gray_cancel(args.key, args.stage), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "rollback":
        emit({"ok": True, "code": 0, "data": client.rollback(args.key, args.version), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "meta-update":
        meta_body = json.loads(read_file(args.meta_file))
        emit({"ok": True, "code": 0, "data": client.meta_update(args.key, meta_body), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "islogin":
        emit({"ok": True, "code": 0, "data": client.is_login(), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "favorite-list":
        emit({"ok": True, "code": 0, "data": client.favorite_list(), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "visited-list":
        emit({"ok": True, "code": 0, "data": client.visited_list(), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "last-modified":
        emit({"ok": True, "code": 0, "data": client.last_modified(), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "pending-count":
        emit({"ok": True, "code": 0, "data": client.pending_count(), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "alertbox":
        emit({"ok": True, "code": 0, "data": client.alertbox_info(), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "get-legacy":
        emit({"ok": True, "code": 0, "data": client.get_legacy(args.key), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "direct-change-check":
        emit({"ok": True, "code": 0, "data": client.direct_change_check(args.key), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "precheck":
        emit({"ok": True, "code": 0, "data": client.precheck(args.key, args.stage, args.gray_strategy), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "schema-get":
        emit({"ok": True, "code": 0, "data": client.schema_get(args.key, args.ksn), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "maintenance":
        emit({"ok": True, "code": 0, "data": client.maintenance(args.key), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "service-level-count":
        emit({"ok": True, "code": 0, "data": client.service_level_count(args.key), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "stability-result":
        emit({"ok": True, "code": 0, "data": client.stability_result(args.inspection_task_id, args.key, args.env), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "history-version":
        emit({"ok": True, "code": 0, "data": client.history_version(args.key, args.change_id), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "history-change-env":
        emit({"ok": True, "code": 0, "data": client.history_change_env(args.key), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "snapshot-get":
        emit({"ok": True, "code": 0, "data": client.snapshot_get(args.id), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "get-reviewers":
        emit({"ok": True, "code": 0, "data": client.get_reviewers(args.key), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "tailnumber-update":
        content = read_file(args.content_file)
        emit({"ok": True, "code": 0, "data": client.tailnumber_update(args.key, args.stage, content, args.comment), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd in ("review-start-flow", "review-gray-next-submit", "review-delete-submit"):
        body = json.loads(read_file(args.body_file)) if args.body_file else None
        if args.cmd == "review-start-flow":
            data = client.review_start_flow(args.key, args.stage, body)
        elif args.cmd == "review-gray-next-submit":
            data = client.review_gray_next_submit(args.key, args.stage, args.comment, args.assignees, body)
        else:
            data = client.review_delete_submit(args.key, args.stage, args.comment, args.assignees, body)
        emit({"ok": True, "code": 0, "data": data, "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "get-manage-data":
        emit({"ok": True, "code": 0, "data": client.get_manage_data(args.key), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "permission-apply":
        body = json.loads(read_file(args.body_file)) if args.body_file else None
        emit({"ok": True, "code": 0, "data": client.permission_apply(args.key, args.reason, body), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "favorite-add":
        emit({"ok": True, "code": 0, "data": client.favorite_add(args.key), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "favorite-remove":
        emit({"ok": True, "code": 0, "data": client.favorite_remove(args.key), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "dir-path":
        emit({"ok": True, "code": 0, "data": client.dir_list_path(args.dir), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "dir-list":
        emit({"ok": True, "code": 0, "data": client.dir_list(args.dir, args.page, args.size), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "dir-get":
        emit({"ok": True, "code": 0, "data": client.dir_get(args.dir), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "dir-search":
        emit({"ok": True, "code": 0, "data": client.dir_search(args.path), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "dir-search-first":
        emit({"ok": True, "code": 0, "data": client.dir_search_first(args.path), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "config-lite-list":
        emit({"ok": True, "code": 0, "data": client.config_lite_list(args.dir, args.page, args.size), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "config-list":
        emit({"ok": True, "code": 0, "data": client.config_list(args.dir, args.page, args.size), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "config-list-keys":
        emit({"ok": True, "code": 0, "data": client.config_list_keys(args.dir), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "search-config":
        status = json.loads(args.config_status) if args.config_status else None
        emit({"ok": True, "code": 0, "data": client.search_config(args.keyword, args.size, status), "next_action": {"type": "CONTINUE"}}, 0)
    elif args.cmd == "search-config-legacy":
        emit({"ok": True, "code": 0, "data": client.search_config_legacy(args.keyword), "next_action": {"type": "CONTINUE"}}, 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        emit({"ok": False, "code": 130, "error": "SIGINT", "msg": "用户中断", "next_action": {"type": "STOP"}}, 130)
    except Exception as exc:
        emit({"ok": False, "code": 1, "error": "NETWORK_ERROR", "msg": str(exc), "next_action": {"type": "STOP"}}, 1)
