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
"""番茄短剧账户筛选 — 公共模块。"""

from __future__ import annotations

import json
import pathlib
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from ks_aimate.sso_login_client import SmartSSOSession, SSOAuthRequiredError

TZ = timezone(timedelta(hours=8))


class ScreenError(Exception):
    def __init__(self, error: str, msg: str, code: int = 1) -> None:
        super().__init__(msg)
        self.error = error
        self.code = code


def file_link(path: Any) -> str:
    """绝对路径 → <FILE>工作区相对路径</FILE> 下载链接，交付时照抄。"""
    s = str(path)
    marker = "/workspace/"
    if marker in s:
        rel = s.split(marker, 1)[1]
    elif s.startswith("tmp/"):
        rel = s
    else:
        rel = Path(s).name
    return f"<FILE>{rel}</FILE>"


def as_raw(cell: Any) -> Any:
    if isinstance(cell, dict):
        if "r" in cell:
            return cell["r"]
        if "v" in cell:
            return cell["v"]
    return cell


def fetch(body: dict[str, Any]) -> dict[str, Any]:
    url = "https://internal.example.corp/api/v1/data/query"
    ds_id = body.get("dataSetId", "?")
    try:
        resp = SmartSSOSession().request("POST", url,
                                          json=body,
                                          headers={"Content-Type": "application/json",
                                                   "Referer": "https://bi-platform.example.corp/"},
                                          timeout=120)
    except SSOAuthRequiredError as exc:
        raise ScreenError("SSO_EXPIRED", "OLAP 登录态失效，请先恢复该域认证", 2) from exc
    except requests.Timeout as exc:
        raise ScreenError("API_TIMEOUT", f"数据集 {ds_id} 查询超时") from exc
    if resp.status_code in (401, 403):
        raise ScreenError("SSO_EXPIRED", "OLAP 登录态失效或无权限", 2)
    if resp.status_code != 200:
        raise ScreenError("API_FAILED", f"数据集 {ds_id} HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if data.get("result") != 1:
        raise ScreenError("QUERY_FAILED",
                          f"数据集 {ds_id} 查询失败：{data.get('error_msg', str(data))[:200]}")
    return data["data"]


def make_body(day: datetime, label: str | None) -> dict[str, Any]:
    """构造第一步查询请求体（基于 olap_query_template.json 完整字段）。"""
    tpl_path = Path(__file__).resolve().parent.parent / "references" / "tomato_step1_template.json"
    body = json.loads(tpl_path.read_text(encoding="utf-8"))
    begin = int(day.timestamp() * 1000)
    end = int((day + timedelta(days=1)).timestamp() * 1000) - 1
    dts = [f for f in body["filters"] if f.get("filterType") == "datetime"]
    if dts:
        dts[0]["filterValue"].update(startTime=begin, endTime=end)
        if isinstance(dts[0].get("originalFilterValue"), dict):
            dts[0]["originalFilterValue"]["value"] = [begin, end]
    body["useCache"] = False
    body["openQueryId"] = str(uuid.uuid4())
    body["queryTime"] = int(datetime.now(TZ).timestamp() * 1000)
    if label:
        for f in body["filters"]:
            if f.get("name") == "可投范围":
                f["filterValue"]["value"] = [label]
                f["originalFilterValue"]["value"] = [label]
    return body


def normalize_step1(data: dict[str, Any], label: str | None) -> tuple[list[tuple[str, str, str]], dict[str, Any]]:
    columns = [c.get("name") for c in (data.get("columns") or [])]
    expected = ["extend_column_0375ed68-bf39-4671-b511-9e13f883d289", "账户ID", "总消耗"]
    idx = {}
    try:
        idx = {name: columns.index(name) for name in expected}
    except ValueError as exc:
        raise ScreenError("COLUMNS_CHANGED", f"第一步列变化：缺少 {exc}")
    rows = data.get("dataList")
    if not isinstance(rows, list) or not isinstance(data.get("rowCount"), int):
        raise ScreenError("QUERY_FAILED", "第一步无明细或总行数")
    if data.get("truncated") or len(rows) >= 100000 or len(rows) != data["rowCount"]:
        raise ScreenError("RESULT_TRUNCATED", f"第一步 {len(rows)}/{data['rowCount']}")
    pairs: list[tuple[str, str, str]] = []
    for row in rows:
        tag = str(as_raw(row[idx["extend_column_0375ed68-bf39-4671-b511-9e13f883d289"]]))
        account = str(as_raw(row[idx["账户ID"]]))
        cost = as_raw(row[idx["总消耗"]])
        if label and tag != label:
            raise ScreenError("INVALID_ROW", f"第一步返回非目标可投范围：{tag}")
        if not re.fullmatch(r"[0-9]+", account):
            raise ScreenError("INVALID_ROW", f"第一步返回非法账户ID：{account}")
        if not isinstance(cost, (int, float)) or float(cost) <= 0.1:
            raise ScreenError("INVALID_ROW", f"第一步返回消耗{float(cost):.2f}≤0.1或无效")
        pairs.append((tag, account, str(cost)))
    summary = {"row_count": len(rows),
               "range_distribution": {tag: sum(1 for t, _, _ in pairs if t == tag)
                                      for tag in set(t for t, _, _ in pairs)}}
    return pairs, summary


def response_json(ok: bool, code: int, started: float, **parts: Any) -> int:
    timeout = parts.get("error") == "API_TIMEOUT"
    action = "CONTINUE" if ok else "RETRY_ONCE_THEN_STOP" if timeout else "STOP"
    print(json.dumps({"ok": ok, "code": code,
                      "duration_ms": int((time.monotonic() - started) * 1000),
                      **parts,
                      "next_action": {"type": action, "retryable": timeout,
                                       "max_retries": 1 if timeout else 0}},
                     ensure_ascii=False))
    return code


# ——— KConf 暗投白名单 ———

KCONF_BASE = "https://kconf.example.corp"
KCONF_KEY = "ad.adtarget3.universeAddChainGrayConfig"
AB_KEY = "enable_chain_antou_30_990_0"


def parse_commented_json(raw: str) -> dict[str, Any]:
    lines = []
    for line in raw.splitlines():
        in_str = escaped = False
        for i, ch in enumerate(line):
            if escaped:
                escaped = False
                continue
            if ch == "\\" and in_str:
                escaped = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if not in_str and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                line = line[:i]
                break
        lines.append(line)
    try:
        return json.loads("\n".join(lines))
    except json.JSONDecodeError as exc:
        raise ScreenError("KCONF_PARSE_ERROR", f"KConf 配置 JSON 解析失败：{exc}") from exc


def kconf_antou_ids() -> tuple[set[str], dict[str, Any]]:
    try:
        resp = SmartSSOSession().request(
            "GET", f"{KCONF_BASE}/api/w/config/get", params={"key": KCONF_KEY}, timeout=30)
    except SSOAuthRequiredError as exc:
        raise ScreenError("KCONF_SSO_EXPIRED", "KConf 登录态失效", 2) from exc
    except requests.Timeout as exc:
        raise ScreenError("API_TIMEOUT", "KConf 请求超时") from exc
    except requests.RequestException as exc:
        raise ScreenError("NETWORK_ERROR", f"无法连接 KConf：{exc}") from exc
    if resp.status_code in (401, 403):
        raise ScreenError("KCONF_SSO_EXPIRED", "KConf 登录态失效或无权限", 2)
    payload = resp.json()
    if payload.get("result") == 101:
        raise ScreenError("KCONF_SSO_EXPIRED", "KConf 登录态失效", 2)
    if payload.get("result") != 1:
        raise ScreenError("KCONF_QUERY_FAILED", f"KConf 读取失败：{str(payload.get('message'))[:200]}")
    data = payload.get("data") or {}
    sub = next((item for item in data.get("subConfigs") or [] if item.get("stage") == "production"), None)
    if sub is None:
        raise ScreenError("KCONF_QUERY_FAILED", "KConf 无 production 子配置")
    raw = (sub.get("curConfig") or {}).get("data", "")
    if not isinstance(raw, str) or not raw.strip():
        raise ScreenError("KCONF_QUERY_FAILED", "KConf 生产配置内容为空")
    content = parse_commented_json(raw)
    configs = content.get("configs")
    if not isinstance(configs, list):
        raise ScreenError("KCONF_PARSE_ERROR", "KConf 配置缺少 configs 数组")
    hit = next((item for item in configs if item.get("ab_key") == AB_KEY), None)
    if hit is None:
        raise ScreenError("KCONF_AB_KEY_MISSING", f"配置中未找到 ab_key={AB_KEY}")
    ids = {str(value) for value in (hit.get("account_white_list") or [])}
    meta = data.get("meta") or {}
    return ids, {"kconf_version": meta.get("version"),
                 "kconf_snapshot_id": (sub.get("curConfig") or {}).get("snapshotId"),
                 "kconf_step_cur": sub.get("stepCur")}