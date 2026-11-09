# Error Handling（错题本）

执行 `scripts/monitor.py` / `scripts/inspect_strategy.py` 遇到报错，**先查本表**，
按脚本返回的 `code` / `exit code` 和 `next_action` 执行。禁止跳过查表自行发挥。

## 错误码速查

| error | code | next_action | 处理 |
|---|---|---|---|
| `AUTH_EXPIRED` | 2 | ASK_USER | 引导用户在内网浏览器访问任意 `example.corp` 页面完成认证后重试 |
| `AUTH_FAILED` | 2 | ASK_USER | 告知用户无 boostControl / OLAP 接口权限，联系管理员 |
| `INVALID_CONFIG` | 1 | STOP | 检查 config JSON：strategies 数组、id/metric/op/threshold 必填 |
| `INVALID_ARGUMENT` | 1 | STOP | 检查参数：op 只支持 `> < >= <= = == !=`；window 只支持 today/yesterday |
| `NOT_FOUND` | 1 | STOP | 策略 ID 不存在（业务错误，非网络） |
| `API_4XX` / `API_5XX` | 1 | STOP | 展示 msg 给用户；5xx 不自动重试 |
| `NETWORK_ERROR` | 1 | STOP | 检查网络/VPN |
| `OLAP 返回空结果` | 1 | STOP | 窗口内无数据或模板列被改；核对 `references/olap_query_template_realtime.json` |
| `过滤后无任何账户数据` | 1 | STOP | 策略账户不在数据集/窗口内，先确认数据是否回补 |

## 常见场景

### AUTH_EXPIRED
- 动作：停止，引导用户重新登录（`SmartAuthSession` 自动刷新，必要时重新扫码）
- 对用户说：`内网登录态已失效，请在浏览器访问任意 example.corp 页面完成认证后回复"已登录"。`

### 指标不在数据集
- 动作：停止，提示用户换指标
- `IAA广告变现ROI` 仅 DS_REALTIME 有；DS_OFFLINE 无此指标。自定义指标必须存在于所选数据集。

### 模板缺日期 filter
- 动作：停止，重新抓取模板
- 原因：模板 `filters[].filterValue.startTime/endTime` 被改或丢失；脚本拒绝执行避免读抓包旧数据。

### 写操作（撤回/降系数）前提
- 只在 `triggered=true` 且 config 显式 `withdraw=true` 或 `reduce_coefficient>0` 时执行。
- 建议先用 `--dry-run` 预览，确认后再正式执行（去掉 `--dry-run`）。
