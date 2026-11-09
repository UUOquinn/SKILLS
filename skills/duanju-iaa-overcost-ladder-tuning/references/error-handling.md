# 错误处理

> 执行任何脚本遇到报错，先读本文档，优先按脚本返回的 `code` / `exit code` 和
> `next_action` 执行。禁止跳过查表自行发挥（改写脚本、降级到 python3/pip、
> 绕过 Auth）。

## 核心规则

- 成功判定 = 进程退出码 `0` **且** stdout 最后一行 JSON `ok=true/code=0`。
- 退出码与业务 JSON 不一致时，以非零退出码为准。
- 写操作（策略新增/名单增删）不自动重试，避免重复提交。

## 错误码速查

| error | 退出码 | 动作 | 说明 |
|---|---|---|---|
| `AUTH_EXPIRED` | 2 | 让用户重新登录 | Auth 失效（OLAP 或 Operation） |
| `AUTH_FAILED` | 2 | 让用户申请权限 | 无看板/接口权限 |
| `EMPTY_WINDOW` | 1 | 停止，下一轮重试 | 窗口内无账户数据，实时数据未回补 |
| `TEMPLATE_MISSING` | 1 | 停止 | OLAP 查询模板丢失 |
| `INVALID_ARGUMENT` | 1 | 停止 | 参数不合法 |
| `FILE_NOT_FOUND` | 1 | 停止 | 输入文件不存在 |
| `UNEXPECTED_ERROR` | 1 | 停止 | 未预期异常，读 stderr 定位 |
| `INTERRUPTED` | 130 | 停止 | 用户中断 |

写操作（`create` / `add-ids` / `remove-ids`）委托给 `flow-strategy-ops` 的
`flow_api.py`，其错误码（`AUTH_EXPIRED`、`AUTH_FAILED`、`API_TIMEOUT`、
`API_4XX`、`API_5XX`、`INDUSTRY_VERSION_MISMATCH`、`NOT_FOUND`、
`INVALID_DIMENSION`、`INVALID_ARGUMENT`、`BATCH_NOT_SUPPORTED` 等）按
`flow-strategy-ops/references/error-handling.md` 处理，不要在本 skill 里另写一套。

## 常见场景

### AUTH_EXPIRED

- 动作：停止，引导用户重新登录内网
- 对用户说：`Auth 已过期，请在浏览器访问任意 example.corp 页面完成认证，然后回复"已登录"。`

### EMPTY_WINDOW

- 动作：停止本轮，等下一轮（20 分钟后）再跑
- 对用户说：`窗口内没有账户数据，通常是实时数据还没回补。等下一轮再跑，不会拿空结果写策略。`

### 写操作失败（API_4XX / API_5XX / API_BUSINESS_ERROR）

- 动作：停止，原样记录报错，不自动重试写请求
- 对用户说：将 `flow_api.py` 返回的报错文案原样呈现，并说明「本轮写操作未完成，下轮会按平台当前名单重新判断」。

## 中断续跑

「平台即状态」：账户当前档位 = 它在哪条阶梯策略名单里。中断后直接重跑本轮
即可，天然幂等——已降档的账户下轮查询看到的就是新档位，不会重复下手。
