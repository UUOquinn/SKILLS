---
name: topbid-strategy-realtime-monitor
description: 读取 flowControl 顶价扶持策略（type=3 顶价 / type=5 CPA顶价），按 scope 判断广告主顶价/媒体顶价/广告主*媒体顶价并确定监控维度，直连 OLAP 数据集 129496（实时，默认当天）或 85587（离线 T-1）取数，按用户设定的「指标 比较符 阈值」规则判定是否触发，触发后按配置执行撤回或降低顶价系数。当用户提到顶价策略监控、顶价扶持策略实时监控、监控策略超成本率/消耗/ROI 并撤回或降系数、读策略 scope 定维度监控、129496/85587 顶价监控时使用。不支持非顶价策略（绿通/打折/索引加白）、非 flowControl 的策略、批量并发写策略；非顶价/非 flowControl 场景不触发。
---

# 顶价扶持策略实时监控

读取一条或多条 flowControl 顶价策略，按策略维度直连 OLAP 取数，用用户设定的规则判定，触发后撤回策略或降低顶价系数。

## 运行模式：一轮一个判断，持久监控由用户定频

本 skill 每执行一次跑「一轮」：读策略 → 取数 → 判定 → 动作 → 报告。**不内置常驻循环**。

持久监控（实时盯盘）由用户决定触发频率——通常用定时任务/cron 定时调用本 skill，每轮重读本 SKILL.md 与平台实时数据，不复用上一轮结论。interval 由用户指定，本 skill 不预设。

<!-- skill-creator: askuser-with-text-fallback-v1 -->

## 用户交互：AskUser 优先与文本降级

凡需向用户收集信息、请求确认或要求选择时，优先调用 `askUser` 工具，并以结构化问题和可选项取得答复；关联问题应合并提问。

若 `askUser` 在当前运行环境不存在、调用失败或返回无效结果，才允许降级为普通聊天文本提问。降级时必须明确说明“当前 AskUser 不可用，以下为兼容提问”，并在收到用户明确答复前停止后续依赖该答复的操作；不得猜测、默认代填或把沉默视为同意。

## 第一原则

1. **写操作（撤回/降系数）必须先确认**。任何撤回或降系数前，先出报告给用户看；建议先 `--dry-run` 预览，用户确认后再去掉 `--dry-run` 正式执行。
2. **不推测指标口径**。指标值一律来自 OLAP/KwaiBI 接口原始值；比值类指标按消耗加权聚合（等价于 OLAP 平台口径），不手工算术平均。
3. **非顶价策略不监控**。`type` 不是 3（顶价）或 5（CPA顶价）的直接跳过，不触发任何动作。
4. **动作在策略级**。撤回/降系数作用在整条策略（`bonusRate`），不是单个账户。

## 输入（用户提供）

- 策略 ID（必填，可多个逗号分隔）
- 监控指标：联盟总消耗 / 浅度超成本率 / IAA广告变现ROI / 自定义（必须存在于所选数据集）
- 规则：`指标 比较符 阈值`，比较符支持 `>` `<` `>=` `<=` `=` `==` `!=`
- 动作：是否撤回 + 降低系数多少（二选一或都配）
- 数据集：129496 实时（默认当天）/ 85587 离线（T-1 及历史）

## 第一步：读策略定维度

先跑只读脚本读策略，确认类型与维度：

```bash
uv run <skill_directory>/scripts/inspect_strategy.py --ids 27123,27124
```

- `type`：3=顶价（CPM 加系数）、5=CPA顶价；其余（绿通/打折/索引加白）跳过。
- `scope`：1=广告主顶价、2=媒体顶价、3=广告主*媒体顶价（双维度）。
- 维度：从 `adCluster`/`mediaCluster` 提取具体值（账户ID、营销目标、转化目标、深度目标、广告场景、联盟媒体ID、广告位）。

监控维度规则：

| 顶价类型 | 监控维度 |
|---|---|
| 广告主顶价（scope=1）/ 媒体顶价（scope=2） | 广告主信息整体；若策略还指定了广告样式/浅度优化目标/深度优化目标/营销目标，则按广告主信息 × 这些维度过滤监控 |
| 广告主*媒体顶价（scope=3） | 广告主信息 × 媒体信息 |
| 未指定（scope=0） | 按策略实际配置的维度过滤（有 accountId 就按广告主过滤），不做跨维度交叉 |

## 第二步：取数（口径抄 OLAP 平台）

指标口径直接沿用数据集字段定义，本地只做消耗加权聚合（比值指标）：

| 指标 | 129496 fieldId | 85587 fieldId | 口径 |
|---|---|---|---|
| 联盟总消耗 | 2215299 | 1329556 | 现金+前返+后返+激励+框返+信用+电商采买实收 |
| 浅度超成本率 | 2215353 | 1330672 | 单元浅度成本超出比率 |
| IAA广告变现ROI | 7098032 | 无 | LTV/总消耗 |

129496 实时、85587 离线统一走 KwaiBI 直连接口（`bi-platform.example.corp/api/v1/data/query`，同一套平台字段 ID，口径与 OLAP 一致；OLAP 模板保留在 references 作口径参考）。取值窗口：`today`（当天实时）/ `yesterday`（T-1）/ `last_hour`（上一个完整小时，分小时监控用）。

聚合维度：策略带账户 ID 按账户分组；纯媒体顶价（无账户）按联盟媒体 ID 分组拿媒体级汇总——不能按账户分组硬拉全量账户行，会被行数上限截断导致口径错。比值指标聚合 = `Σ(消耗×指标) / Σ消耗`，非算术平均。

## 第三步：判定 + 动作

执行一轮监控：

```bash
# 预览（不写）
uv run <skill_directory>/scripts/monitor.py --dry-run --config '<json>'

# 正式执行
uv run <skill_directory>/scripts/monitor.py --config '<json>'
```

config 结构：

```json
{
  "dataset": 129496,
  "strategies": [
    {"id": 27123, "metric": "浅度超成本率", "op": ">=", "threshold": 0.20,
     "withdraw": false, "reduce_coefficient": 0.10, "window": "today", "floor": 1.0}
  ]
}
```

- `window`：`today`（今天 00:00~当前整点，完整小时）/ `yesterday` / `last_hour`（上一个完整小时，分小时监控用）。实时集非整点 end 会触发服务端「新增列」报错，所以 today 对齐整点。
- `floor`：降系数封底，默认 1.0（降到 1.0 = 取消顶价）。

多策略批量：`strategies` 数组放多条即可（逗号分隔 ID 也可，但建议每条独立配置 metric/阈值/动作）。

**连续段模式（streak）**：连续 N 个小时满足条件才动作，避免单小时抖动误触发。配置样例（连续 4h 超 30% 降 0.1，连续 24h 低于 25% 提 0.01）：

```json
{"id": 27387, "metric": "浅度超成本率", "window": "last_hour",
 "streak": {
   "down": {"op": ">", "threshold": 0.30, "hours": 4, "delta": 0.10, "floor": 1.0},
   "up":   {"op": "<", "threshold": 0.25, "hours": 24, "delta": 0.01}
 }}
```

- 连续计数持久化在 `--state` 状态文件（默认 cwd `.topbid-monitor-streak-state.json`），跨轮累计；窗口无数据时计数保持不变（断档不断连续）。
- 动作后重新计数；到封底（降无可降）时保持计数并标记 `at_floor`，首次到封底 `floor_first=true`（供推送降噪：只报第一次，后续轮次静默）。
- `--dry-run` 在副本上计算，**不会推进/重置真实计数**，预览后正式轮次照常触发。
- 提系数（raise）无上限，`delta` 需 > 0。

- 判定：`整体指标值 比较符 阈值` → 命中/未命中。
- 命中动作：
  - 撤回：`POST /flowControl/withdraw`
  - 降系数：`POST /flowControl/mergeEdit` 改 `bonusRate`（原值 - 降幅，封底 floor）

## 批量能力（多策略）

批量能力评估结论：**启用编排模式（orchestrated，串行）**。多策略监控含撤回/降系数写副作用，故串行执行、写前须确认。

- **Normal 模式**：单策略直接 `--config '<json>'`（strategies 数组只有一条）。
- **Batch 模式**：`strategies` 数组多条，脚本串行逐条 `process_one`；`--batch-size` 限批、`--max-runtime` 限时。
- **单项重试**：`--item <策略ID>` 只重跑该策略。
- **敏感操作幂等**：撤回/降系数都派生稳定 `action_id`（`withdraw:<id>` / `reduce:<id>:<old>-><new>`），`--check-action <action_id>` 回查 flowControl 当前状态判定是否已执行。
- **权威数据源**：flowControl（`--authoritative-source`）。

## 报告

脚本 stdout 最后一行是业务 JSON，前面是每策略一行的人读摘要：

```
策略 27123 [广告主顶价] 浅度超成本率=0.2345 >=0.20 → 触发 | 已降低系数 0.10（1.20 → 1.10）
```

把 JSON 的 `data.results` 里的每策略 `metric/overall/triggered/actions` 整理成表格报告给用户，注明是否为 dry-run。

## 禁止事项

- 禁止对非顶价策略（type≠3/5）执行撤回/降系数。
- 禁止在没有 dry-run 预览、未经用户确认时直接执行撤回/降系数。
- 禁止对 85587 查询「IAA广告变现ROI」（该指标仅 129496 有）。
- 禁止手工算术平均比值指标；必须消耗加权。
- 禁止复用上一轮取数结果；每轮重读平台实时数据。

## 更多资料

- flowControl 类型/scope/撤回/降系数契约与字段映射：[references/flow-control-api.md](references/flow-control-api.md)
- 报错处理：[references/error-handling.md](references/error-handling.md)
- OLAP 查询模板：[references/olap_query_template_129496.json](references/olap_query_template_129496.json)
