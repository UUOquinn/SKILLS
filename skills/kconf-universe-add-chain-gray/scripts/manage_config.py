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
"""KConf universeAddChainGrayConfig 管理命令。"""
import argparse
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Tuple

import requests
from ks_aimate.sso_login_client import SmartSSOSession, SSOAuthRequiredError, get_username

BASE_URL = "https://kconf.example.corp"
KEY = "ad.adtarget3.universeAddChainGrayConfig"
MAX_RULES = 50


class SkillError(Exception):
    def __init__(self, error: str, msg: str, code: int = 1, next_type: str = "STOP"):
        super().__init__(msg)
        self.error, self.msg, self.code, self.next_type = error, msg, code, next_type


def emit(payload: Dict[str, Any], code: int) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    sys.exit(code)


def fail(error: str, msg: str, code: int = 1, next_type: str = "STOP") -> None:
    emit({"ok": False, "code": code, "error": error, "msg": msg,
          "next_action": {"type": next_type, "retryable": error == "API_TIMEOUT"}}, code)


def _classify_response_error(response: requests.Response) -> None:
    """对非 2xx 响应按状态码分类抛出 SkillError；2xx 无副作用。"""
    if response.status_code in (401, 403):
        raise SkillError("AUTH_FAILED", "当前账号无 KConf 权限或登录已失效", 2, "ASK_USER")
    if response.status_code >= 500:
        raise SkillError("API_5XX", f"KConf 服务异常（HTTP {response.status_code}）")


def _is_login_required_payload(payload: Any) -> bool:
    """KConf 对未建立域登录态的请求返回 HTTP 200 + result=101「需要登录」，
    与 SmartSSOSession 抛出的 SSOAuthRequiredError 同属登录态问题，需专门识别。"""
    return isinstance(payload, dict) and payload.get("result") == 101


class KConfClient:
    def __init__(self) -> None:
        self.session = SmartSSOSession()

    def request(self, method: str, path: str, *, params=None, body=None) -> Dict[str, Any]:
        try:
            response = self.session.request(method, f"{BASE_URL}{path}", params=params, json=body, timeout=30)
        except SSOAuthRequiredError as exc:
            raise SkillError("SSO_EXPIRED", "KConf SSO 登录态已失效或等待扫码确认，请完成内网登录后重试", 2, "ASK_USER") from exc
        except requests.Timeout as exc:
            raise SkillError("API_TIMEOUT", "KConf 接口请求超时", 1, "RETRY_ONCE_THEN_STOP") from exc
        except requests.RequestException as exc:
            raise SkillError("NETWORK_ERROR", f"无法连接 KConf：{exc}") from exc
        _classify_response_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SkillError("INVALID_RESPONSE", f"KConf 返回非 JSON 内容（HTTP {response.status_code}）") from exc
        if _is_login_required_payload(payload):
            raise SkillError("SSO_EXPIRED", "KConf 返回「需要登录」，该域 SSO 登录态未建立，请完成内网登录后重试", 2, "ASK_USER")
        if payload.get("result") != 1:
            raise SkillError("API_4XX", str(payload.get("message", "KConf 返回失败")))
        return payload

    def get_config(self) -> Dict[str, Any]:
        return self.request("GET", "/api/w/config/get", params={"key": KEY})

    def direct_check(self) -> Dict[str, Any]:
        return self.request("GET", "/api/w/workflow/directChangeCheck", params={"key": KEY})


def target_subconfig(config: Dict[str, Any], stage: str) -> Dict[str, Any]:
    for item in config.get("data", {}).get("subConfigs", []):
        if item.get("stage") == stage:
            return item
    raise SkillError("VALIDATION_ERROR", f"未找到环境：{stage}")


def load_rules(path: str) -> List[Dict[str, Any]]:
    try:
        rules = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SkillError("FILE_NOT_FOUND", f"规则文件不存在：{path}")
    except json.JSONDecodeError as exc:
        raise SkillError("VALIDATION_ERROR", f"规则文件不是有效 JSON：{exc}")
    if not isinstance(rules, list) or not rules or len(rules) > MAX_RULES:
        raise SkillError("VALIDATION_ERROR", f"规则必须是 1 到 {MAX_RULES} 条的数组")
    allowed = {"account_white_list", "product_name_white_list", "second_industry_id_white_list"}
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("ab_key"), str):
            raise SkillError("VALIDATION_ERROR", "每条规则必须包含字符串 ab_key")
        if not any(field in rule for field in allowed):
            raise SkillError("VALIDATION_ERROR", f"规则 {rule['ab_key']} 未提供白名单字段")
        for field in allowed & set(rule):
            if not isinstance(rule[field], list):
                raise SkillError("VALIDATION_ERROR", f"{rule['ab_key']}.{field} 必须是数组")
    return rules


def action_id(stage: str, rules: List[Dict[str, Any]]) -> str:
    normalized = json.dumps({"key": KEY, "stage": stage, "rules": rules}, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"))
    return hashlib.sha256(normalized.encode()).hexdigest()[:24]


def apply_rules(content: Dict[str, Any], rules: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    updated = deepcopy(content)
    configs = updated.get("configs")
    if not isinstance(configs, list):
        raise SkillError("VALIDATION_ERROR", "线上配置缺少 configs 数组")
    by_key = {item.get("ab_key"): item for item in configs}
    changes = []
    for rule in rules:
        current = by_key.get(rule["ab_key"])
        if current is None:
            changes.append({"ab_key": rule["ab_key"], "status": "not_found", "added": {}})
            continue
        added = {}
        for field in ("account_white_list", "product_name_white_list", "second_industry_id_white_list"):
            if field not in rule:
                continue
            existing = current.setdefault(field, [])
            values = [value for value in rule[field] if value not in existing]
            existing.extend(values)
            added[field] = values
        changes.append({"ab_key": rule["ab_key"], "status": "matched", "added": added})
    return updated, changes


def snapshot(config: Dict[str, Any], stage: str) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    sub = target_subconfig(config, stage)
    raw = sub.get("curConfig", {}).get("data", "")
    try:
        content = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SkillError("VALIDATION_ERROR", f"线上配置 JSON 无法解析：{exc}")
    meta = config.get("data", {}).get("meta", {})
    return sub, content, meta


def render_query(config: Dict[str, Any], stage: str) -> Dict[str, Any]:
    sub, content, meta = snapshot(config, stage)
    return {"key": KEY, "stage": stage, "version": meta.get("version"),
            "snapshot_id": sub.get("curConfig", {}).get("snapshotId"), "step_cur": sub.get("stepCur"),
            "step_total": sub.get("stepTotal"), "configs": content.get("configs", [])}


def preview(client: KConfClient, stage: str, rules_path: str) -> Dict[str, Any]:
    config = client.get_config()
    sub, content, meta = snapshot(config, stage)
    rules = load_rules(rules_path)
    _, changes = apply_rules(content, rules)
    direct = client.direct_check().get("data", {})
    return {"key": KEY, "stage": stage, "version": meta.get("version"),
            "snapshot_id": sub.get("curConfig", {}).get("snapshotId"), "can_direct_change": direct.get("canDirectChange"),
            "changes": changes, "action_id": action_id(stage, rules), "requested_by": get_username()}


def precheck(client: KConfClient, stage: str) -> None:
    client.request("POST", "/api/w/config/subconfig/precheck", body={
        "key": KEY, "ksn": None, "stage": stage, "checkType": "fullCheck", "customName": "",
        "grayParams": {}, "grayStrategy": "add"})


def update_payload(config: Dict[str, Any], stage: str, rules: List[Dict[str, Any]], comment: str) -> Dict[str, Any]:
    sub, content, meta = snapshot(config, stage)
    updated, _ = apply_rules(content, rules)
    return {"key": KEY, "content": json.dumps(updated, ensure_ascii=False, separators=(",", ":")),
            "comment": comment, "stage": stage, "snapshotId": sub.get("curConfig", {}).get("snapshotId"),
            "configVersion": meta.get("version"), "ksn": None,
            "grayParams": {"name": "", "hosts": [], "services": [], "kwsServices": [], "podNames": []},
            "customName": "", "emergency": "false", "grayTemplate": {}, "grayStrategy": "add"}


def verify_rules(client: KConfClient, stage: str, rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    config = client.get_config()
    _, content, meta = snapshot(config, stage)
    current = {item.get("ab_key"): item for item in content.get("configs", [])}
    checked = []
    complete = True
    for rule in rules:
        found = current.get(rule["ab_key"], {})
        fields = {}
        for field in ("account_white_list", "product_name_white_list", "second_industry_id_white_list"):
            if field in rule:
                values = found.get(field, [])
                present = all(value in values for value in rule[field])
                fields[field] = present
                complete = complete and present
        checked.append({"ab_key": rule["ab_key"], "fields": fields})
    return {"applied": complete, "version": meta.get("version"), "checks": checked}


def require_confirmation(args: argparse.Namespace) -> None:
    if not args.confirm:
        raise SkillError("CONFIRMATION_REQUIRED", "请先执行 preview 并让用户明确确认后，再携带 --confirm 执行", 2, "ASK_USER")


def run_apply(client: KConfClient, args: argparse.Namespace) -> Dict[str, Any]:
    require_confirmation(args)
    rules = load_rules(args.rules_file)
    expected = action_id(args.stage, rules)
    if args.action_id != expected:
        raise SkillError("ACTION_MISMATCH", "action_id 与当前规则或环境不一致，请重新生成预览", 2, "ASK_USER")
    already = verify_rules(client, args.stage, rules)
    if already["applied"]:
        raise SkillError("ACTION_ALREADY_APPLIED", "权威配置已包含全部目标白名单，未重复提交")
    config = client.get_config()
    direct = client.direct_check().get("data", {})
    precheck(client, args.stage)
    body = update_payload(config, args.stage, rules, args.comment)
    if direct.get("canDirectChange"):
        result = client.request("POST", "/api/w/config/subconfig/update", body=body)
        mode = "direct_update"
    else:
        if not args.assignees:
            raise SkillError("CONFIRMATION_REQUIRED", "当前变更需要 Review，请提供审批人 assignees", 2, "ASK_USER")
        body.update({"action": "update", "assignees": args.assignees, "assessmentId": None})
        result = client.request("POST", "/api/w/review/submit", body=body)
        mode = "review_submit"
    return {"mode": mode, "action_id": expected, "result": result.get("data", {}),
            "verification": verify_rules(client, args.stage, rules)}


def gray_action(client: KConfClient, args: argparse.Namespace, endpoint: str) -> Dict[str, Any]:
    require_confirmation(args)
    config = client.get_config()
    sub, _, _ = snapshot(config, args.stage)
    body = {"key": KEY, "snapshotId": sub.get("curConfig", {}).get("snapshotId"), "stage": args.stage, "ksn": None}
    result = client.request("POST", endpoint, body=body)
    return {"stage": args.stage, "previous_step": sub.get("stepCur"), "result": result.get("data", {})}


def sso_check() -> Dict[str, Any]:
    """只探测 KConf 域的 SSO 登录态，不验证业务接口权限。供本脚本单独调用，
    享受同 1 份 result=101 识别逻辑，避免在 short_drama_workflow.py 里重复实现。"""
    session = SmartSSOSession()
    try:
        response = session.request("GET", f"{BASE_URL}/api/w/config/get", params={"key": KEY}, timeout=10, allow_redirects=False)
    except SSOAuthRequiredError:
        return {"domain": "kconf.example.corp", "status": "expired", "detail": "SSO 登录态失效或待扫码"}
    except requests.Timeout:
        return {"domain": "kconf.example.corp", "status": "unreachable", "detail": "请求超时"}
    except requests.RequestException as exc:
        return {"domain": "kconf.example.corp", "status": "unreachable", "detail": str(exc)[:200]}
    if response.status_code in (401, 403):
        return {"domain": "kconf.example.corp", "status": "unauthorized", "detail": f"HTTP {response.status_code}，已登录但缺少权限"}
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if _is_login_required_payload(payload):
        return {"domain": "kconf.example.corp", "status": "expired", "detail": "接口返回「需要登录」（result=101），该域登录态未建立"}
    return {"domain": "kconf.example.corp", "status": "ok", "http_status": response.status_code}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sso-check")
    for command in ("query", "gray-next", "gray-cancel"):
        item = sub.add_parser(command)
        item.add_argument("--stage", default="production")
        item.add_argument("--item")
        item.add_argument("--concurrency", type=int, default=1)
        item.add_argument("--batch-size", type=int, default=10)
        item.add_argument("--max-runtime", type=int, default=300)
        item.add_argument("--authoritative-source", default="kconf")
        if command != "query":
            item.add_argument("--confirm", action="store_true")
    for command in ("preview", "apply", "check-action"):
        item = sub.add_parser(command)
        item.add_argument("--stage", default="production")
        item.add_argument("--item")
        item.add_argument("--concurrency", type=int, default=1)
        item.add_argument("--batch-size", type=int, default=10)
        item.add_argument("--max-runtime", type=int, default=300)
        item.add_argument("--authoritative-source", default="kconf")
        item.add_argument("--rules-file", required=True)
        if command != "preview":
            item.add_argument("--action-id", required=True)
            item.add_argument("--check-action")
        if command == "apply":
            item.add_argument("--comment", required=True)
            item.add_argument("--assignees")
            item.add_argument("--confirm", action="store_true")
    history = sub.add_parser("history")
    history.add_argument("--page", type=int, default=1)
    history.add_argument("--size", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "sso-check":
        data = sso_check()
        if data["status"] != "ok":
            error = "SSO_EXPIRED" if data["status"] == "expired" else "AUTH_FAILED" if data["status"] == "unauthorized" else "NETWORK_ERROR"
            code = 2 if data["status"] in ("expired", "unauthorized") else 1
            next_type = "ASK_USER" if data["status"] in ("expired", "unauthorized") else "STOP"
            emit({"ok": False, "code": code, "error": error, "msg": data["detail"], "data": data,
                  "next_action": {"type": next_type, "retryable": False}}, code)
        emit({"ok": True, "code": 0, "data": data, "next_action": {"type": "CONTINUE"}}, 0)
    client = KConfClient()
    if args.command == "query":
        data = render_query(client.get_config(), args.stage)
    elif args.command == "preview":
        data = preview(client, args.stage, args.rules_file)
    elif args.command == "check-action":
        rules = load_rules(args.rules_file)
        if args.action_id != action_id(args.stage, rules):
            raise SkillError("ACTION_MISMATCH", "action_id 与当前规则或环境不一致", 2, "ASK_USER")
        data = {"action_id": args.action_id, **verify_rules(client, args.stage, rules)}
    elif args.command == "apply":
        data = run_apply(client, args)
    elif args.command == "gray-next":
        data = gray_action(client, args, "/api/w/config/subconfig/gray/next")
    elif args.command == "gray-cancel":
        data = gray_action(client, args, "/api/w/config/subconfig/gray/cancel")
    else:
        data = client.request("GET", "/api/w/config/history/list", params={"key": KEY, "page": args.page, "size": args.size}).get("data", {})
    emit({"ok": True, "code": 0, "data": data, "next_action": {"type": "CONTINUE"}}, 0)


if __name__ == "__main__":
    try:
        main()
    except SkillError as exc:
        fail(exc.error, exc.msg, exc.code, exc.next_type)
    except KeyboardInterrupt:
        fail("CANCELLED", "用户已取消执行", 130)
    except Exception as exc:
        print(f"unexpected error: {exc}", file=sys.stderr)
        fail("UNEXPECTED_ERROR", "执行失败，请检查参数或稍后重试")
