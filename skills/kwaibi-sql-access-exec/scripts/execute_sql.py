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
"""通过 Auth 直连 BI SQL 取数接口执行 Hive SQL 并下载 CSV 结果。

接口链（均已实测）：notebook/add → notebook/execute → snippet/record 轮询
→ task/exportCheck → task/exportCSVDirectly（CSV）。

长耗时任务：本脚本含轮询循环，必须用 call_remote_agent 执行；
未检测到 MYFLICKER_REMOTE_AGENT_METHOD 时立即退出并引导重试。

stdout 契约：进度保活日志在前，最后一行是业务 JSON；stderr 只承载诊断。
退出码与 JSON 的 code 一致（0 成功 / 1 失败 / 2 需远程 Agent）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
import uuid
from pathlib import Path

from auth_helper.sso_login_client import SmartAuthSession, get_username

BASE = "https://bi.example.corp"
NB_CACHE = Path("tmp") / "kwaibi_sql_access_notebook.json"
START_MS = int(time.time() * 1000)

# 任务状态（与前端枚举一致）
STATUS = {
    0: "STOPED", 1: "RUNNING", 2: "SUCCESS", 3: "KILLED",
    5: "FAILED", 7: "EXPORTING", 8: "TERMINATE", 13: "WAITING_UPSTREAM",
    18: "SUBMITTED",
}
TERMINAL_OK = {2}
TERMINAL_BAD = {0, 3, 5, 8}
POLLING = {1, 7, 13, 18}


def emit(data: dict | None = None, code: int = 0, error: str | None = None,
         msg: str | None = None, next_type: str = "CONTINUE",
         retryable: bool = False) -> None:
    out = {
        "ok": code == 0,
        "code": code,
        "duration_ms": int(time.time() * 1000) - START_MS,
        "next_action": {"type": next_type, "retryable": retryable},
    }
    if data is not None:
        out["data"] = data
    if error:
        out["error"] = error
    if msg:
        out["msg"] = msg
    print(json.dumps(out, ensure_ascii=False))


class BISqlExec:
    def __init__(self, session: SmartAuthSession):
        self.s = session

    def _post(self, path: str, body: dict) -> dict:
        resp = self.s.request("POST", BASE + path, json=body)
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, params: dict) -> dict:
        resp = self.s.request("GET", BASE + path, params=params)
        resp.raise_for_status()
        return resp.json()

    def _get_text(self, path: str, params: dict) -> str:
        resp = self.s.request("GET", BASE + path, params=params)
        resp.raise_for_status()
        return resp.text

    def get_or_create_notebook(self, name: str) -> dict:
        if NB_CACHE.exists():
            cached = json.loads(NB_CACHE.read_text(encoding="utf-8"))
            if cached.get("uuid") and cached.get("id"):
                print(f"[bi-exec] 复用 notebook {cached['name']} "
                      f"(id={cached['id']})", flush=True)
                return cached
        nb_uuid = str(uuid.uuid4())
        r = self._post("/sql-access/api/v1/notebook/add",
                       {"uuid": nb_uuid, "name": name})
        if r.get("result") != 1 or not r.get("data"):
            raise RuntimeError(f"创建 notebook 失败: {r}")
        data = r["data"]
        nb = {"uuid": data["uuid"], "id": data["id"], "name": name}
        NB_CACHE.parent.mkdir(parents=True, exist_ok=True)
        NB_CACHE.write_text(json.dumps(nb, ensure_ascii=False),
                            encoding="utf-8")
        print(f"[bi-exec] 新建 notebook {name} (id={nb['id']})", flush=True)
        return nb

    def execute(self, nb: dict, sql: str, engine: dict) -> tuple[int, str]:
        """提交执行，返回 (task_id, snippet_uuid)。"""
        snippet_uuid = str(uuid.uuid4())
        snippet = {
            "isFromTemplate": False, "templateId": None, "name": "q",
            "remark": "", "query": sql, "querySelection": "", "depList": [],
            "dbEngine": engine["db_engine"],
            "dataSourceId": engine["data_source_id"],
            "hiveEngine": engine["hive_engine"],
            "userGroup": engine["user_group"],
            "clusterName": None, "shutdown": False,
            "setting": {"editorHeight": 300, "resultAreaHeight": 300},
            "sort": -1, "uuid": snippet_uuid, "taskId": None,
            "exePlatform": None, "platform": None, "exeType": None,
            "type": 1, "cutType": None, "cutRows": None, "needSplit": False,
            "assetTableMap": {}, "varMap": {},
        }
        body = {
            "id": nb["id"], "uuid": nb["uuid"], "name": nb["name"],
            "type": 1, "favoriteId": None,
            "data": {"autoIncrement": 0, "varMap": {}, "assetTableMap": {},
                     "snippetList": [snippet]},
        }
        r = self._post("/sql-access/api/v1/notebook/execute", body)
        if r.get("result") != 1:
            raise RuntimeError(f"提交执行失败: {json.dumps(r, ensure_ascii=False)[:500]}")
        snippet_list = (r.get("data") or {}).get("snippetList") or []
        if not snippet_list or not snippet_list[0].get("taskId"):
            raise RuntimeError(f"执行响应缺少 taskId: {json.dumps(r, ensure_ascii=False)[:500]}")
        return snippet_list[0]["taskId"], snippet_uuid

    def poll(self, nb_uuid: str, snippet_uuid: str,
             timeout_sec: int, poll_sec: int) -> dict:
        """轮询 snippet/record 直到终态，返回 taskEntity。"""
        deadline = time.time() + timeout_sec
        while True:
            r = self._get("/sql-access/api/v1/notebook/snippet/record",
                          {"notebookUuid": nb_uuid,
                           "snippetUuid": snippet_uuid})
            content = (r.get("data") or {}).get("content") or []
            task = (content[0].get("taskEntity") if content else None) or {}
            status = task.get("status")
            name = STATUS.get(status, f"UNKNOWN({status})")
            elapsed = int(time.time() * 1000) - START_MS
            print(f"[bi-exec] 状态 {name}（已用 {elapsed // 1000}s）",
                  flush=True)
            if status in TERMINAL_OK:
                return task
            if status in TERMINAL_BAD:
                return self._enrich_task(task)
            if time.time() > deadline:
                raise TimeoutError(f"轮询超时（>{timeout_sec}s），最后状态 {name}")
            time.sleep(poll_sec)

    def _enrich_task(self, task: dict) -> dict:
        """终态失败时用 task/show 补全 log/errorInfo（snippet/record 可能为空）。"""
        tid = task.get("id") or task.get("taskId")
        if not tid:
            return task
        try:
            r = self._get("/sql-access/api/v1/task/show", {"taskId": tid})
            full = (r.get("data") or {})
            task["log"] = full.get("log") or task.get("log")
            task["errorInfo"] = full.get("errorInfo") or task.get("errorInfo")
            task["resultRowCount"] = full.get("resultRowCount") or task.get("resultRowCount")
        except Exception:  # 补查失败不影响主流程
            pass
        return task

    def download_csv(self, task_id: int, username: str) -> tuple[str, int]:
        """exportCheck + exportCSVDirectly，返回 (csv_text, 行数估算)。"""
        params = {"taskId": task_id, "fileType": "csv",
                  "downloadType": 0, "receiveUsers": username}
        r = self._post("/sql-access/api/v1/task/exportCheck", params)
        if r.get("result") != 1 or not r.get("data"):
            raise RuntimeError(f"exportCheck 未通过: {json.dumps(r, ensure_ascii=False)[:400]}")
        text = self._get_text("/sql-access/api/v2/task/exportCSVDirectly", params)
        text = text.lstrip("\ufeff")
        rows = len(text.rstrip("\n").splitlines())
        return text, rows


def main() -> int:
    if not os.environ.get("MYFLICKER_REMOTE_AGENT_METHOD"):
        emit(code=2, error="REMOTE_AGENT_REQUIRED",
             msg="该脚本是长耗时任务，请使用 call_remote_agent 执行（不要用本地 exec）。",
             next_type="RETRY_WITH_CALL_REMOTE_AGENT", retryable=True)
        return 2

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sql", help="要执行的 SQL（SELECT 为主）")
    ap.add_argument("--sql-file", type=Path, help="SQL 文件路径")
    ap.add_argument("--name", default="bi-sql-access-exec",
                    help="自建 notebook 名称（首次创建，之后复用）")
    ap.add_argument("--out", type=Path, default=Path("tmp") / "kwaibi_result.csv",
                    help="CSV 结果输出路径")
    ap.add_argument("--timeout-sec", type=int, default=1800,
                    help="轮询超时秒数")
    ap.add_argument("--poll-sec", type=int, default=5, help="轮询间隔秒数")
    ap.add_argument("--db-engine", type=int, default=1, help="1=Hive")
    ap.add_argument("--hive-engine", type=int, default=1, help="1=智能路由")
    ap.add_argument("--data-source-id", type=int, default=1003)
    ap.add_argument("--user-group", type=int, default=855)
    ap.add_argument("--no-download", action="store_true",
                    help="只执行并回报行数，不下载 CSV")
    ap.add_argument("--allow-write", action="store_true",
                    help="允许执行非 SELECT 语句（默认拒绝）")
    ap.add_argument("--item", help="单项补跑入口（轻量批量）：与 --sql 等价的单次执行")
    args = ap.parse_args()

    try:
        if args.sql_file:
            if not args.sql_file.exists():
                emit(code=1, error="FILE_NOT_FOUND",
                     msg=f"SQL 文件不存在：{args.sql_file}",
                     next_type="ASK_USER")
                return 1
            sql = args.sql_file.read_text(encoding="utf-8")
        elif args.sql:
            sql = args.sql
        elif args.item:
            sql = args.item
        else:
            sql = sys.stdin.read()
        sql = sql.strip().rstrip(";").strip()
        if not sql:
            emit(code=1, error="SQL_EMPTY",
                 msg="没有收到 SQL。请通过 --sql / --sql-file / stdin 提供。",
                 next_type="STOP")
            return 1
        if not args.allow_write and not re.match(r"(?is)^\s*(select|with|show|desc|explain)\b", sql):
            emit(code=1, error="WRITE_SQL_DENIED",
                 msg="默认只执行只读语句（SELECT/WITH/SHOW/DESC/EXPLAIN）。"
                     "如确需执行写语句，加 --allow-write。",
                 next_type="ASK_USER")
            return 1

        username = get_username()
        print(f"[bi-exec] 当前用户 {username}", flush=True)
        client = BISqlExec(SmartAuthSession())
        nb = client.get_or_create_notebook(args.name)
        engine = {"db_engine": args.db_engine, "hive_engine": args.hive_engine,
                  "data_source_id": args.data_source_id,
                  "user_group": args.user_group}
        task_id, snippet_uuid = client.execute(nb, sql, engine)
        print(f"[bi-exec] 已提交 taskId={task_id}", flush=True)
        task = client.poll(nb["uuid"], snippet_uuid,
                           args.timeout_sec, args.poll_sec)

        status = task.get("status")
        if status in TERMINAL_BAD:
            log_tail = (task.get("errorInfo") or task.get("log") or "")[-1500:]
            emit(code=1, error="TASK_" + STATUS.get(status, "FAILED"),
                 msg=f"执行未成功（{STATUS.get(status)}）：{log_tail}",
                 next_type="STOP")
            return 1

        rows = task.get("resultRowCount") or 0
        csv_path = None
        if rows and not args.no_download:
            csv_text, csv_rows = client.download_csv(task_id, username)
            out: Path = args.out
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(csv_text, encoding="utf-8")
            csv_path = str(out)
            print(f"[bi-exec] CSV 已写入 {out}（{csv_rows} 行）", flush=True)
        elif not rows:
            print("[bi-exec] 结果为空（0 行），跳过下载", flush=True)

        emit({
            "task_id": task_id,
            "query_id": task.get("queryId"),
            "rows": rows,
            "csv_path": csv_path,
            "use_time_s": task.get("useTime"),
        }, msg="执行完成")
        return 0
    except TimeoutError as e:
        print(traceback.format_exc(), file=sys.stderr)
        emit(code=1, error="TASK_TIMEOUT", msg=str(e), next_type="STOP")
        return 1
    except Exception as e:
        print(traceback.format_exc(), file=sys.stderr)
        emit(code=1, error="EXEC_FAILED",
             msg=f"执行失败：{e}", next_type="STOP")
        return 1


if __name__ == "__main__":
    sys.exit(main())
