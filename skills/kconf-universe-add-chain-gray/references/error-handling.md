# 错误处理

执行脚本失败时，以进程退出码和 stdout 最后一行 JSON 为准；先按错误码执行，不以日志文本猜测替代。

| 错误码 | 处理方式 |
|---|---|
| `SSO_EXPIRED` | 停止，请用户完成内网登录（如需扫码则扫码）后再执行；可用 `sso-check` 逐域确认登录态。 |
| `AUTH_FAILED` | 停止，提示当前账号缺少 Data Agent、KConf 或 flowControl 权限。 |
| `API_TIMEOUT` | 只读查询仅重试一次；KConf 写入不自动重试，先用 `check-action` 回查。 |
| `API_4XX`、`API_BUSINESS_ERROR` | 展示服务端信息，请用户核对查询条件、行业版本或审批人。 |
| `API_5XX` | 停止并稍后再试；不得重复提交 KConf 写入。 |
| `NETWORK_ERROR` | 停止，请用户检查内网网络。 |
| `VALIDATION_ERROR` | 修正规则文件、目标环境或参数后重新预览。 |
| `ACTION_MISMATCH` | 重新生成预览，不得复用旧 `action_id`。 |
| `ACTION_ALREADY_APPLIED` | 不重复写入，返回已验证状态。 |
| `CONFIRMATION_REQUIRED` | 展示预览，等待用户明确确认。 |
| `KCONF_FAILED`、`INVALID_RESPONSE` | 停止，保留输出并人工核对下游服务返回。 |

退出码 `0` 表示成功；`1` 是普通失败；`2` 表示权限或需要确认；`130` 表示用户取消。写操作失败后不得改用浏览器 Cookie、临时接口请求或紧急通道。
