#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#   "requests>=2.31.0,<3",
#   "ks-aimate>=1.0.30",
#   "openpyxl>=3.1.0,<4",
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
"""flowControl API-only CLI. No cookies, browser automation, or UI fallback."""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

import requests
from ks_aimate.sso_login_client import SmartSSOSession

BASE_URL = "https://ops-platform.example.corp/ops/rest"
FLOW_URL = f"{BASE_URL}/flowControl"
DEFAULT_TIMEOUT = 30
DROP_FIELDS = {
    "_apiPrefix", "_strategyType", "status", "statusDesc", "creatorName",
    "createTime", "updateTime", "isDel", "lastStatus", "greyStartTime",
    "expireSoon", "snapshot", "toolBar", "displayQuickPushBtn",
    "displayProhibitionPeriodQuickPushBtn", "displayPushBtn", "ruleTypes",
    "rangeTime",
}
# 可增删名单的维度：广告主信息模板 -> adCluster，媒体信息模板 -> mediaCluster。
# 值来自 get 详情中的逗号分隔字符串字段，非名单字段（如 osType、placements）不允许操作。
LIST_DIMS = {
    # 广告主信息模板（adCluster）
    "accountId": ("adCluster", "广告主ID"),
    "product": ("adCluster", "产品"),
    "secondIndustryV6": ("adCluster", "二级行业"),
    "campaignType": ("adCluster", "推广目的"),
    "ocpxActionType": ("adCluster", "优化目标"),
    # 媒体信息模板（mediaCluster）
    "appId": ("mediaCluster", "媒体应用ID"),
    "uid": ("mediaCluster", "媒体用户ID"),
    "posId": ("mediaCluster", "广告位ID"),
    "adStyle": ("mediaCluster", "广告样式"),
}
# 纯数字维度：增删时校验 ID 必须是数字，防止把产品名等写进账户 ID 字段
NUMERIC_DIMS = {"accountId", "appId", "uid", "posId"}


class FlowError(Exception):
    def __init__(self, error: str, msg: str, *, code: int = 1,
                 action: str = "STOP", retryable: bool = False):
        super().__init__(msg)
        self.error = error
        self.msg = msg
        self.code = code
        self.action = action
        self.retryable = retryable


_STARTED_AT = time.perf_counter()


def emit(payload: dict[str, Any], exit_code: int = 0) -> None:
    payload.setdefault("duration_ms",
                       int((time.perf_counter() - _STARTED_AT) * 1000))
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    sys.exit(exit_code)


def ok(data: Any, msg: str = "请求成功") -> None:
    emit({"ok": True, "code": 0, "msg": msg, "data": data,
          "next_action": {"type": "CONTINUE"}})


def parse_json(value: str, label: str) -> dict[str, Any]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise FlowError("INVALID_ARGUMENT", f"{label} 不是合法 JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise FlowError("INVALID_ARGUMENT", f"{label} 必须是 JSON 对象")
    return data


class FlowClient:
    def __init__(self, timeout: int = DEFAULT_TIMEOUT):
        self.timeout = timeout
        self.session = SmartSSOSession()
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://ops-platform.example.corp",
            "Referer": "https://ops-platform.example.corp/flow",
        }

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                body: Any = None, raw: bool = False) -> Any:
        url = f"{FLOW_URL}/{path}"
        try:
            response = self.session.request(
                method, url, params=params, json=body, headers=self.headers,
                timeout=self.timeout, allow_redirects=False,
            )
        except requests.Timeout as exc:
            raise FlowError("API_TIMEOUT", "Operation API 请求超时", action="RETRY_ONCE_THEN_STOP", retryable=True) from exc
        except requests.ConnectionError as exc:
            raise FlowError("NETWORK_ERROR", f"Operation API 网络不可达: {exc}") from exc
        except requests.RequestException as exc:
            raise FlowError("API_REQUEST_FAILED", f"Operation API 请求失败: {exc}") from exc

        if response.status_code in (301, 302, 307, 308, 401):
            raise FlowError("SSO_EXPIRED", "当前 SSO 身份无效，请重新完成内网认证后再试", code=2, action="ASK_USER")
        if response.status_code == 403:
            raise FlowError("AUTH_FAILED", "当前账号无 flowControl 接口权限", code=2, action="ASK_USER")
        if response.status_code >= 500:
            raise FlowError("API_5XX", f"Operation 服务异常（HTTP {response.status_code}）")
        if response.status_code >= 400:
            raise FlowError("API_4XX", f"Operation 请求失败（HTTP {response.status_code}）: {response.text[:300]}")
        if raw:
            return response

        try:
            envelope = response.json()
        except ValueError as exc:
            raise FlowError("INVALID_RESPONSE", f"Operation 返回非 JSON: {response.text[:300]}") from exc
        status = envelope.get("status", envelope.get("code"))
        message = envelope.get("message") or envelope.get("msg") or "业务请求失败"
        if status in (411, 412, "411", "412"):
            # 服务端把 411 当通用校验失败码使用，真实原因在 message 里。
            # 只有确实提到行业版本时才是 INDUSTRY_VERSION_MISMATCH，
            # 否则按业务校验失败展示真实原因，避免误导用户去查行业版本。
            if "行业版本" in str(message) or "industryVersion" in str(message):
                raise FlowError(
                    "INDUSTRY_VERSION_MISMATCH",
                    f"{message}。API-only 模式不会转 UI；请确认 industryVersion 后重试",
                )
            raise FlowError(
                "VALIDATION_FAILED",
                validation_hint(str(message)),
            )
        if status not in (None, 0, 200, "0", "200"):
            raise FlowError("API_BUSINESS_ERROR", f"Operation 业务错误 {status}: {message}")
        return envelope.get("data", envelope.get("result", envelope))

    def get(self, strategy_id: int) -> dict[str, Any]:
        data = self.request("GET", "get", params={"id": strategy_id})
        # 不存在时服务端仍返回 200 成功壳（无 id/name 字段），必须识别为查无此策略，
        # 否则后续 add-ids/edit 会把空壳当详情提审，对不存在的策略盲目写
        if not isinstance(data, dict) or not data or "id" not in data:
            raise FlowError("NOT_FOUND", f"未找到扶持策略 {strategy_id}")
        return data

    def query(self, body: dict[str, Any]) -> Any:
        body.setdefault("pager", {"pageNum": 1, "pageSize": 20})
        body.setdefault("industryVersion", "6.6")
        return self.request("POST", "query", body=body)

    def sanitize(self, detail: dict[str, Any]) -> dict[str, Any]:
        body = copy.deepcopy(detail)
        for key in list(body):
            if key in DROP_FIELDS or key.startswith("_"):
                body.pop(key, None)
        for key in ("adCluster", "mediaCluster"):
            cluster = body.get(key)
            if isinstance(cluster, dict) and isinstance(cluster.get("maxNum"), dict):
                cluster.pop("maxNum", None)
        ad_cluster = body.get("adCluster")
        if isinstance(ad_cluster, dict):
            ad_cluster.setdefault("industryVersion", "6.6")
        if not str(body.get("background") or "").strip():
            body["background"] = str(body.get("name") or "扶持策略调整")
        return body

    def edit(self, strategy_id: int, patch: dict[str, Any]) -> Any:
        detail = self.get(strategy_id)
        assert_writable(detail, strategy_id)
        body = self.sanitize({**detail, **patch})
        body["id"] = detail.get("id", strategy_id)
        return self.request("POST", "mergeEdit", body=body)

    def create(self, item: dict[str, Any]) -> Any:
        if "list" in item:
            raise FlowError("BATCH_NOT_SUPPORTED", "只支持单条新建，请直接传一个策略对象")
        body = self.sanitize(item)
        return self.request("POST", "batchAddV2", body={"list": [body]})

    def update_list(self, strategy_id: int, dim: str, ids: str, remove: bool) -> Any:
        """在指定策略的某个维度模板上新增/删除 ID 名单并提审。

        前提是调用方已提供明确的策略 ID：detail 必然存在（先 GET 核对）。
        提交复用 mergeEdit 的「先 GET 全量，再合并 patch」语义：
        先校验维度与现有名单，再做集合增删，然后白名单裁剪并提审。
        """
        if dim not in LIST_DIMS:
            raise FlowError(
                "INVALID_DIMENSION",
                f"不支持的维度 {dim}。支持：{', '.join(sorted(LIST_DIMS))}",
            )
        add = parse_ids(ids, dim)
        if not add:
            raise FlowError("INVALID_ARGUMENT", "ids 为空或格式非法")
        detail = self.get(strategy_id)
        assert_writable(detail, strategy_id)
        cluster_key, label = LIST_DIMS[dim]
        cluster = detail.get(cluster_key) or {}
        existing = parse_ids(str(cluster.get(dim) or ""), dim, strict=False)
        if remove:
            target = [x for x in existing if x in add]
            new_ids = [x for x in existing if x not in add]
        else:
            target = [x for x in add if x not in existing]
            new_ids = existing + target
        verb = "删除" if remove else "新增"
        if not target:
            reason = "ALREADY_ABSENT" if remove else "ALREADY_PRESENT"
            return {
                "skipped": True, "reason": reason, "strategyId": strategy_id,
                "dimension": dim, "label": label, "ids": ids, "existing": existing,
            }
        body = self.sanitize(detail)
        body.setdefault(cluster_key, {})
        body[cluster_key][dim] = ",".join(new_ids) if new_ids else ""
        if not str(body.get("background") or "").strip():
            body["background"] = str(body.get("name") or "名单调整")
        body["id"] = detail.get("id", strategy_id)
        self.request("POST", "mergeEdit", body=body)
        return {
            "strategyId": strategy_id, "dimension": dim, "label": label,
            "action": verb, "changed": target, "existing_before": existing,
            "existing_after": new_ids,
        }


def positive_id(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("策略 ID 必须是正整数") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("策略 ID 必须是正整数")
    return result


def human_time(value: Any) -> str:
    """13 位毫秒时间戳转本地可读时间（YYYY-MM-DD HH:MM），非法值原样返回。"""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return str(value)
    if ms <= 0:
        return "未设置"
    if ms < 100000000000:  # 10 位秒级时间戳
        ms *= 1000
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(value)


def parse_ids(value: str, dim: str, strict: bool = True) -> list[str]:
    """把逗号/分号/顿号分隔的名单拆成去重的字符串列表。

    strict 模式用于用户输入：纯数字维度必须全部是数字，
    防止把产品名或账户名误写进 accountId/appId/uid/posId。
    """
    ids: list[str] = []
    for chunk in re.split(r"[,;，；、]", str(value or "")):
        token = chunk.strip()
        if token and token not in ids:
            if strict and dim in NUMERIC_DIMS and not token.isdigit():
                raise FlowError(
                    "INVALID_ARGUMENT",
                    f"维度 {dim} 只接受数字 ID，收到非法值「{token}」",
                )
            ids.append(token)
    return ids


# 状态枚举：change-status 的 status 参数语义
STATUS_LABELS = {
    "0": "待提交",
    "1": "待审核",
    "2": "发布成功",
    "3": "审核拒绝",
    "4": "生效中",
    "5": "已失效",
    "6": "审核通过",
    "12": "封禁期",
    "13": "封禁期待推全",
}
# 立即推全类型语义（type 参数）
PUSH_TYPE_LABELS = {
    0: "普通立即推全",
    1: "提交封禁期审批",
    2: "封禁期立即推全",
}


def status_label(value: Any) -> str:
    """状态数字/文本转中文标签，未知状态原样返回。"""
    if isinstance(value, bool) or value is None:
        return "未知"
    label = STATUS_LABELS.get(str(value))
    if label:
        return label
    for k, v in STATUS_LABELS.items():
        if v == str(value):
            return v
    return str(value)


# 服务端校验失败的常见场景 → 明确下一步建议；未命中才走通用「补必填字段」文案
VALIDATION_HINTS = (
    ("审核流程中不能更新", "策略正在审核流程中：先等审核结果，或先撤回再修改"),
    ("审核中不能", "策略正在审核流程中：先等审核结果，或先撤回再修改"),
    ("状态不合法", "状态流转路径不合法：change-status 支持人工流转「0待提交→1提审」「1待审核→0撤回」；审核通过/发布/生效由平台审核流程驱动，无法用 API 硬跳"),
    ("无法应用于状态", "当前策略状态不支持该操作：先确认状态，必要时撤回后重试"),
    ("不能为空", "缺少必填字段：对照 API.md 补全后重试"),
    ("为空", "缺少必填字段：对照 API.md 补全后重试"),
    ("请输入正确", "字段格式或取值不合法：对照 API.md 检查后重试"),
    ("格式不正确", "字段格式不合法：对照 API.md 检查后重试"),
)


def validation_hint(message: str) -> str:
    """把服务端的校验失败信息翻译成带下一步建议的提示。"""
    for pattern, hint in VALIDATION_HINTS:
        if pattern in str(message):
            return f"{message}。{hint}"
    return f"{message}。请对照 API.md 补全字段后重试"


def brief(detail: dict[str, Any]) -> dict[str, Any]:
    """单条策略的关键字段摘要：状态信号浮出、时间戳转人类可读。

    让用户在聊天里一眼看清「是否生效、还能写吗、时间范围」，
    不再被 4KB 全量 JSON 和 13 位毫秒时间戳淹没。
    """
    ac = detail.get("adCluster") or {}
    mc = detail.get("mediaCluster") or {}
    writable = True
    if detail.get("isDel"):
        writable = False
    elif detail.get("status") == 5:
        writable = False
    return {
        "id": detail.get("id"),
        "name": detail.get("name"),
        "type": detail.get("type"),
        "typeDesc": detail.get("typeDesc"),
        "status": detail.get("status"),
        "statusDesc": detail.get("statusDesc") or status_label(detail.get("status")),
        "isDel": bool(detail.get("isDel")),
        "writable": writable,
        "beginTime": detail.get("beginTime"),
        "beginTimeText": human_time(detail.get("beginTime")),
        "endTime": detail.get("endTime"),
        "endTimeText": human_time(detail.get("endTime")),
        "creatorName": detail.get("creatorName"),
        "background": detail.get("background"),
        "scope": detail.get("scope"),
        "accountId": ac.get("accountId"),
        "product": ac.get("product"),
        "secondIndustryV6": ac.get("secondIndustryV6"),
        "campaignType": ac.get("campaignType"),
        "ocpxActionType": ac.get("ocpxActionType"),
        "appId": mc.get("appId"),
        "uid": mc.get("uid"),
        "posId": mc.get("posId"),
        "adStyle": mc.get("adStyle"),
        "bonusRate": detail.get("bonusRate"),
    }


def assert_writable(detail: dict[str, Any], strategy_id: int) -> None:
    """写操作前置门禁：已失效/已删除的策略不能作为增删改的对象。

    原因：delete 后 get 仍能查到 isDel=true 的记录（逻辑删除），
    对这类策略执行 mergeEdit/add-ids 要么无效要么污染数据，必须拦截。
    """
    if not isinstance(detail, dict):
        raise FlowError("NOT_FOUND", f"未找到扶持策略 {strategy_id}")
    if detail.get("isDel"):
        raise FlowError(
            "NOT_WRITABLE",
            f"策略 {strategy_id}「{detail.get('name')}」已被删除，不能执行写操作",
        )
    status = detail.get("status")
    if status == 5:
        raise FlowError(
            "NOT_WRITABLE",
            f"策略 {strategy_id}「{detail.get('name')}」已失效（status=5），不能执行写操作",
        )
    # 审核流程中的策略不能直接编辑/改名单（服务端拒绝），给出明确下一步而不是撞墙
    if status == 1:
        raise FlowError(
            "NOT_WRITABLE",
            f"策略 {strategy_id}「{detail.get('name')}」正在审核中（status=1），"
            "不能修改：先等审核结果，或先撤回再改（change-status 退回待提交）",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="扶持策略 flowControl API-only CLI")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    sub = parser.add_subparsers(dest="command", required=True)

    get_p = sub.add_parser("get", help="查询单条详情")
    get_p.add_argument("id", type=positive_id)
    get_p.add_argument("--brief", action="store_true",
                       help="只返回关键字段摘要（状态/时间/名单，时间已转可读格式）")

    query_p = sub.add_parser("query", help="分页查询")
    query_p.add_argument("body", nargs="?", default="{}")

    create_p = sub.add_parser("create", help="新建单条策略")
    create_p.add_argument("body")

    edit_p = sub.add_parser("edit", help="修改单条策略并提审")
    edit_p.add_argument("id", type=positive_id)
    edit_p.add_argument("--set", dest="patch", required=True)

    status_p = sub.add_parser("change-status", help="单条状态流转")
    status_p.add_argument("id", type=positive_id)
    status_p.add_argument(
        "status",
        help="目标状态：0待提交 / 1待审核 / 2发布成功 / 4生效中 / 5已失效 / 6审核通过 / 12封禁期 / 13封禁期待推全",
    )
    status_p.add_argument("--confirm", action="store_true")

    delete_p = sub.add_parser("delete", help="删除单条策略")
    delete_p.add_argument("id", type=positive_id)

    log_p = sub.add_parser("log", help="查询状态时间线")
    log_p.add_argument("id", type=positive_id)

    diff_p = sub.add_parser("diff", help="查询版本 diff")
    diff_p.add_argument("id", type=positive_id)

    push_p = sub.add_parser("quick-push", help="单条立即推全")
    push_p.add_argument("id", type=positive_id)
    push_p.add_argument("--type", type=int, choices=(0, 1, 2), default=0,
                        help="0普通立即推全（默认）/ 1提交封禁期审批 / 2封禁期立即推全")
    push_p.add_argument("--confirm", action="store_true")

    export_p = sub.add_parser("export", help="导出查询结果到本地 xlsx")
    export_p.add_argument("body", nargs="?", default="{}")
    export_p.add_argument("--output", required=True,
                          help="输出文件路径（绝对路径；目录不存在时自动创建）")

    for name, help_text, dims in (
        ("add-ids", "在指定策略的维度模板上新增 ID 并提审", ["appId", "uid", "posId", "accountId", "product", "secondIndustryV6", "campaignType", "ocpxActionType", "adStyle"]),
        ("remove-ids", "从指定策略的维度模板删除 ID 并提审", ["appId", "uid", "posId", "accountId", "product", "secondIndustryV6", "campaignType", "ocpxActionType", "adStyle"]),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("id", type=positive_id)
        p.add_argument("--dim", required=True, choices=dims)
        p.add_argument("--ids", required=True, help="逗号分隔的 ID/名称列表")
    return parser


def run(args: argparse.Namespace) -> None:
    client = FlowClient(args.timeout)
    cmd = args.command
    if cmd == "get":
        detail = client.get(args.id)
        if args.brief:
            ok(brief(detail), f"策略 {detail.get('id')} 摘要")
        else:
            ok({"detail": detail,
                "writable": not (detail.get("isDel") or detail.get("status") == 5),
                "statusDesc": detail.get("statusDesc") or status_label(detail.get("status")),
                "beginTimeText": human_time(detail.get("beginTime")),
                "endTimeText": human_time(detail.get("endTime"))},
               "策略详情")
    if cmd == "query":
        data = client.query(parse_json(args.body, "查询条件"))
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            data["data"] = [brief(r) for r in data["data"]]
        ok(data)
    if cmd == "create":
        ok(client.create(parse_json(args.body, "策略对象")), "单条策略已提交新建")
    if cmd == "edit":
        ok(client.edit(args.id, parse_json(args.patch, "修改字段")), "单条策略已修改并提审")
    if cmd == "change-status":
        params = {"id": args.id, "status": args.status}
        if args.confirm:
            params["confirm"] = "true"
        ok(client.request("POST", "changeStatus", params=params, body={}), "状态流转已提交")
    if cmd == "delete":
        ok(client.request("DELETE", "delete", body=[args.id]), "单条策略已删除")
    if cmd == "add-ids":
        result = client.update_list(args.id, args.dim, args.ids, remove=False)
        msg = ("名单已是最新，无需提审" if result.get("skipped")
               else "名单新增已提审")
        ok(result, msg)
    if cmd == "remove-ids":
        result = client.update_list(args.id, args.dim, args.ids, remove=True)
        msg = ("名单已是最新，无需提审" if result.get("skipped")
               else "名单删除已提审")
        ok(result, msg)
    if cmd == "log":
        ok(client.request("GET", "log", params={"id": args.id}))
    if cmd == "diff":
        ok(client.request("POST", "showDiffInfo", params={"id": args.id}, body={}))
    if cmd == "quick-push":
        params = {"id": args.id, "type": args.type}
        if args.confirm:
            params["confirm"] = "true"
        ok(client.request("POST", "quickPushAll", params=params, body={}), "立即推全已受理")
    if cmd == "export":
        # export 接口仅返回 JSON 壳（受理回执），从不返回文件流；
        # 真实导出路径是 query 取数据、本地生成 xlsx。
        body = parse_json(args.body, "导出条件")
        pager = dict(body.get("pager") or {"pageNum": 1, "pageSize": 500})
        pager.setdefault("pageNum", 1)
        pager.setdefault("pageSize", 500)
        body["pager"] = pager
        page = client.query(body)
        rows = []
        if isinstance(page, dict):
            rows = page.get("data") if isinstance(page.get("data"), list) else [page]
        elif isinstance(page, list):
            rows = page
        if not rows:
            raise FlowError("EXPORT_FAILED",
                            "没有符合导出条件的策略，已跳过生成文件")
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "flow策略"
        headers = ["策略ID", "名称", "类型", "状态", "提交人", "生效时间", "到期时间",
                   "背景", "广告主账户", "媒体应用", "媒体用户", "广告位"]
        ws.append(headers)
        for r in rows:
            ac = r.get("adCluster") or {}
            mc = r.get("mediaCluster") or {}
            ws.append([
                r.get("id"), r.get("name"), r.get("typeDesc") or r.get("type"),
                r.get("statusDesc") or r.get("status"), r.get("creatorName"),
                human_time(r.get("beginTime")), human_time(r.get("endTime")),
                r.get("background"),
                ac.get("accountId"), mc.get("appId"), mc.get("uid"),
                mc.get("posId"),
            ])
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        wb.save(output)
        ok({"output": str(output), "rows": len(rows)}, "导出完成")


def main() -> None:
    try:
        run(build_parser().parse_args())
    except KeyboardInterrupt:
        emit({"ok": False, "code": 130, "error": "INTERRUPTED", "msg": "用户中断执行",
              "next_action": {"type": "STOP", "retryable": False}}, 130)
    except FlowError as exc:
        print(f"{exc.error}: {exc.msg}", file=sys.stderr)
        emit({"ok": False, "code": exc.code, "error": exc.error, "msg": exc.msg,
              "next_action": {"type": exc.action, "retryable": exc.retryable}}, exc.code)
    except Exception as exc:
        print(f"UNEXPECTED_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        emit({"ok": False, "code": 1, "error": "UNEXPECTED_ERROR", "msg": str(exc),
              "next_action": {"type": "STOP", "retryable": False}}, 1)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal.default_int_handler)
    main()
