# 错误处理（kwaibi-sql-access-exec）

执行 `scripts/execute_sql.py` 遇到问题时，先读本文件，按脚本返回的 `code` / `exit code` 与 `next_action` 处理。禁止跳过查表自行发挥（包括：本地 exec 强跑、改脚本、降级到 python3/pip）。

## 协议速览

- 进程退出码：`0` 成功，`1` 失败，`2` 权限/需远程 Agent，`130` SIGINT
- stdout 最后一行是业务 JSON；stderr 仅承载诊断（traceback）
- 退出码与 JSON `code` 不一致时，以非零退出码为准

## 错误码速查

| error | code | 动作 | 对用户说 / 处理 |
|-------|------|------|-----------------|
| `REMOTE_AGENT_REQUIRED` | 2 | RETRY_WITH_CALL_REMOTE_AGENT | 该脚本是长耗时任务，必须用 `call_remote_agent` 执行 |
| `SQL_EMPTY` | 1 | STOP | 没有收到 SQL，检查 `--sql` / `--sql-file` / stdin |
| `WRITE_SQL_DENIED` | 1 | ASK_USER | 默认只执行只读语句；确需写语句，用户确认后加 `--allow-write` |
| `FILE_NOT_FOUND` | 1 | ASK_USER | SQL 文件路径不存在，请确认 |
| `TASK_FAILED` / `TASK_KILLED` | 1 | STOP | SQL 执行失败/被终止，把 msg 里的平台日志尾部展示给用户定位；报错含「输入分区不存在」时提示用户把 `hi` 往前退一档（该表分区落地延迟 ~2 小时） |
| `TASK_TIMEOUT` | 1 | STOP | 轮询超时；调大 `--timeout-sec` 或把 SQL 优化（加分区过滤） |
| `EXEC_FAILED` | 1 | STOP | 接口层失败（提交/下载）；重试一次，仍失败看 stderr traceback |
| `AUTH_FAILED` / `SSO_EXPIRED`（HTTP 401/403 透传） | 1 | ASK_USER | SSO 登录态失效；引导用户在内网页面重新登录后重试 |
| `API_5XX`（HTTP 5xx 透传） | 1 | STOP | 平台服务暂时不可用，稍后重试 |
| `API_TIMEOUT` / `NETWORK_ERROR`（requests 异常透传） | 1 | STOP | 检查网络/内网连通性，重试一次 |
| `UV_NOT_FOUND` | 1 | STOP | 安装 uv：`curl -LsSf https://astral.sh/uv/install.sh | sh` |

## 平台状态码（轮询）

| status | 含义 | 处理 |
|--------|------|------|
| 1 / 18 | RUNNING / SUBMITTED | 继续轮询 |
| 2 | SUCCESS | 进入 CSV 下载 |
| 3 / 8 | KILLED / TERMINATE | 按 TASK_* 失败处理，msg 含平台日志尾部 |
| 5 | FAILED | 同上；常见原因：语法错误、权限不足、分区过滤缺失超时 |

## 注意

- notebook 缓存文件 `tmp/kwaibi_sql_access_notebook.json` 缺失或损坏时脚本会自动重建，无需手工处理
- 结果 0 行时脚本跳过下载、直接回报（不是错误）
- 同一 notebook 可重复执行不同 SQL，勿在页面删除 `kwaibi-sql-access-exec` 文件（平台删除接口对 API 自建 notebook 有缺陷，删了也会重建）
