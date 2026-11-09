# Error Handling — kconf-generic-api

执行任何脚本遇到报错，必须先读取本文件，并优先按脚本返回的 `code` / `exit code` 和 `next_action` 执行。禁止跳过查表步骤自行发挥（改写脚本、偷其他 Skill 代码、降级到原生 python3/pip）。

脚本同时提供：进程退出码（`0=成功/引导`、`1=失败`、`2=权限拒绝/询问`、`130=SIGINT`）与 stdout 最后一行的业务 JSON（`ok`/`code`/`error`/`msg`/`next_action`）。两者不一致时以非零退出码为准。

## 错误码速查表

| error_code | 动作 | 说明 |
|-----------|------|------|
| `UV_NOT_FOUND` | 停止，引导用户安装 uv | 系统未安装 `uv` |
| `DEPENDENCY_MISSING` | 停止，引导用户安装 `batch-task-orchestrator` 依赖 | `ks-aimate` 或依赖 Skill 缺失 |
| `SSO_EXPIRED` | 引导用户重新登录 | Token 过期 |
| `AUTH_FAILED` | 询问用户 / 提示联系管理员 | 权限不足（KConf 权限或直改被拒） |
| `API_TIMEOUT` | 重试 1 次，仍失败则中止 | 网络超时 |
| `API_4XX` | 展示 KConf 返回的 `message` 给用户 | 参数非法、key 不存在、precheck 未过 |
| `API_5XX` | 中止，告知用户稍后重试 | KConf 服务端故障 |
| `NETWORK_ERROR` | 提示用户检查 VPN | 网络不可达 |
| `RATE_LIMITED` | 退避后重试 1 次，仍失败则中止 | KConf `result=104` 请求过于频繁（搜索接口易触发） |
| `FILE_NOT_FOUND` | 询问用户路径 | `--content-file`/`--keys-file`/`--params-file` 不存在 |
| `PERMISSION_DENIED` | 提示用户检查权限 | 文件/目录无权限 |
| `KCONF_RESULT_NOT_1` | 展示 `message`，中止 | KConf 响应包 `result != 1` |
| `VERIFY_MISMATCH` | 停止，明确告知回查未通过 | 写操作后回查发现配置未按预期生效 |
| `BATCH_TIMEOUT` | 停止，报告已完成/未完成 key 列表 | 整批运行超过 10 分钟，直接判定失败，不做续跑 |
| `BATCH_SIZE_EXCEEDED` | 停止，提示拆批 | 单批 key 数超过 50 |
| `ACTION_ALREADY_DONE` | 跳过该项，标记 skipped | `--check-action` 命中已执行的 action_id，避免重复副作用 |

## 场景模板

### UV_NOT_FOUND
对用户说：当前系统未安装 uv 工具，请执行 `curl -LsSf https://astral.sh/uv/install.sh | sh` 安装后回复"已安装"。

### SSO_EXPIRED
对用户说：SSO Token 已过期，请重新登录内网后回复"已登录"。

### API_TIMEOUT
重试 1 次，仍失败则中止；对用户说：请求超时（已重试 1 次），请检查 VPN 后重试。

### RATE_LIMITED
等 3~5 秒后重试 1 次，仍失败则中止；对用户说：KConf 频控触发（请求过于频繁），已稍作等待重试，若仍失败请稍后再试。搜索/目录批量操作时主动降低请求频率。

### API_4XX / KCONF_RESULT_NOT_1
将 KConf 返回的 `message` 原样呈现给用户，由用户决定下一步。

### API_5XX
立即中止，不重试；对用户说：KConf 服务暂时不可用，请稍后重试。

### VERIFY_MISMATCH
停止，明确说明写操作已提交但回查未确认生效（可能仍在灰度或审批中），附带回查到的实际内容，让用户判断。

### BATCH_TIMEOUT
停止批量任务；汇总已成功、失败、未执行的 key 列表，提示用户可对未完成部分单独重跑（Normal 模式）或缩小批次后重新提交。

### ACTION_ALREADY_DONE
不视为错误；跳过该项并在汇总里标注 `skipped: 已执行过`，避免重复提交。

## AI 执行流程

```
脚本执行
   ↓
退出码 = 0 且 stdout 最后一行是 {"ok": true, ...}？
   ├── 是 → 正常处理 data
   └── 否 → 解析 JSON 中的 error 字段 → 查表 → 执行对应动作与话术
```
