---
name: configservice-universe-add-chain-gray
description: 管理联盟短剧暗投配置，按“Data Agent 取数、ConfigService 新链路灰度、CPM 顶价策略查询”顺序提供只读诊断与批量灰度变更。当用户说“短剧暗投配置”“短剧流量效果”“添加新链路灰度”“批量加白”或“CPM顶价策略”时触发。不支持其他 ConfigService Key、非短剧数据集、自动调价，或未经确认的生产变更。
---

# 联盟短剧暗投配置

目标链路固定为 **Data Agent → ConfigService → boostControl**：先读取「短剧-流量-效果数据」，再读取 `ad.demo.chainGrayConfig`，最后查询扶持策略管理中规则名称精确等于「CPM顶价」的策略。前两段只读；ConfigService 写入保留原有预览、确认、回查机制。FlowControl 不提供新建、修改、提审或推全。

<!-- skill-creator: askuser-with-text-fallback-v1 -->

## 用户交互：AskUser 优先与文本降级

凡需向用户收集信息、请求确认或要求选择时，优先调用 `askUser` 工具，并以结构化问题和可选项取得答复；关联问题应合并提问。

若 `askUser` 在当前运行环境不存在、调用失败或返回无效结果，才允许降级为普通聊天文本提问。降级时必须明确说明“当前 AskUser 不可用，以下为兼容提问”，并在收到用户明确答复前停止后续依赖该答复的操作；不得猜测、默认代填或把沉默视为同意。

## 短剧暗投只读流程

首次使用前，向用户收集 Data Agent 的 `agent-id`；不可臆测。随后按固定顺序执行：

```bash
uv run <skill_directory>/scripts/short_drama_workflow.py run --agent-id <agent_id> --question "按账户统计今日消耗、ROI 与曝光" --stage production
```

也可独立定位单段问题：

```bash
uv run <skill_directory>/scripts/short_drama_workflow.py data --agent-id <agent_id> --question "查询最近一小时账户消耗和ROI"
uv run <skill_directory>/scripts/short_drama_workflow.py configservice --stage production
uv run <skill_directory>/scripts/short_drama_workflow.py cpm --status 4
```

执行前或登录态报错后，可用 `sso-check` 逐域确认 Data Agent（tc）、ConfigService、boostControl（ops-platform）三个域的 Auth 登录态；任一域失效会返回 `AUTH_EXPIRED`（退出码 2）并附逐域状态：

```bash
uv run <skill_directory>/scripts/short_drama_workflow.py sso-check
```

只需确认 ConfigService 单个域时，直接用 `manage_config.py` 的同名子命令（两者共享同一份识别逻辑）：

```bash
uv run <skill_directory>/scripts/manage_config.py sso-check
```

`run` 的输出按 `data_agent`、`configservice`、`cpm_topbid_strategies` 三段返回。Data Agent 请求固定携带数据集名称「短剧-流量-效果数据」；策略查询固定使用 `boostControl/query` 并在本地精确过滤 `name=CPM顶价`。

## ConfigService 灰度写入

写入仅作用于 `ad.demo.chainGrayConfig`。先预览，再展示环境、版本、命中项、实际新增值和 `action_id`；获得用户明确确认后才可提交：

```bash
uv run <skill_directory>/scripts/manage_config.py preview --stage production --rules-file <workspace>/rules.json
uv run <skill_directory>/scripts/manage_config.py apply --stage production --rules-file <workspace>/rules.json --comment "短剧暗投批量加白" --action-id <action_id> --concurrency 1 --batch-size 10 --max-runtime 300 --authoritative-source configservice --confirm
```

规则文件为数组；每项必须含 `ab_key`，并可包含 `account_white_list`、`product_name_white_list`、`second_industry_id_white_list`。单次最多 50 条；只追加并去重，不删除既有值。写入后必须 `check-action` 回查；失败时不自动重试写操作。

## 批量与幂等边界

批量能力评估结论：已启用编排模式（`execution_mode=orchestrated`）。

已启用编排模式：Data Agent 取数、ConfigService 读取、CPM 顶价策略读取严格串行，避免用未核验的效果数据驱动后续判断。ConfigService 批量写入采用单配置提交；稳定 `action_id` 由环境与规则生成，`check-action` 从 ConfigService 权威配置回查。单项补跑使用 `--item <ab_key>`。批量执行前需提供真实但可脱敏的对象清单与权威结果样本，完成正常、续接、失败、幂等四类回测后才允许发布。

## 异常处理

脚本报错时先读取 `<skill_directory>/references/error-handling.md`，依据退出码和最后一行 JSON 的 `error`、`next_action` 处理。返回 `AUTH_EXPIRED` 表示 Auth 登录态失效或等待扫码，完成内网登录后重试，必要时先跑 `sso-check` 逐域确认。不得手工复制 Cookie、绕过确认直接写生产，或改用网页操作。

<!-- batch-runtime-contract: orchestrated -->
