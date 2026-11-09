# Error Handling — Operation + BI Scripts

## AI 执行规则

执行 `operation_client.py` 或 `kwaikwaibi_client.py` 遇到报错时，优先按脚本 stdout 最后一行的 JSON 中 `error` 和 `next_action` 执行。不要跳过查表自行发挥。

## 错误码速查

| error_code | 动作 | 说明 |
|-----------|------|------|
| `AUTH_EXPIRED` | 引导用户重新触发 Auth 登录 | Token 过期或未认证 |
| `AUTH_FAILED` | 询问用户 | 401/403，权限不足 |
| `API_TIMEOUT` | 重试 1 次，仍失败则中止 | 网络超时 |
| `API_4XX` | 展示错误信息给用户 | 请求参数问题 |
| `API_5XX` | 中止，告知用户稍后重试 | 服务端故障 |
| `API_ERROR` | 中止，展示错误信息 | 未归类的 API 错误 |
| `NETWORK_ERROR` | 提示用户检查 VPN | 网络不可达 |
| `PREFIX_NOT_FOUND` | 告知用户策略 ID 未命中任何已知类型 | 策略类型探测失败 |
| `STRATEGY_NOT_FOUND` | 告知用户 get 返回空壳 | 策略不存在或 ID 错误 |
| `MISSING_ID` | 提示用户提供 --id 或 --ids | CLI 参数缺失 |
| `FILE_NOT_FOUND` | 提示用户检查文件路径 | --file 指定的 JSON 文件不存在 |
| `UNKNOWN_COMMAND` | 展示帮助信息 | CLI 命令拼写错误 |
| `DIMENSION_INVALID` | 停止，提示正确维度 | search/query-alias 的 --dim 值不合法 |
| `BATCH_TOO_LARGE` | 提示分批（同维度一起、不同维度分开） | 单次 ID 数超过 50 |
| `DICT_PARSE_FAILED` | 用内置静态字典兜底，可继续 | industry_lookup refresh 解析失败 |
| `OLAP_QUERY_FAILED` | 停止，核对时间窗、字段及模板；扫描超限则缩至单日 | 流量侧 OLAP 模式业务错误或列不匹配 |

## 场景模板

### AUTH_EXPIRED

- **动作**：停止，引导用户重新登录
- **对用户说**：Auth 登录已过期。请在浏览器中访问任意 `example.corp` 页面完成认证，然后重新执行。

### API_TIMEOUT

- **动作**：重试 1 次，仍失败则中止
- **对用户说**：请求超时（已重试 1 次仍失败）。请检查 VPN 连接，稍后重试。

### AUTH_FAILED

- **动作**：停止，把问题抛给用户
- **对用户说**：当前账号无权限执行该操作，请联系管理员开通权限。

### PREFIX_NOT_FOUND

- **动作**：告知用户策略 ID 可能不是已知类型，建议用户确认 ID 正确性或指定 --prefix
- **对用户说**：策略 ID 未命中 targetingControl / boostControl / shadowControl / placementControl / internalControl / shareControl 任一前缀。请确认 ID 是否正确，或通过 --prefix 手动指定类型。

### STRATEGY_NOT_FOUND

- **动作**：告知用户策略不存在
- **对用户说**：策略 ID 查询返回空结果，可能不存在或已被删除。