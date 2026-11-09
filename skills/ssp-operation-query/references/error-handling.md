# 错误处理

运行脚本出现异常时，先以进程退出码和 stdout 最后一行 JSON 为准，再按下表处理；不要根据自然语言猜测后续动作。

| 错误码 | 动作 | 说明 |
|---|---|---|
| `INVALID_INPUT` | `ASK_USER` | 任务 JSON、接口标识、HTTP 方法或参数类型不合法；请用户补充或修正。 |
| `ENDPOINT_NOT_ALLOWED` | `ASK_USER` | 请求了目录外或非只读接口；改用 `query-catalog.md` 中的接口。 |
| `SSO_EXPIRED` | `STOP` | 内网登录态失效；请用户完成 SSO 登录后再试。 |
| `AUTH_FAILED` | `ASK_USER` | 当前账号没有目标接口权限；请用户申请权限。 |
| `API_TIMEOUT` | `RETRY_ONCE_THEN_STOP` | 单项请求超时；最多重试一次。 |
| `API_4XX` | `ASK_USER` | 参数格式错误或业务校验未通过；展示服务端响应后请用户修正。 |
| `API_5XX` | `STOP` | 目标服务异常；稍后再试。 |
| `NETWORK_ERROR` | `STOP` | 网络不可达；检查内网连接后重试。 |

批量请求中，单项 API 错误会记录在该任务结果内，脚本整体仍会继续处理其余任务；只有输入、认证或网络初始化问题才会终止整体执行。
