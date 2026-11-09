---
name: union-strategy-insights
description: >
  封装联盟诊断工作台已验证的 Operation（ops-platform）与 KwaiBI（kwaibi）上游 REST API，并提供 129496 实时数据的 OLAP 直连快路径。
  触发场景：查询联盟广告策略、按 ID 反查 appid/uid/posid 被哪些屏蔽策略覆盖及屏蔽的行业/产品名称、
  按策略维度或 ID 维度查效果数据（行业/产品周期消耗、流量侧场景/合作模式/媒体类型/投放类型/联盟运营分类）、查当天/实时指标（OLAP 加速）、修改策略提审延期推全、查询数据集字段与结构化查数。
  不触发场景：非联盟广告问题、纯数据平台对话式取数（走 data-agent）、效果监控/定时推送/Kim 消息编排。
  关键词：Operation、Orient、定投、审核、延期、mergeEdit、KwaiBI、dataset、data/query、85587、129496、olap、实时、屏蔽查询、反查屏蔽、行业消耗、产品消耗。
---

# Operation + KwaiBI API

本技能封装 Operation（策略运营平台）与 KwaiBI（数据分析平台）的上游 REST API，通过 Python 脚本统一调用，SSO 认证由 `SmartSSOSession` 自动管理，对 AI 执行完全透明。

<!-- skill-creator: askuser-with-text-fallback-v1 -->

## 用户交互：AskUser 优先与文本降级

凡需向用户收集信息、请求确认或要求选择时，优先调用 `askUser` 工具，并以结构化问题和可选项取得答复；关联问题应合并提问。

若 `askUser` 在当前运行环境不存在、调用失败或返回无效结果，才允许降级为普通聊天文本提问。降级时必须明确说明"当前 AskUser 不可用，以下为兼容提问"，并在收到用户明确答复前停止后续依赖该答复的操作；不得猜测、默认代填或把沉默视为同意。

## 快速开始

### Operation 策略查询

```bash
# 单条查询（自动探测策略类型）
uv run <skill_directory>/scripts/operation_client.py get --id 12345

# 批量查询多个 ID
uv run <skill_directory>/scripts/operation_client.py get --ids 12345,67890,11111

# 按条件列表查询
uv run <skill_directory>/scripts/operation_client.py query --app-id 570400002

# 按 ID 反查被哪些屏蔽策略覆盖（同维度批量，不同维度分开查）
uv run <skill_directory>/scripts/operation_client.py search --dim appId --ids 10000001,123456

# 行业 code 翻译成名称
uv run <skill_directory>/scripts/industry_lookup.py lookup --codes 1028,1029,1030
uv run <skill_directory>/scripts/industry_lookup.py refresh

# 延期提审（只改 endTime）
uv run <skill_directory>/scripts/operation_client.py extend --id 12345 --end-time 1735689600000

# 撤回 / 推全 / 审核查询
uv run <skill_directory>/scripts/operation_client.py withdraw --id 12345
uv run <skill_directory>/scripts/operation_client.py quick-push --prefix orientControl --id 12345 --type 0
uv run <skill_directory>/scripts/operation_client.py list-user
```

### OLAP 实时快路径（129496）

当用户明确查询**当天/实时**数据，且已有适用的、无残留业务过滤的 129496 OLAP 模板时，优先使用：

```bash
uv run <skill_directory>/scripts/olap_client.py \
  --template <template.json> --start <epoch_ms> --end <epoch_ms> \
  --referer <olap_dashboard_url> --dim uid --ids 1,2,3 --fields scene,cost
```

- 只支持 `uid` / `app_id` / `pos_id` 中一个维度，单次最多 50 个 ID；
- `--fields` 用内置字段表填充维度/指标（如 `scene,cost`）；模板自带 fields 时不传；
- 接入域名为 `kwaibi.corp`（OLAP 查询模式）；`olap.corp` 直连在当前 SSO 环境不可用，不要改回直连；
- 模板日期过滤由脚本改写；结果数值读取原始 `r` 字段；
- 模板带特定业务过滤时只能用于该业务，不能作为通用策略查询模板；
- 85587 历史/T-1 查询、临时字段、复杂筛选、OLAP 异常或对账不通过时，仍走 KwaiBI API；
- 详情见 [references/olap_129496.md](references/olap_129496.md)。

流量侧消耗可直接用已封装的查询命令，不再临时拼 Python 请求体：

```bash
uv run <skill_directory>/scripts/kwaibi_client.py query-alias --data-set 129496 \
  --dim app_id --ids 540400001,1387000058 --group-by scene --days 1
```

`--group-by` 支持 `scene`（广告场景）、`coop`（合作模式）、`mtype`（媒体类型_内外部）、`deliver`（投放类型）、`opclass`（联盟运营分类）；这些新增维度仅支持 129496。走 `kwaibi.corp` 的 OLAP 查询模式，不是 `olap.corp` 域名直连。每次最多 50 个同维度 ID；大窗口扫描超限时缩至逐日查询。`opclass` 不等于一个已验证名为「流量分类(流量侧)」的字段；用前须核对口径。

### KwaiBI 数据查询

```bash
# 查数据集字段
uv run <skill_directory>/scripts/kwaibi_client.py dataset-detail --id 85587

# 结构化查数（--body-file 或 --fields + --filters + --start/--end）
uv run <skill_directory>/scripts/kwaibi_client.py query --data-set 85587 --body-file body.json

# 按 ID 维度查行业/产品周期消耗（同维度批量，不同维度分开查）
uv run <skill_directory>/scripts/kwaibi_client.py query-alias --dim uid --ids 10000001 --group-by industry --days 7
uv run <skill_directory>/scripts/kwaibi_client.py query-alias --dim app_id --ids 10000001 --group-by product --days 7
uv run <skill_directory>/scripts/kwaibi_client.py query-alias --dim uid --ids 10000001 --group-by industry --start 1719936000000 --end 1720022399999
```

## 读哪个文件

| 内容 | 文件 |
|------|------|
| Operation API 详细接口说明 | [tools-operation.md](tools-operation.md) |
| KwaiBI API 详细接口说明 | [tools-kwaibi.md](tools-kwaibi.md) |
| 129496 OLAP 快路径与模板约束 | [references/olap_129496.md](references/olap_129496.md) |
| 屏蔽反查与行业字典语义 | [references/shield-search.md](references/shield-search.md) |
| 脚本错误码与处理动作 | [references/error-handling.md](references/error-handling.md) |
| 错误码机器策略 | [references/error-policy.json](references/error-policy.json) |

## 鉴权说明

两个脚本均使用 `ks_aimate.sso_login_client.SmartSSOSession` 管理 SSO 认证，Session 创建、Token 刷新、登录态维护全部由 SDK 内部处理。**不要手动传入 Cookie 或自行调 `sso_session.py` 取 Cookie**——脚本内部已经做了这些事，直接 `uv run` 即可。

如果执行时出现 `SSO_EXPIRED`，引导用户在浏览器中访问任意 `example.corp` 页面完成认证，然后重新执行脚本。

## Operation 脚本命令参考

| 命令 | 说明 | 示例参数 |
|------|------|----------|
| `get` | 单条/批量查询策略详情 | `--id 12345` 或 `--ids 1,2,3` |
| `query` | 按条件列表查询 | `--app-id 570400002 --uid xxx --status 4` |
| `search` | 按 ID 反查屏蔽策略（同维度批量 ≤50） | `--dim appId --ids 1,2,3` |
| `extend` | 延期提审 | `--id 12345 --end-time 1735689600000` |
| `merge-edit` | 完整编辑提审 | `--prefix orientControl --file body.json` |
| `quick-push` | 立即推全 | `--prefix orientControl --id 12345 --type 0` |
| `withdraw` | 撤回 | `--id 12345` |
| `approve-query` | 审核单查询 | `--file query.json` |
| `approve-change` | 审核状态流转 | `--file change.json` |
| `list-user` | 用户列表 | （无参数） |

`get` 自动探测策略类型（orientControl → flowControl → darkControl → …），也可通过 `--prefix` 指定。`query` 支持 `--app-id`、`--uid`、`--pos-id`、`--status`、`--raw-body` 等筛选条件。

`search` 反查一个媒体/app ID 被哪些**屏蔽类**策略覆盖：orientControl 走服务端筛选（顶层 `appId`/`uid`/`posId` key），mediaControl 不支持筛选故全量拉取（约 356 条）后本地匹配 `mediaCluster`。返回每条策略的 id/名称/type/status/`shieldUserType`/行业 codes（`industryCodes`）/产品名（`products`）。行业 codes 用 `industry_lookup.py lookup --codes` 翻译成名称。一次只查一个维度；用户给了不同维度 ID 时分开调用。批量失败项可用 `--item <id>` 单项补跑。语义细节见 [references/shield-search.md](references/shield-search.md)。

## KwaiBI 脚本命令参考

| 命令 | 说明 |
|------|------|
| `dataset-detail --id 85587` | 字段定义（默认精简 id/name/type/urn；`--full` 看原始） |
| `query --data-set 85587 --fields '[...]' --filters '[...]' --start ... --end ...` | 结构化查数 |
| `query-alias --dim uid --ids 1,2 --group-by industry --days 7` | 按 ID 维度查行业/产品周期消耗 |
| `query-alias --data-set 129496 --dim app_id --ids 1,2 --group-by scene --days 1` | OLAP 模式查流量侧维度消耗 |

`query-alias` 内置 85587（离线）与 129496（实时）字段 id；`--dim` 支持 `uid`/`app_id`/`pos_id`（一次一个维度）。`industry`/`sub_industry`/`product` 仍使用原查询；新增的 `scene`/`coop`/`mtype`/`deliver`/`opclass` 使用 129496 的完整 OLAP 模板，返回按原始消耗 `r` 聚合、跳过 `$ALL$` 小计与 null/空分类后的降序行。时间窗用 `--days N` 或 `--start/--end`（epoch ms），单次最多 50 个 ID；失败项可用 `--item <id>` 单项补跑。

字段名/id 对应关系、高频模板、别名映射等详见 [tools-kwaibi.md](tools-kwaibi.md)。

## 策略→查数桥接工作流

1. `operation_client.py get --id xxx` → 取策略 `adCluster` / `mediaCluster`
2. 从策略字段提取维度值（uid、posId、appId、adStyle 等），对照 [tools-kwaibi.md §4.3](tools-kwaibi.md) 的桥接映射表
3. **注意字段名差异**：策略 `appId` → KwaiBI `联盟媒体ID`；策略 `posId` → KwaiBI `广告位`
4. `kwaibi_client.py dataset-detail --id <dataSetId>` → 取字段 id
5. 构造 `kwaibi_client.py query` 的 body（套 [tools-kwaibi.md §6](tools-kwaibi.md) 模板）
6. 按 columns + dataList 制表输出

## 批量能力评估

本 Skill 启用轻量顺序批量模式（只读查询）。

- **批量适用**：
  - `get --ids` 接受逗号分隔的多个策略 ID（只读查询）
  - `search --dim X --ids a,b,c` 同维度多 ID 反查屏蔽（一次一个维度，不同维度分开调用）
  - `query-alias --dim X --ids a,b,c` 同维度多 ID 查消耗（一次一个维度，不同维度分开调用）
  - `olap_client --dim X --ids a,b,c` 同维度多 ID 查实时效果（一次一个维度，不同维度分开调用）
- **执行模式**：`execution_mode=lightweight_sequential`，顺序查询，结果汇总
- **只读**：以上四条仅涉及 GET/POST 查询，无写操作或副作用
- **上限**：单次 `max_items=50`，超出返回 `BATCH_TOO_LARGE` 提示分批
- **单项重试**：`get --id <single_id>` 可独立补跑失败项；search/query-alias/olap_client 可用 `--item <id>` 单 ID 重跑
- **不启用批量**：merge-edit / quick-push / withdraw 等写操作每次只处理单条，不在批量范围

## 边界

- **数据路由**：当天/实时固定指标可走 129496 OLAP 快路径；历史/T-1、任意字段和异常兜底走 KwaiBI。策略信息始终来自 Operation。
- **只封装上游 API**：路径、方法、鉴权、请求/响应形状、已知坑。
- **不含**：效果监控流程、Data Agent NL 对话、Kim/定时推送。
- **Operation 与 KwaiBI Cookie 不同域**：两个脚本分别管理各自的 Session，不会混用。
- **行业字典是静态内置 + 可刷新**：`references/industry-code-name.json` 内置 353 个二级行业 code→名称映射，来自联盟策略平台 seed 字典；`industry_lookup.py refresh` 可从 seed 平台（10.0.0.1:3000，可能内网不可达）刷新，失败时用内置字典兜底，不阻塞主流程。
- **依赖**：需要 `ks-aimate` 包（通过内网 pypi 安装），脚本首次运行 `uv run` 自动处理。