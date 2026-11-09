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
# url = "https://internal.example.corp/kuaishou/prod/+simple/"
# publish = false
# ///
import argparse
import json
import sys
from ks_aimate.sso_login_client import SmartSSOSession, SSOAuthRequiredError


def emit(payload, code):
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(code)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "ensure"])
    parser.add_argument("skills", nargs="+")
    args = parser.parse_args()
    session = SmartSSOSession()
    states = {}
    try:
        for slug in args.skills:
            response = session.request("GET", f"https://internal.example.corp/api/v1/fe/skills/installed/{slug}", timeout=20)
            response.raise_for_status()
            states[slug] = bool(response.json().get("data", {}).get("installed"))
    except SSOAuthRequiredError:
        emit({"ok": False, "code": 2, "error": "SSO_EXPIRED", "msg": "SSO 登录态已失效或等待扫码，请完成内网登录后重试", "next_action": {"type": "ASK_USER"}}, 2)
    missing = [slug for slug, installed in states.items() if not installed]
    if missing:
        emit({"ok": False, "code": 2, "error": "DEPENDENCY_MISSING", "msg": "缺少依赖：" + ", ".join(missing), "data": states, "next_action": {"type": "ASK_USER"}}, 2)
    emit({"ok": True, "code": 0, "data": states, "next_action": {"type": "CONTINUE"}}, 0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit({"ok": False, "code": 1, "error": "NETWORK_ERROR", "msg": str(exc), "next_action": {"type": "STOP"}}, 1)
