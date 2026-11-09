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

import argparse
import json
import sys
import time
from typing import Any

import requests
from auth_helper.sso_login_client import SmartAuthSession

BASE_URL = "https://app.example.corp/rest/ssp-admin/"
MAX_TASKS = 50
ENDPOINTS = {
    "account.allContract": ("GET", "account/allContract"),
    "account.allCorporationName": ("GET", "account/allCorporationName"),
    "account.allUid": ("GET", "account/allUid"),
    "account.checkKsId": ("GET", "account/checkKsId"),
    "account.companyInfo": ("GET", "account/companyInfo"),
    "account.contactSearch": ("POST", "account/contactSearch"),
    "account.list": ("POST", "account/list"),
    "account.registerFrom": ("GET", "account/registerFrom"),
    "account.sameCorporationNameCount": ("GET", "account/sameCorporationNameCount"),
    "account.suggestSearch": ("POST", "account/suggestSearch"),
    "account.tagList": ("GET", "account/tagList"),
    "account.uid2NameList": ("GET", "account/uid2NameList"),
    "invitationCode.getCode": ("GET", "invitationCode/getCode"),
    "invitationCode.invitationCodeInfo": ("GET", "invitationCode/invitationCodeInfo"),
    "invitationCode.search": ("POST", "invitationCode/search"),
    "companyNewSettle.appInfo": ("GET", "companyNewSettle/appInfo"),
    "companyNewSettle.companyNewSettle": ("GET", "companyNewSettle/companyNewSettle"),
    "companyNewSettle.corporationNameConflict": ("GET", "companyNewSettle/corporationNameConflict"),
    "companyNewSettle.searchByCompanyName": ("GET", "companyNewSettle/searchByCompanyName"),
    "companySettle.companySettleInfo": ("GET", "companySettle/companySettleInfo"),
    "companySettle.corporationNameConflict": ("GET", "companySettle/corporationNameConflict"),
    "companySettle.invoiceType": ("GET", "companySettle/invoiceType"),
    "companySettle.searchByCompanyName": ("GET", "companySettle/searchByCompanyName"),
    "contract.searchByUid": ("GET", "contract/searchByUid"),
    "ecology.noCostAppInfo": ("POST", "ecology/noCostAppInfo"),
    "ecology.search": ("POST", "ecology/search"),
    "apiAudit.search": ("POST", "apiAudit/search"),
    "app.appStoreList": ("GET", "app/appStoreList"),
    "app.checkSha1Operator": ("GET", "app/checkSha1Operator"),
    "app.getContentApp": ("GET", "app/getContentApp"),
    "app.id2NameList": ("GET", "app/id2NameList"),
    "app.list": ("GET", "app/list"),
    "app.listAllApp": ("POST", "app/listAllApp"),
    "app.packageNameCheck": ("GET", "app/packageNameCheck"),
    "app.queryAppCert": ("POST", "app/queryAppCert"),
    "app.queryHistoryFile": ("POST", "app/queryHistoryFile"),
    "app.search": ("POST", "app/search"),
    "position.allPositionId": ("GET", "position/allPositionId"),
    "position.allPositionName": ("GET", "position/allPositionName"),
    "position.get": ("GET", "position/get"),
    "position.getCpmEditRecordView": ("POST", "position/getCpmEditRecordView"),
    "position.id2NameList": ("POST", "position/id2NameList"),
    "position.list": ("GET", "position/list"),
    "position.search": ("POST", "position/search"),
    "position.showCoopMode": ("GET", "position/showCoopMode"),
    "content.allEnterSceneStyle": ("GET", "content/allEnterSceneStyle"),
    "content.get": ("GET", "content/get"),
    "content.getAllContentConf": ("GET", "content/getAllContentConf"),
    "content.getContentAdConf": ("GET", "content/getContentAdConf"),
    "content.list": ("GET", "content/list"),
    "content.listByContentId": ("GET", "content/listByContentId"),
    "content.search": ("POST", "content/search"),
    "traffic.allTrafficName": ("GET", "traffic/allTrafficName"),
    "traffic.list": ("POST", "traffic/list"),
    "traffic.positionValid": ("POST", "traffic/positionValid"),
    "traffic.shield": ("GET", "traffic/shield"),
    "appCert.industryCertInfoDetail": ("POST", "appCert/industryCertInfoDetail"),
    "appCert.industryCertInfoList": ("POST", "appCert/industryCertInfoList"),
    "appCert.listCertInfo": ("POST", "appCert/listCertInfo"),
    "positionMap.search": ("POST", "positionMap/search"),
    "settlement.appShareInfo": ("GET", "settlement/appShareInfo"),
    "settlement.contract": ("GET", "settlement/contract"),
    "settlement.querySettlementInfo": ("POST", "settlement/querySettlementInfo"),
    "settlement.search": ("POST", "settlement/search"),
    "settlement.statusLog": ("GET", "settlement/statusLog"),
    "recordTime.searchFinanceTime": ("POST", "recordTime/searchFinanceTime"),
    "settlement.reviewSearch": ("POST", "settlement/reviewSearch"),
    "recordTime.searchSettleTime": ("POST", "recordTime/searchSettleTime"),
    "dailyReport.search": ("POST", "dailyReport/search"),
    "mediaPush.posOrAccountDetail": ("POST", "mediaPush/posOrAccountDetail"),
    "mediaPush.searchMediaPackage": ("POST", "mediaPush/searchMediaPackage"),
    "trackUrl.search": ("POST", "trackUrl/search"),
    "log.search": ("POST", "log/search"),
    "compliance.searchViolationInfo": ("POST", "compliance/searchViolationInfo"),
    "common.cityMap": ("GET", "common/cityMap"),
    "common.findBank": ("GET", "common/findBank"),
    "common.findSubBank": ("POST", "common/findSubBank"),
    "common.invoiceType": ("GET", "common/invoiceType"),
    "common.listIndustry": ("GET", "common/listIndustry"),
    "common.tags": ("POST", "common/tags"),
}


def output(payload: dict[str, Any], code: int) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))
    sys.exit(code)


def fail(error: str, msg: str, code: int, started: float, action: str) -> None:
    output({"ok": False, "code": code, "error": error, "msg": msg,
            "next_action": {"type": action, "retryable": error == "API_TIMEOUT"},
            "duration_ms": int((time.monotonic() - started) * 1000)}, code)


def parse_tasks(raw: str) -> list[dict[str, Any]]:
    tasks = json.loads(raw)
    if not isinstance(tasks, list) or not tasks or len(tasks) > MAX_TASKS:
        raise ValueError(f"tasks 必须是 1 到 {MAX_TASKS} 条的 JSON 数组")
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("endpoint"), str):
            raise ValueError("每个任务必须是含 endpoint 的对象")
        if task["endpoint"] not in ENDPOINTS:
            raise PermissionError(f"不允许的只读接口标识：{task['endpoint']}")
        if "params" in task and not isinstance(task["params"], dict):
            raise ValueError("params 必须是对象")
        if "body" in task and not isinstance(task["body"], dict):
            raise ValueError("body 必须是对象")
    return tasks


def request_one(session: SmartAuthSession, task: dict[str, Any]) -> dict[str, Any]:
    method, path = ENDPOINTS[task["endpoint"]]
    payload_key = "params" if method == "GET" else "json"
    payload = task.get("params", {}) if method == "GET" else task.get("body", {})
    for attempt in range(2):
        try:
            response = session.request(method, BASE_URL + path, timeout=30, **{payload_key: payload})
            if response.status_code in (301, 302, 303, 307, 308):
                return {"label": task.get("label"), "endpoint": task["endpoint"], "ok": False,
                        "error": "AUTH_EXPIRED", "msg": "Auth 登录态已失效，请完成重新认证后重试。",
                        "http_status": response.status_code, "attempts": attempt + 1}
            try:
                body = response.json()
            except ValueError:
                body = {"text": response.text}
            return {"label": task.get("label"), "endpoint": task["endpoint"],
                    "ok": response.ok, "http_status": response.status_code, "response": body,
                    "attempts": attempt + 1}
        except requests.Timeout as exc:
            if attempt == 0:
                continue
            return {"label": task.get("label"), "endpoint": task["endpoint"], "ok": False,
                    "error": "API_TIMEOUT", "msg": str(exc), "attempts": attempt + 1}
        except requests.RequestException as exc:
            return {"label": task.get("label"), "endpoint": task["endpoint"], "ok": False,
                    "error": "NETWORK_ERROR", "msg": str(exc), "attempts": attempt + 1}
    raise RuntimeError("unexpected request loop exit")


def main() -> None:
    started = time.monotonic()
    parser = argparse.ArgumentParser(description="批量查询 SSP 运营平台只读接口")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tasks-json", help="任务 JSON 数组")
    group.add_argument("--item-json", help="单项补跑任务 JSON 对象")
    args = parser.parse_args()
    raw_tasks = args.tasks_json if args.tasks_json else f"[{args.item_json}]"
    try:
        tasks = parse_tasks(raw_tasks)
    except PermissionError as exc:
        fail("ENDPOINT_NOT_ALLOWED", str(exc), 2, started, "ASK_USER")
    except (ValueError, json.JSONDecodeError) as exc:
        fail("INVALID_INPUT", str(exc), 2, started, "ASK_USER")

    try:
        session = SmartAuthSession()
        results = [request_one(session, task) for task in tasks]
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        error = "AUTH_FAILED" if status in (401, 403) else "API_5XX" if status >= 500 else "API_4XX"
        action = "ASK_USER" if error in ("AUTH_FAILED", "API_4XX") else "STOP"
        fail(error, str(exc), 2 if error == "AUTH_FAILED" else 1, started, action)
    except requests.RequestException as exc:
        fail("NETWORK_ERROR", str(exc), 1, started, "STOP")
    except Exception as exc:
        print(f"诊断信息：{exc}", file=sys.stderr)
        fail("NETWORK_ERROR", "请求初始化失败，请检查内网登录和网络连接。", 1, started, "STOP")

    succeeded = sum(1 for result in results if result["ok"])
    sso_expired = any(result.get("error") == "AUTH_EXPIRED" for result in results)
    output({"ok": True, "code": 0, "data": {"total": len(results), "succeeded": succeeded,
            "failed": len(results) - succeeded, "results": results},
            "next_action": {"type": "ASK_USER" if sso_expired else "CONTINUE"},
            "duration_ms": int((time.monotonic() - started) * 1000)}, 0)


if __name__ == "__main__":
    main()
