# Error Handling — 联盟审核 Skill 错题本

执行 `scripts/union_audit.py` 遇到任何报错，先读本文件，按脚本返回的 `error` 码与 `next_action` 行动，禁止自行发挥。

## 错误码速查表

| error | 动作 | 对用户的话 |
|-------|------|-----------|
| `UV_NOT_FOUND` | 停止，禁止降级 python3 | 未安装 uv。请执行 `curl -LsSf https://astral.sh/uv/install.sh \| sh` 安装后回复"已安装"。 |
| `DEPENDENCY_MISSING` | 停止 | uv 依赖安装失败（多为内网源不通）。核对脚本头部 `[[tool.uv.index]]` 的 url 是否为 `https://app.example.corp/kuaishou/prod/+simple/`，确认已连内网后重试。 |
| `API_TIMEOUT` | 重试 1 次，仍失败则中止 | 请求超时（已重试 1 次仍失败）。审核全流程含推全收敛最长 3 分钟；先到 Targeting 审核平台确认策略是否已在"推全中"，不要盲目整单重审。 |
| `API_4XX` | 展示接口返回的 msg | 将接口返回的 msg 原样呈现（常见：状态流转不允许、审核记录不存在、不能审批自己提交的策略）。 |
| `API_5XX` | 立即中止，不重试 | 联盟诊断工作台暂时不可用（5xx），稍后重试。 |
| `AUTH_FAILED` | 停止，抛给用户 | 当前账号无权限执行该操作。 |
| `NETWORK_ERROR` | 提示检查内网 | 无法连接 10.0.0.1:3000，请确认已连公司内网/VPN。 |
| `API_ERROR` | 展示 msg | 工作台接口返回异常：将 msg 转述给用户，必要时用 `--action query --ids <id>` 复核策略状态。 |

## 业务性失败（接口 200 但动作未成功）

这类失败 `code=0`、`data.items[].ok=false`，属于策略状态不允许，不是脚本错误：

- 消息含 `无法应用于状态` → 策略不在可执行该动作的状态（如已发布成功再审核）。解释给用户，不要重试。
- 消息含 `APPROVE_NOT_FOUND` / `未找到审核记录` → 策略未提交审核或审核已结束。
- 消息含 `权限` → 不能审批自己提交的策略。
- `stuckAt=6` 或 `auditOk=true 但 publishOk=false` → 审核通过但发布失败，卡在"审核成功"，到 Targeting 确认。

## AI 执行流程

```
脚本执行
  ↓
退出码 = 0 且 stdout 最后一行 {"ok": true, ...}？
  ├── 是 → 正常处理 data（注意 items[] 里单项 ok=false 是业务失败，按上表解释）
  └── 否 → 解析 error 字段，查本表执行对应动作
```

## 环境预检

- 脚本用 `uv run` 执行；stdout 最后一行必须是业务 JSON，前面的 `>>` 进度行可忽略。
- 工作台为内网地址，需在快手内网环境运行；Cookie/Auth 由 `SmartAuthSession` 自动处理，禁止手动读取本地 token。
