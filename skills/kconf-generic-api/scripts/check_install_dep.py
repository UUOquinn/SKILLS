#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["ks-aimate>=1.0.30"]
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
from auth_helper.sso_login_client import SmartAuthSession, SSOAuthRequiredError

BASE = "https://app.example.corp"
START_MS = int(time.time() * 1000)


def emit(payload, code):
    payload.setdefault("duration_ms", int(time.time() * 1000) - START_MS)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "ensure"])
    parser.add_argument("skills", nargs="+")
    args = parser.parse_args()
    session = SmartAuthSession()
    states = {}
    try:
        for slug in args.skills:
            resp = session.request("GET", f"{BASE}/api/v1/fe/skills/installed/{slug}", timeout=20)
            resp.raise_for_status()
            states[slug] = bool(resp.json().get("data", {}).get("installed"))

        if args.action == "check":
            missing = [s for s, ok in states.items() if not ok]
            if missing:
                emit({"ok": False, "code": 2, "error": "DEPENDENCY_MISSING", "msg": "缺少依赖：" + ", ".join(missing), "data": states, "next_action": {"type": "ASK_USER"}}, 2)
            emit({"ok": True, "code": 0, "data": states, "next_action": {"type": "CONTINUE"}}, 0)

        missing = [s for s, ok in states.items() if not ok]
        for slug in missing:
            resp = session.request("POST", f"{BASE}/api/v1/fe/skills/install/{slug}", params={"autoDispatchUpdate": "true"}, timeout=30)
            resp.raise_for_status()
            body = resp.json()
            if body.get("result") not in (1, 0, None) and not body.get("success", True):
                emit({"ok": False, "code": 1, "error": "DEPENDENCY_MISSING", "msg": f"安装依赖 {slug} 失败: {body}", "next_action": {"type": "STOP"}}, 1)
            states[slug] = True
        emit({"ok": True, "code": 0, "data": states, "next_action": {"type": "CONTINUE"}}, 0)
    except SSOAuthRequiredError:
        emit({"ok": False, "code": 2, "error": "AUTH_EXPIRED", "msg": "Auth 登录态已失效或等待扫码，请完成内网登录后重试", "next_action": {"type": "ASK_USER"}}, 2)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit({"ok": False, "code": 1, "error": "NETWORK_ERROR", "msg": str(exc), "next_action": {"type": "STOP"}}, 1)
