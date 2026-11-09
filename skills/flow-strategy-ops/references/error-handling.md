# 错误处理

执行 `scripts/flow_api.py` 报错后，先解析进程退出码和 stdout 最后一行 JSON，再按本表处理。
只有退出码 `0` 且 JSON `ok=true/code=0` 才算成功。禁止改用 UI、浏览器自动化、手工 Cookie 或裸 `python`。

| error | exit | 动作 |
|---|---:|---|
| `AUTH_EXPIRED` | 2 | 请求用户重新完成内网认证；不要代开网页操作 |
| `AUTH_FAILED` | 2 | 告知用户当前账号无 boostControl 权限 |
| `VALIDATION_FAILED` | 1 | 展示 msg 中的真实原因（缺必填字段/名单冲突等）；对照 API.md 补字段后重试。**不是**行业版本问题 |
| `INDUSTRY_VERSION_MISMATCH` | 1 | msg 明确提到行业版本时才会报此错；检查顶层/广告维度 `industryVersion` 后重试 |
| `API_TIMEOUT` | 1 | 只读操作可重试一次；写操作停止，先查状态避免重复提交 |
| `NETWORK_ERROR` | 1 | 检查内网/VPN 后重试 |
| `API_4XX` | 1 | 展示服务端错误，修正参数或权限 |
| `API_5XX` | 1 | 停止，稍后重试；写操作先查状态 |
| `API_BUSINESS_ERROR` | 1 | 展示业务码和消息，不猜测 |
| `INVALID_RESPONSE` | 1 | 停止并展示响应摘要 |
| `EXPORT_FAILED` | 1 | 展示服务端错误信息；导出接口失败时不落盘 .xlsx |
| `INVALID_ARGUMENT` | 1 | 修正 ID 或 JSON 输入 |
| `INVALID_DIMENSION` | 1 | 名单维度不合法；按「名单维度表」改用 appId/uid/posId/accountId 等支持维度 |
| `NOT_FOUND` | 1 | 核对策略 ID |
| `NOT_WRITABLE` | 1 | 目标策略已删除或已失效，写操作被拦截；换一条生效中的策略 |
| `BATCH_NOT_SUPPORTED` | 1 | 改为一次处理一个策略 |
| `INTERRUPTED` | 130 | 停止，不自动重试 |
| `UNEXPECTED_ERROR` | 1 | 停止并展示诊断信息 |

## 写操作幂等

新建、修改并提审、名单增删、状态流转、删除和立即推全都不自动重试。名单增删自身幂等：
新增已存在的 ID 或删除不存在的 ID 都会跳过、不写线上，返回 `ALREADY_PRESENT` / `ALREADY_ABSENT`。
若请求超时或结果未知，先执行 `get`、`log` 或 `diff` 确认服务端状态，再由用户决定是否重试。
