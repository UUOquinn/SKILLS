---
name: ssp-operation-query
description: 查询快手联盟 SSP 运营平台中的开发者、应用、广告位、内容位、流量包、结算、报表、日志和申诉记录，支持按多个查询任务批量获取结果。当用户说“查 SSP 运营平台”“查询开发者或应用”“查广告位/结算/操作日志”“批量查询 SSP”时使用。不支持新增、修改、审核、删除、导出、解密、推送或任何会改变平台数据的操作；不替业务预设筛选口径、阈值或决策规则。
---

# SSP 运营平台查询

本技能仅调用经过白名单限定的只读接口。用户需提供查询主题与筛选条件；若字段名、请求体或分页规则没有在接口文档中定义，应先收集一条实际请求样例，不得猜测参数。

<!-- skill-creator: askuser-with-text-fallback-v1 -->

## 用户交互：AskUser 优先与文本降级

凡需向用户收集信息、请求确认或要求选择时，优先调用 `askUser` 工具，并以结构化问题和可选项取得答复；关联问题应合并提问。

若 `askUser` 在当前运行环境不存在、调用失败或返回无效结果，才允许降级为普通聊天文本提问。降级时必须明确说明“当前 AskUser 不可用，以下为兼容提问”，并在收到用户明确答复前停止后续依赖该答复的操作；不得猜测、默认代填或把沉默视为同意。

## 支持范围

可查询的主题包括：开发者与账号、邀请码、企业入驻、合同、应用、广告位、内容位、流量包、行业资质、媒体广告位映射、结算与结算查询、日分成、媒体包、宏替换、操作日志与申诉记录。

查询前先阅读 <skill_directory>/references/query-catalog.md，选择对应的只读接口；不要把路径名称相近的写接口混入请求。

## 单次或批量查询

批量能力评估结论：`execution_mode=lightweight_sequential`。本技能仅做只读查询，采用串行模式以适配接口限流；`max_items=50`，每个任务可独立补跑一次。

调用脚本时以 JSON 传入一个或多个任务。每个任务必须指定：

- `endpoint`：目录中列出的只读接口标识，例如 `position.search`；
- `params`：GET 接口的查询参数；或 `body`：POST 接口的 JSON 请求体；
- 可选 `label`：用于汇总结果时标识该任务。

```bash
uv run <skill_directory>/scripts/query_ssp_operation.py \
  --tasks-json '[{"label":"广告位查询","endpoint":"position.search","body":{"posId":123}}]'
```

批量查询直接传入多个任务。脚本逐项串行请求，返回每项的状态和原始响应；单项失败不会阻断后续任务。

```bash
uv run <skill_directory>/scripts/query_ssp_operation.py \
  --tasks-json '[{"label":"应用","endpoint":"app.search","body":{"appId":1}},{"label":"广告位","endpoint":"position.search","body":{"posId":2}}]'
```

若某一项失败，使用同一个任务 JSON 作为单项补跑入口；该任务最多自动重试一次超时请求：

```bash
uv run <skill_directory>/scripts/query_ssp_operation.py \
  --item-json '{"label":"广告位补跑","endpoint":"position.search","body":{"posId":2}}'
```

## 约束

- 不得用本技能调用目录外路径，也不得传入完整 URL。
- 批量任务上限为 50 条，避免对平台造成无控制的压力。
- 业务接口字段、分页与响应结构以实际前端请求为准；当前文档只提供路径清单，不足以推导字段语义。
- 脚本报错时，先阅读 <skill_directory>/references/error-handling.md，并按其中的错误码与 `next_action` 处理。
