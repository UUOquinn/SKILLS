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
"""联盟短剧暗投的只读编排入口：Data Agent → ConfigService → boostControl。"""
import argparse
import json
import os
import subprocess
import sys
from typing import Any

from dataclasses import dataclass

import requests
from auth_helper.sso_login_client import SmartAuthSession, SSOAuthRequiredError, get_username

DATA_AGENT_URL = "https://app.example.corp/rest/flow/api/v1/llm/agent/open/chat/completions/v2"
FLOW_QUERY_URL = "https://ops.example.corp/ops/rest/boostControl/query"
DATASET_NAME = "短剧-流量-效果数据"


@dataclass(frozen=True)
class SsoCheckTarget:
    domain: str
    probe_url: str
    probe_params: dict | None = None


# sso-check 逐域探测目标：Data Agent 与 boostControl 在此就地探测；ConfigService 域的登录态识别
# 与读取、写入都归 manage_config.py 单一持有（该域对未登录请求返回的“HTTP 200 + result=101”
# 经验只需在 ConfigService 自己的客户端里维护一份）。
SSO_CHECK_TARGETS = {
    "data_agent": SsoCheckTarget("tc.example.corp", "https://app.example.corp/"),
    "flow_control": SsoCheckTarget("ops.example.corp", "https://ops.example.corp/"),
}

_session: SmartAuthSession | None = None


def sso_session() -> SmartAuthSession:
    """进程内共享单个 Auth 会话，避免重复初始化与重复加载 cookie 状态。"""
    global _session
    if _session is None:
        _session = SmartAuthSession()
    return _session


class WorkflowError(Exception):
    def __init__(self, error: str, msg: str, code: int = 1, action: str = "STOP"):
        self.error, self.msg, self.code, self.action = error, msg, code, action
        super().__init__(msg)


def emit(ok: bool, code: int, data: Any = None, error: str | None = None,
         msg: str | None = None, action: str = "CONTINUE") -> None:
    result = {"ok": ok, "code": code, "next_action": {"type": action, "retryable": error == "API_TIMEOUT"}}
    if data is not None:
        result["data"] = data
    if error:
        result["error"] = error
    if msg:
        result["msg"] = msg
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(code)


def parse_json_line(text: str) -> dict:
    for line in reversed(text.splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise WorkflowError("INVALID_RESPONSE", "下游脚本未返回标准 JSON")


def run_config(args: list[str]) -> dict:
    command = ["uv", "run", os.path.join(os.path.dirname(__file__), "manage_config.py"), *args]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=360)
    response = parse_json_line(completed.stdout)
    if completed.returncode != 0 or not response.get("ok"):
        raise WorkflowError(response.get("error", "CONFIG_FAILED"), response.get("msg", "ConfigService 调用失败"), response.get("code", 1), response.get("next_action", {}).get("type", "STOP"))
    return response["data"]


def data_agent(question: str, agent_id: int) -> dict:
    username = get_username()
    if not username:
        raise WorkflowError("AUTH_FAILED", "无法获取当前 Auth 用户", 2, "ASK_USER")
    body = {"agentId": agent_id, "module": "DATA_AGENT", "question": f"仅使用数据集「{DATASET_NAME}」。{question}", "userName": username, "extraInfo": {"externalSessionKey": os.getenv("KS_AGENT_SESSION_ID", ""), "externalChatId": os.getenv("KS_AGENT_CHAT_ID", "")}, "stream": False}
    try:
        response = sso_session().request("POST", DATA_AGENT_URL, json=body, timeout=(10, 300), extra_config={"obo_required": True})
    except SystemExit as exc:
        # SmartAuthSession 的 obo 探测拿不到 token 时会在库内部直接 sys.exit(1)，
        # 这里转换成标准 JSON 错误，避免上游拿到非结构化输出。
        raise WorkflowError("AUTH_EXPIRED", "Data Agent 所需的 OBO 登录态缺失，请完成内网 Auth 登录后重试", 2, "ASK_USER") from exc
    except SSOAuthRequiredError as exc:
        raise WorkflowError("AUTH_EXPIRED", "Data Agent Auth 登录态已失效或等待扫码，请完成内网登录后重试", 2, "ASK_USER") from exc
    except requests.Timeout as exc:
        raise WorkflowError("API_TIMEOUT", "Data Agent 请求超时") from exc
    except requests.RequestException as exc:
        raise WorkflowError("NETWORK_ERROR", f"无法请求 Data Agent：{exc}") from exc
    if response.status_code in (401, 403):
        raise WorkflowError("AUTH_FAILED", "当前账号缺少 Data Agent 权限", 2, "ASK_USER")
    if response.status_code >= 500:
        raise WorkflowError("API_5XX", f"Data Agent 服务异常（HTTP {response.status_code}）")
    if not response.ok:
        raise WorkflowError("API_4XX", f"Data Agent 请求失败（HTTP {response.status_code}）：{response.text[:300]}")
    return response.json()


def is_cpm_strategy(item: dict) -> bool:
    return item.get("name") == "CPM顶价"


def cpm_strategies(status: int | None) -> list[dict]:
    body = {"pager": {"pageNum": 1, "pageSize": 100}, "industryVersion": "6.6"}
    if status is not None:
        body["status"] = status
    headers = {"Origin": "https://ops.example.corp", "Referer": "https://ops.example.corp/flow"}
    try:
        response = sso_session().request("POST", FLOW_QUERY_URL, json=body, headers=headers, timeout=(10, 60), allow_redirects=False)
    except SSOAuthRequiredError as exc:
        raise WorkflowError("AUTH_EXPIRED", "boostControl Auth 登录态已失效或等待扫码，请完成内网登录后重试", 2, "ASK_USER") from exc
    except requests.Timeout as exc:
        raise WorkflowError("API_TIMEOUT", "boostControl 请求超时") from exc
    except requests.RequestException as exc:
        raise WorkflowError("NETWORK_ERROR", f"无法请求 boostControl：{exc}") from exc
    if response.status_code in (301, 302, 401, 403):
        raise WorkflowError("AUTH_FAILED", "当前账号缺少 boostControl 权限或 Auth 已失效", 2, "ASK_USER")
    if response.status_code >= 500:
        raise WorkflowError("API_5XX", f"boostControl 服务异常（HTTP {response.status_code}）")
    if not response.ok:
        raise WorkflowError("API_4XX", f"boostControl 请求失败（HTTP {response.status_code}）：{response.text[:300]}")
    envelope = response.json()
    if envelope.get("status") not in (None, 0, 200, "0", "200"):
        raise WorkflowError("API_BUSINESS_ERROR", envelope.get("message", "boostControl 业务失败"))
    data = envelope.get("data", {})
    rows = data.get("data", []) if isinstance(data, dict) else []
    return [{"id": row.get("id"), "name": row.get("name"), "status": row.get("status"), "statusDesc": row.get("statusDesc"), "scope": row.get("scope"), "adCluster": row.get("adCluster"), "mediaCluster": row.get("mediaCluster"), "beginTime": row.get("beginTime"), "endTime": row.get("endTime")} for row in rows if is_cpm_strategy(row)]

def probe_domain(target: SsoCheckTarget) -> dict:
    """对单个域发一次轻量 GET，按响应分类 Auth 登录态。只验证登录态，不验证业务接口权限。"""
    try:
        response = sso_session().request("GET", target.probe_url, params=target.probe_params, timeout=10, allow_redirects=False)
    except SSOAuthRequiredError:
        return {"domain": target.domain, "status": "expired", "detail": "Auth 登录态失效或待扫码"}
    except requests.Timeout:
        return {"domain": target.domain, "status": "unreachable", "detail": "请求超时"}
    except requests.RequestException as exc:
        return {"domain": target.domain, "status": "unreachable", "detail": str(exc)[:200]}
    if response.status_code in (401, 403):
        return {"domain": target.domain, "status": "unauthorized", "detail": f"HTTP {response.status_code}，已登录但缺少权限"}
    return {"domain": target.domain, "status": "ok", "http_status": response.status_code}


def probe_config() -> dict:
    """ConfigService 域的登录态识别逻辑与写入共享一份，直接复用 manage_config.py 的 sso-check，
    避免在此处重复实现 ConfigService 特有的“HTTP 200 + result=101”识别。超时设为 240 秒：
    子进程重新初始化 SmartAuthSession 时，未登录域的认证轮询可能耗时达到数分钟。"""
    command = ["uv", "run", os.path.join(os.path.dirname(__file__), "manage_config.py"), "sso-check"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        return {"domain": "config.example.corp", "status": "unreachable", "detail": "ConfigService sso-check 超时，可能正在等待扫码确认"}
    try:
        response = parse_json_line(completed.stdout)
    except WorkflowError:
        return {"domain": "config.example.corp", "status": "unreachable", "detail": "ConfigService sso-check 未返回标准 JSON"}
    data = response.get("data")
    if isinstance(data, dict) and "domain" in data:
        return data
    return {"domain": "config.example.corp", "status": "unreachable", "detail": response.get("msg", "ConfigService sso-check 调用异常")}


def sso_check() -> None:
    domains = {key: probe_domain(target) for key, target in SSO_CHECK_TARGETS.items()}
    domains["configservice"] = probe_config()
    statuses = {item["status"] for item in domains.values()}
    if statuses <= {"ok"}:
        emit(True, 0, {"all_ok": True, "domains": domains})
    data = {"all_ok": False, "domains": domains}
    if "expired" in statuses:
        emit(False, 2, data, error="AUTH_EXPIRED", msg="存在 Auth 登录态失效的域，请完成内网登录后重试", action="ASK_USER")
    if "unauthorized" in statuses:
        emit(False, 2, data, error="AUTH_FAILED", msg="存在已登录但缺少权限的域，请确认当前账号权限", action="ASK_USER")
    emit(False, 1, data, error="NETWORK_ERROR", msg="存在无法连接的域，请检查内网网络", action="STOP")


def main() -> None:
    parser = argparse.ArgumentParser(description="联盟短剧暗投配置读取与编排")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sso-check")
    p = sub.add_parser("data")
    p.add_argument("--question", required=True)
    p.add_argument("--agent-id", type=int, required=True)
    p = sub.add_parser("configservice")
    p.add_argument("--stage", default="production")
    p = sub.add_parser("cpm")
    p.add_argument("--status", type=int, default=None)
    p = sub.add_parser("run")
    p.add_argument("--question", required=True)
    p.add_argument("--agent-id", type=int, required=True)
    p.add_argument("--stage", default="production")
    p.add_argument("--status", type=int, default=None)
    args = parser.parse_args()
    if args.command == "sso-check":
        sso_check()
    elif args.command == "data":
        result = {"data_agent": data_agent(args.question, args.agent_id)}
    elif args.command == "configservice":
        result = {"configservice": run_config(["query", "--stage", args.stage])}
    elif args.command == "cpm":
        result = {"cpm_topbid_strategies": cpm_strategies(args.status)}
    else:
        result = {"data_agent": data_agent(args.question, args.agent_id), "configservice": run_config(["query", "--stage", args.stage]), "cpm_topbid_strategies": cpm_strategies(args.status), "sequence": ["data_agent", "configservice", "boostControl"]}
    emit(True, 0, result)


if __name__ == "__main__":
    try:
        main()
    except WorkflowError as exc:
        emit(False, exc.code, error=exc.error, msg=exc.msg, action=exc.action)
    except KeyboardInterrupt:
        emit(False, 130, error="INTERRUPTED", msg="用户已取消", action="STOP")
    except Exception as exc:
        print(f"unexpected error: {exc}", file=sys.stderr)
        emit(False, 1, error="UNEXPECTED_ERROR", msg="执行失败，请检查参数或稍后重试", action="STOP")
