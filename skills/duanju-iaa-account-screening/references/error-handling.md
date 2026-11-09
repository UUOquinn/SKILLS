# 错误处理

运行脚本失败时先核对进程退出码（`0/1/2/130`）与 stdout 最后一行 JSON 中的 `ok`、`code`、`error`、`next_action`。只有退出码 `0` 且 `ok=true` 算成功；非零退出不能被打印日志或残留旧文件覆盖。错误策略见 `error-policy.json`。

| 错误码 | 处理动作 |
|---|---|
| `INVALID_DATE` / `INVALID_ARGUMENT` | 停止；更正 `--date YYYY-MM-DD`（已完成的北京自然日）或 `--item`（七猫/爱奇艺/番茄）。|
| `TEMPLATE_MISSING` / `TEMPLATE_INVALID` | 停止；检查固化模板是否缺失或口径漂移，重新按原看板抓取并对数，不要猜字段。|
| `SSO_EXPIRED` | 停止；先恢复 `olap.example.corp` 的认证，再重试，不降级到看板域名。|
| `AUTH_FAILED` | 停止；联系数据集权限管理员确认当前身份可查询。|
| `API_TIMEOUT` | 最多重试一次，仍失败则停止。|
| `API_4XX` / `API_5XX` / `NETWORK_ERROR` | 停止；按返回的状态/网络报错定位，不盲目重试。|
| `QUERY_FAILED` / `COLUMNS_CHANGED` / `INVALID_ROW` | 停止；对照看板字段及请求体；不交付可能错误的账户清单。|
| `RESULT_TRUNCATED` | 停止；服务端行数或截断标志异常，不能宣布“全量筛出”。|
| `EMPTY_RESULT` | 停止；核对日期和数据回补，不把空结果当成功。|
| `UNEXPECTED_ERROR` | 停止；保留 stderr 错误摘要以定位原因。|
| `INTERRUPTED` | 立即停止；不自动重试。|
| `KCONF_SSO_EXPIRED` | 停止；先恢复 `kconf.example.corp` 的 SSO 认证再重试，不跳过暗投白名单。|
| `KCONF_QUERY_FAILED` / `KCONF_PARSE_ERROR` | 停止；核对 KConf key `ad.adtarget3.universeAddChainGrayConfig` 结构与内容，不臆造白名单。|
| `KCONF_AB_KEY_MISSING` | 停止；配置中找不到 `enable_chain_antou_30_990_0`，向用户确认配置是否迁移，不继续第二步。|
| `EXCLUSION_EMPTY` | 停止；第一步与 KConf 排除集都为空意味着口径异常，需人工核对后再跑。|
| `FILE_NOT_FOUND` / `VALIDATION_ERROR` | 停止；`--accounts-file` 指向的账户清单不存在、缺「账户ID」列或格式非法，先重跑第二步生成清单。|

本流程只读；重跑不会修改业务平台。脚本仅在查询与完整性核验均成功后覆盖对应日期的 `tmp/` 清单文件；失败时可能留有以前成功运行的同名文件，不能凭文件是否存在判断本轮成功。
