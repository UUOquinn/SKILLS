---
name: kwaibi-sql-access-exec
description: >
  通过 SSO 直连 KwaiBI SQL 取数接口，代为执行 Hive/ClickHouse 等 SQL 并把结果下载为 CSV 到工作区。
  自动创建并复用执行 notebook，提交后轮询任务状态，成功后拉取 CSV。
  当用户说执行 SQL、代跑、跑一下这条 SQL、sql-access 执行、取数并下载 CSV 时使用。
  不适用：SQL 的编写与优化（交给 kwaibi-sql-unit-hh 或用户）、看板 dataset 结构化查数
  （走 operation-kwaibi-1-1）、需要审批的写操作或敏感数据下载。
---

<!-- skill-creator: askuser-with-text-fallback-v1 -->

## 用户交互：AskUser 优先与文本降级

凡需向用户收集信息、请求确认或要求选择时，优先调用 `askUser` 工具，并以结构化问题和可选项取得答复；关联问题应合并提问。

若 `askUser` 在当前运行环境不存在、调用失败或返回无效结果，才允许降级为普通聊天文本提问。降级时必须明确说明“当前 AskUser 不可用，以下为兼容提问”，并在收到用户明确答复前停止后续依赖该答复的操作；不得猜测、默认代填或把沉默视为同意。

## 定位

- 平台：KwaiBI SQL 取数（bi-platform.example.corp），SQL 编辑页 shareId=105af2bd-4ed2-42b3-a0f9-8b1c8799bfb6
- 鉴权：脚本内嵌 `SmartSSOSession`（ks-aimate），自动管理 SSO 登录态，不需要用户提供 Cookie 或 token
- 默认只执行只读语句（SELECT / WITH / SHOW / DESC / EXPLAIN）；写语句需 `--allow-write`

## 执行路由（必读）

`scripts/execute_sql.py` 含异步轮询，是**长耗时任务**，必须用 `call_remote_agent` 执行：

- 命令：`call_remote_agent`，`method=CALLBACK`（默认）
- 脚本自带远程 Agent 保护：本地 exec 直接跑会以 `REMOTE_AGENT_REQUIRED`（code=2）退出并引导重试
- 不要在普通 exec 里跑它；SQL 短到秒级返回也不行（脚本按长耗时设计）

## 用法

```bash
# 在 call_remote_agent 中执行（沙箱内 CWD 即 workspace）
uv run <skill_directory>/scripts/execute_sql.py \
  --sql "SELECT account_id, put_status FROM ks_origin_ad_center_sync_db.s_ad_core_unit_hh WHERE dt='2026-10-28' AND hi='1400' AND account_id IN (1,2,3)" \
  --timeout-sec 1800 --poll-sec 5

# 或从文件读 SQL
uv run <skill_directory>/scripts/execute_sql.py --sql-file tmp/query.sql
```

参数速查：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--sql` / `--sql-file` | stdin | SQL 来源 |
| `--name` | kwaibi-sql-access-exec | 自建 notebook 名（首次创建、之后复用） |
| `--out` | tmp/kwaibi_result.csv | CSV 结果路径 |
| `--timeout-sec` | 1800 | 轮询超时 |
| `--poll-sec` | 5 | 轮询间隔 |
| `--db-engine` | 1 | 1=Hive，3=ClickHouse，4=Mysql，8=Bleem |
| `--hive-engine` | 1 | 1=智能路由，2=spark，3=mr |
| `--data-source-id` / `--user-group` | 1003 / 855 | 数据源与用户组，可按需覆盖 |
| `--no-download` | 关 | 只回报行数不下载 |
| `--item` | 无 | 单项补跑入口（轻量批量）：与 `--sql` 等价的单次执行 |
| `--allow-write` | 关 | 允许写语句 |

输出契约：stdout 进度保活日志在前，最后一行是业务 JSON（`ok`、`code`、`data.task_id`、`data.rows`、`data.csv_path`、`next_action`）；stderr 只承载诊断。执行后先看最后一行 JSON 的 `code`，非 0 按 `references/error-handling.md` 处理。

## Agent 工作流

1. 确认 SQL 来源（用户给 / kwaibi-sql-unit-hh 生成），确认只读意图
2. 用 AskUser 确认后再执行写语句（用户要写语句时）
3. 通过 `call_remote_agent` 运行上面的命令；短查询一般 1～3 分钟，大查询可达 30 分钟
4. 读 stdout 最后一行 JSON：`code=0` → 报告 `rows`、`csv_path`（用文件标签交付）；`code≠0` → 查错误表
5. 交付 CSV 时给出行数、task_id、query_id 摘要

## 接口链（已实测）

1. `POST /sql-access/api/v1/notebook/add` 创建执行 notebook（uuid 缓存于 `tmp/kwaibi_sql_access_notebook.json`，后续复用）
2. `POST /sql-access/api/v1/notebook/execute` 提交 SQL → 返回 taskId
3. `GET /sql-access/api/v1/notebook/snippet/record` 轮询状态：1/18 运行中 → 2 成功 / 3 终止 / 5 失败
4. `POST /sql-access/api/v1/task/exportCheck` + `GET /sql-access/api/v2/task/exportCSVDirectly` 下载 CSV

## 边界

- 批量能力评估结论：轻量顺序模式（轻量批量）；批量体现在单条 SQL 内的多 ID，单次一个任务，不做多任务编排；单项补跑入口 `--item "<单条 SQL>"`
- 平台删除 notebook 接口对 API 自建 notebook 有缺陷：脚本复用同一个 notebook，避免垃圾文件；如页面出现 `kwaibi-sql-access-exec` 文件请保留
- 不生成 SQL、不改写用户 SQL；敏感数据下载需走平台审批的由平台拦截
- 与市场技能 ks-kwaibi-sql-exec / devops-query-sql 能力重叠：本 skill 为接口直连方案（SSO），响应更快；浏览器方案可作为兜底
