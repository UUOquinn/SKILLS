#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["requests>=2.31.0,<3", "ks-aimate>=1.0.30"]
# [tool.uv.sources]
# "ks-aimate" = { index = "kuaishou" }
# [[tool.uv.index]]
# name = "kuaishou"
# url = "https://internal.example.corp/kuaishou/prod/+simple/"
# publish = false
# ///
"""Industry code → name dictionary (built-in static + refresh from seed platform).

Usage:
  uv run scripts/industry_lookup.py lookup 1028,1029
  uv run scripts/industry_lookup.py refresh

Data sources:
  - Built-in: references/industry-code-name.json (353 industries, extracted from the
    orient-union-shield seed dictionary, 44 first-level / 364 second-level taxonomy)
  - Refresh: http://10.0.0.1:3000/data/industry-map.json (may be unreachable
    from some networks; built-in dictionary is the fallback)

Output contract: stdout last line is a JSON object with ok/code/error/msg/next_action.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from ks_aimate.sso_login_client import SmartSSOSession

REFS = Path(__file__).resolve().parent.parent / "references"
DICT_PATH = REFS / "industry-code-name.json"
INDUSTRY_MAP_URL = "http://10.0.0.1:3000/data/industry-map.json"
TIMEOUT = 10
_STARTED = time.monotonic()


def load_builtin() -> dict:
    if not DICT_PATH.exists():
        return {}
    try:
        with open(DICT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("codes", {})
    except Exception:
        return {}


def emit_success(data: dict) -> None:
    print(json.dumps({"ok": True, "code": 0, "data": data,
                      "duration_ms": int((time.monotonic() - _STARTED) * 1000),
                      "next_action": {"type": "CONTINUE"}}, ensure_ascii=False))


def emit_error(code: int, error: str, msg: str, action: str = "STOP",
               retryable: bool = False) -> None:
    print(json.dumps({"ok": False, "code": code, "error": error, "msg": msg,
                      "duration_ms": int((time.monotonic() - _STARTED) * 1000),
                      "next_action": {"type": action, "retryable": retryable}},
                     ensure_ascii=False))
    sys.exit(code)


def main() -> None:
    parser = argparse.ArgumentParser(description="Industry code dictionary lookup / refresh")
    sub = parser.add_subparsers(dest="command", required=True)

    p_lookup = sub.add_parser("lookup", help="Translate industry codes to names")
    p_lookup.add_argument("--codes", required=True,
                          help="Comma-separated industry codes, e.g. 1028,1029,1030")
    p_lookup.add_argument("--codes-file", help="JSON file with array of codes (alternative)")

    sub.add_parser("refresh", help="Refresh dictionary from seed platform")

    args = parser.parse_args()

    try:
        if args.command == "lookup":
            if args.codes_file:
                codes = json.loads(Path(args.codes_file).read_text(encoding="utf-8"))
            else:
                codes = [c.strip() for c in args.codes.split(",") if c.strip()]
            mapping = load_builtin()
            found = {c: mapping.get(c) for c in codes}
            missing = [c for c in codes if c not in mapping]
            emit_success({"found": found, "missing": missing,
                          "total": len(codes), "resolved": len(codes) - len(missing)})

        elif args.command == "refresh":
            session = SmartSSOSession()
            resp = session.request("GET", INDUSTRY_MAP_URL, timeout=TIMEOUT)
            resp.raise_for_status()
            raw = resp.json()
            # industry-map.json shape: {"first": {...}, "second": {...}} (code -> name)
            second = raw.get("second", {})
            if not second:
                emit_error(1, "DICT_PARSE_FAILED",
                           "industry-map.json 缺少 second 字段，无法解析二级行业字典",
                           "STOP")
            merged = dict(load_builtin())
            merged.update({str(k): v for k, v in second.items()})
            out = {"version": 1, "source": "industry-map.json (seed platform)", "codes": merged}
            DICT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
            emit_success({"path": str(DICT_PATH), "codes": len(merged),
                          "added": len(second)})

    except Exception as e:
        msg = str(e)
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            emit_error(1, "API_TIMEOUT", "行业字典源连接超时（10.0.0.1:3000 可能不可达），"
                                          "仍可用内置静态字典", "RETRY_ONCE_THEN_STOP", True)
        elif "SSO" in msg.upper() or "login" in msg.lower():
            emit_error(1, "SSO_EXPIRED", "SSO 登录已过期，请重新认证", "ASK_USER", True)
        else:
            emit_error(1, "NETWORK_ERROR", f"行业字典源不可达：{msg}，仍可用内置静态字典",
                       "RETRY_ONCE_THEN_STOP", True)


if __name__ == "__main__":
    main()
