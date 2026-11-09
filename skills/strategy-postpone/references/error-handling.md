# Error Handling — strategy-postpone

执行脚本遇到报错时，先查此表再行动，禁止跳过自行发挥。

## 错误码速查表

| error_code | 动作 | 用户提示 |
|---|---|---|
| `UV_NOT_FOUND` | 停止，引导安装 uv | `当前系统未安装 uv。请执行：curl -LsSf https://astral.sh/uv/install.sh \| sh，安装后回复"已安装"。` |
| `SSO_EXPIRED` | 停止，引导重新登录 | `SSO Token 已过期，请在浏览器访问任意 example.corp 页面重新登录，完成后回复"已登录"。` |
| `AUTH_FAILED` | 停止，告知权限不足 | `当前账号无权限执行该操作，请联系管理员开通。` |
| `STRATEGY_NOT_FOUND` | 记失败，继续下一条 | （不中断流程，汇总表中显示"失败 — STRATEGY_NOT_FOUND"）|
| `INVALID_IDS` | 停止，请用户确认 ID | `未解析到有效策略 ID，请检查输入格式（如：15392,382）。` |
| `API_TIMEOUT` | 重试 1 次，仍失败则中止该条 | `请求超时，已重试 1 次仍失败。请检查网络 / VPN 连接。` |
| `API_4XX` | 展示错误详情 | 将 API 返回 msg 原样呈现 |
| `API_5XX` | 中止，不重试 | `目标服务暂时不可用，请稍后重试。` |
| `NETWORK_ERROR` | 提示检查 VPN | `网络不可达，请确认已连接内网 VPN。` |
| `UNEXPECTED_ERROR` | 中止，展示 stderr 诊断 | 将错误信息原样展示 |
| `SIGINT` | 停止，不重试 | — |

## AI 执行流程

```
运行脚本
  ↓
exit code = 0 且 stdout 最后一行 ok=true ?
  ├── 是 → 读取 data.results，渲染汇总表
  └── 否 → 解析 error 字段 → 查上表 → 执行对应动作
```

## 特殊场景

- **401 / Cookie 过期**：归类为 `SSO_EXPIRED`
- **业务返回 412（行业版本系统升级）**：已通过提交体写入 `adCluster.industryVersion = "6.6"` 解决。若平台行业版本升级导致重现，修改 `scripts/postpone.py` 中 `INDUSTRY_VERSION` 常量即可（GET 详情不返回此字段，也无单独的版本查询接口，只能本地维护）
- **`adCluster.maxNum` 等 Kconf 抽象类型字段**：GET 返回的 `{versioned:{version,data},inited}` 包装字段服务端无法反序列化回去（`com.kuaishou.kconf.client.Kconf` 抽象类型），脚本已自动在提交前删除此类字段，依赖服务端对未传字段保留原值
- **单条连续失败 2 次**：记入失败表，继续下一条
- **维度字段被清空**：通常因提交前未带全量 GET，禁止用空 body 重试
