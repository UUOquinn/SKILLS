---
name: duanju-iaa-overcost-ladder-tuning
description: >-
  按「浅度超成本率」对短剧 IAA 账户做离散阶梯压价：直连 OLAP 数据集 DS_REALTIME 取近 1 小时数据，
  筛选联盟总消耗≥500 且 浅度超成本率≥0.20 的账户，以二十分钟一轮把账户在 0.8/0.6/0.4/0.2
  四档公用策略间逐档下压（基数 1.0，每步 -0.2，0.2 封底），不再超成本则从策略名单撤出。
  账户档位由 4 条「AI_短剧_系数X」公用策略的账户名单承载，平台即状态、重跑幂等。
  当用户提到短剧IAA阶梯压价、超成本率0.20压价、系数0.8/0.6/0.4/0.2档位、
  公用策略账户名单升降档、开始持续跑超成本阶梯时使用；用户在循环中说结束/暂停/停止时按结束汇总收尾。
  不支持非短剧IAA调价、连续系数顶价压价（用 duanju-iaa-roi-cpm-tuning）、批量并发写策略、策略提审与推全。
---

# 短剧IAA超成本率阶梯压价

对「首日变现ROI（短剧IAA专用）」账户按浅度超成本率做离散阶梯压价。与
`duanju-iaa-roi-cpm-tuning` 的连续系数顶价压价不同，本 skill 是**纯压价 + 四档
离散阶梯 + 公用策略名单**：同一档位的所有账户共用一条策略，账户在档位之间迁移。

<!-- skill-creator: askuser-with-text-fallback-v1 -->

## 用户交互：AskUser 优先与文本降级

凡需向用户收集信息、请求确认或要求选择时，优先调用 `askUser` 工具，并以结构化问题和可选项取得答复；关联问题应合并提问。

若 `askUser` 在当前运行环境不存在、调用失败或返回无效结果，才允许降级为普通聊天文本提问。降级时必须明确说明“当前 AskUser 不可用，以下为兼容提问”，并在收到用户明确答复前停止后续依赖该答复的操作；不得猜测、默认代填或把沉默视为同意。

## 前置依赖

本 Skill 依赖 `flow-strategy-ops`（扶持策略 boostControl API）。运行前请确认已安装：

```bash
uv run <skill_directory>/scripts/check_install_dep.py ensure flow-strategy-ops
```

## 批量能力评估

每轮对全部命中账户做判断（读与决策天然批量），但**不支持批量写策略**：写入受
`flow-strategy-ops` 限制，必须按策略串行执行——同一档位多个账户可一次
add-ids / remove-ids，跨档位要分开提交。账本即 4 条公用策略的账户名单
（平台即状态），无需本地检查点，重跑幂等，因此不引入批量编排器。

## 四档公用策略 + 平台即状态

共 4 条公用策略，每条一个系数档位，`bonusRate` 即系数：

| 策略名 | bonusRate | 含义 |
|---|---|---|
| `AI_短剧_系数0.8` | 0.8 | 基数 1.0 第一次压价后的档位 |
| `AI_短剧_系数0.6` | 0.6 | 第二次压价 |
| `AI_短剧_系数0.4` | 0.4 | 第三次压价 |
| `AI_短剧_系数0.2` | 0.2 | 封底档 |

**平台即状态**：账户当前在哪条策略的 `adCluster.accountId` 名单里，就处于哪一档。
不维护本地台账，中断后直接重跑本轮天然幂等——账户已降档后，下轮查询名单看到的
就是新档位，不会重复下手。也意味着：账户当前档位完全依赖平台名单，若名单被人工
改过，判断会随之变化。

## 阈值与档位（唯一权威来源）

Agent 不得在对话里心算或改写。精确逻辑见 `scripts/decide.py`。

- 压价触发筛选：`联盟总消耗 >= 500` **且** `浅度超成本率 >= 0.20`
- 阶梯：`0.8 -> 0.6 -> 0.4 -> 0.2`，每步 -0.2，基数 1.0
- 封底：压到 0.2 仍超，保持 0.2 不动，标记持续监控
- 撤出：账户在策略名单里，但超成本率回落到 0.20 以下 → 从名单拿掉
- 保持：账户仍超成本（≥0.20）但消耗 < 500 → 暂不继续压，保持当前档

## 执行流程（每轮）

取数窗口固定为过去 1 小时（右端对齐到 5 分钟前，规避数据回补延迟），
每二十分钟一轮。一轮三步：

```bash
# 1. 取数：OLAP 聚合账户级超成本率 + 消耗
uv run <skill_directory>/scripts/fetch_accounts.py --output tmp/accounts.csv

# 2. 查 4 条阶梯策略名单
uv run <skill_directory>/scripts/fetch_strategies.py --output tmp/strategies.json

# 3. 决策：进入 / 降档 / 封底 / 撤出
uv run <skill_directory>/scripts/decide.py \
  --accounts tmp/accounts.csv --strategies tmp/strategies.json
```

`decide.py` 输出决策表和分组好的写入计划（`data.adds` / `data.removes`，按档位
分组的账户 ID 列表）。

### 写操作（委托 flow-strategy-ops）

按 `decide.py` 的 `adds` / `removes` 逐档位执行（同一档位多个账户可一次性加/删）：

```bash
# 加名单（进入某档 / 降到某档）
uv run <flow-strategy-ops>/scripts/flow_api.py add-ids <策略ID> --dim accountId --ids 账户1,账户2

# 删名单（降档迁出 / 撤出）
uv run <flow-strategy-ops>/scripts/flow_api.py remove-ids <策略ID> --dim accountId --ids 账户1,账户2
```

写操作串行执行、不并发、不自动重试。add-ids / remove-ids 走 submitEdit 自动提审，
幂等（已存在/不存在会自动跳过）。若 `decide.py` 报 `档位缺失`，先补齐对应策略再写。

## 首次准备（建 4 条策略）

循环开始前，先确认 4 条策略都存在（`fetch_strategies.py` 的 `missing_levels` 列出
缺失档位）。缺失时用 `flow_api.py create` 逐条新建，`bonusRate` 分别为 0.8/0.6/0.4/0.2，
`accountId` 留空（账户后续用 add-ids 加）：

```bash
uv run <flow-strategy-ops>/scripts/flow_api.py create \
  '{"name":"AI_短剧_系数0.8","type":3,"scope":1,"beginTime":{now_ms},"endTime":{now_ms+30天},"background":"AI_短剧_系数0.8","bonusRate":0.8,"isStaticRate":false,"isUseSystemBonus":false,"adCluster":{"placements":2,"placementsArr":[2],"bidType":10,"ocpxActionType":"990","deepConversionType":"0","isAppDirect":false,"shieldEquityCard":false,"shieldSex":false,"shieldFastApplication":false,"shieldFastApplicationV2":false,"shieldFastApplicationV3":false,"ruleType":0,"subRuleType":0,"industryVersion":"6.6"},"mediaCluster":{}}'
```

其余三档同理，只改 `name`、`background`、`bonusRate`（0.6 / 0.4 / 0.2）。
新建后走平台审核（纯新建 batchCreate 若停在草稿，用 `flow_api.py change-status <ID> <状态>`
提交审核），等生效（status=4）后再开始循环。

## 循环节奏

每二十分钟一轮，持续运行直到用户说结束/暂停/停止。推荐用 cron 或后台循环驱动，
每轮按上面三步 + 写操作串行执行。结束时按 `decide.py` 的 summary 汇总本轮动作。

## 错误处理

遇到报错先读 `references/error-handling.md`，按 `code` / `exit code` 和
`next_action` 执行，不要自行发挥。写操作失败不自动重试。
