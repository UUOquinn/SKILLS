---
name: flow-strategy-ops
description: >-
  操作快手内网策略运营平台 ops-platform.example.corp/flow 的扶持策略管理
  （flowControl）API，支持单条查询、新建、修改并提审、撤回、删除、立即推全、导出、
  状态时间线和版本 diff；在用户指定策略 ID 后，可在广告主信息/媒体信息模板上新增或
  删除名单 ID（账户/媒体应用/媒体用户/广告位等维度）。用户提到扶持策略、flowControl、
  扶持策略管理、新增或修改扶持策略、给策略加 ID/删 ID、提交审核、撤回、删除、推全时触发。
  仅通过当前用户 SSO 身份直连 REST API。
  不支持网页 UI 操作、浏览器自动化、Cookie 配置及批量写入；非扶持策略场景不触发。
---

# 扶持策略管理（flowControl）

通过 `SmartSSOSession` 复用并自动刷新当前用户 SSO，直接调用 Operation 的 REST API。
执行路径只有一条：**SSO → API**。禁止打开 Operation 页面、模拟点击、复制 Cookie，或在 API 失败后降级到 UI。基址：`https://ops-platform.example.corp/ops/rest`

<!-- skill-creator: askuser-with-text-fallback-v1 -->

## 用户交互：AskUser 优先与文本降级

凡需向用户收集信息、请求确认或要求选择时，优先调用 `askUser` 工具，并以结构化问题和可选项取得答复；关联问题应合并提问。

若 `askUser` 在当前运行环境不存在、调用失败或返回无效结果，才允许降级为普通聊天文本提问。降级时必须明确说明“当前 AskUser 不可用，以下为兼容提问”，并在收到用户明确答复前停止后续依赖该答复的操作；不得猜测、默认代填或把沉默视为同意。

## 执行入口

```bash
uv run <skill_directory>/scripts/flow_api.py <command> [arguments]
```

脚本 stdout 最后一行固定为结构化 JSON；判断成功必须同时满足进程退出码为 `0` 且
JSON 中 `ok=true/code=0`。失败时先读
[references/error-handling.md](references/error-handling.md)，按错误码处理。

## 快速创建：四要素直接建

用户给出「顶价方式 / 名单维度及 ID / 系数 / 时间」，即视为**已明确授权创建**，
无需再反问确认，直接把四要素映射成一条 create 命令。完整映射表见
[references/quick-create.md](references/quick-create.md)。

硬约束（踩坑固化）：

- **不要带空的 cluster 对象**：scope=2 只传 mediaCluster，scope=1 只传 adCluster，
  scope=3 才两个都传；多传空对象会报「生效范围不能为空」。
- 名称默认按 `媒体顶价<系数>-AI创建-<ID>` 拼；冲突（名称已存在）时追加 -2/-3 重试。
- 相对时间必须用 `date -d` 计算（月=30天、周=7天、日=24小时）。
- 创建后告知策略 ID 与「待提交」状态，**不自动提审**；是否提审由用户决定。

## 常用命令

```bash
# 单条详情（全量，data.detail 为原始详情，顶层含 writable/statusDesc/可读时间）
uv run <skill_directory>/scripts/flow_api.py get 12345

# 单条详情（关键字段摘要：状态信号、可读时间、各维度名单，聊天场景推荐）
uv run <skill_directory>/scripts/flow_api.py get 12345 --brief

# 分页查询；脚本自动补 industryVersion=6.6
# 返回行已收敛为关键字段摘要（含 statusDesc、writable、beginTimeText/endTimeText）
uv run <skill_directory>/scripts/flow_api.py query \
  '{"pager":{"pageNum":1,"pageSize":20},"status":4}'

# 只记得策略名不记得 ID：先按名称模糊查询，确认 ID 后再操作（见「名称→ID」流程）
uv run <skill_directory>/scripts/flow_api.py query \
  '{"pager":{"pageNum":1,"pageSize":20},"name":"短剧明投"}'

# 单条新建（最小可跑示例：type=3 必填 accountId、placements、bonusRate）
# 服务端要求 list 包装，脚本内部自动包装为单元素列表
uv run <skill_directory>/scripts/flow_api.py create \
  '{"name":"示例策略","type":3,"scope":1,"beginTime":1780000000000,"endTime":1788300000000,"bonusRate":1.0,"strategyDetails":"1.0","adCluster":{"industryVersion":"6.6","accountId":"1001","placements":1,"placementsArr":[1]},"mediaCluster":{},"background":"示例"}'

# 单条修改并提审：先 GET 全量，再合并 patch、清理只读字段后 mergeEdit
uv run <skill_directory>/scripts/flow_api.py edit 12345 --set \
  '{"endTime":1788300000000,"background":"调整结束时间"}'

# 在指定策略的维度模板上新增/删除 ID 并提审
# 必须由用户先给出明确的策略 ID（dim 为广告主信息/媒体信息模板里的名单字段）
uv run <skill_directory>/scripts/flow_api.py add-ids 12345 --dim accountId --ids 1001,1002
uv run <skill_directory>/scripts/flow_api.py add-ids 12345 --dim appId --ids 2001,2002
uv run <skill_directory>/scripts/flow_api.py remove-ids 12345 --dim posId --ids 3001

# 状态流转：change-status 只支持人工流转
#   - 0 待提交 → 1 提审
#   - 1 待审核 → 0 撤回
# 审核通过/发布/生效由平台审核流程驱动，无法用 API 硬跳（传 6/4 会报 VALIDATION_FAILED）
uv run <skill_directory>/scripts/flow_api.py change-status 12345 1
uv run <skill_directory>/scripts/flow_api.py change-status 12345 0

# 删除、时间线、版本 diff
uv run <skill_directory>/scripts/flow_api.py delete 12345
uv run <skill_directory>/scripts/flow_api.py log 12345
uv run <skill_directory>/scripts/flow_api.py diff 12345

# 立即推全：--type 0普通推全（默认）/1提交封禁期审批/2封禁期立即推全
uv run <skill_directory>/scripts/flow_api.py quick-push 12345 --type 0
```

> 导出说明：平台 export 接口只返回受理回执（JSON 壳）；真实导出路径是 query 取数据后
> 本地生成 xlsx（见「常用命令」中 export 用法）。没有符合条件的数据时返回 `EXPORT_FAILED`，不落盘。

## 名称→ID 定位流程

用户常常只报策略名（「给短剧明投1.3加两个账户」）而不报 ID。此时按以下流程走，**禁止猜测 ID**：

1. `query '{"pager":{"pageNum":1,"pageSize":20},"name":"<用户说的名称>"}'` 模糊匹配
2. 命中多条时，把候选的 id/name/statusDesc/creatorName 列给用户，用 askUser 让用户选
3. 用户确认唯一策略 ID 后，再执行 add-ids / edit / 名单操作

## 状态信号

`get`（非 brief）顶层和 `query` 每行都带 `writable` 字段：策略已删除（isDel=true）
或已失效（status=5）时 writable=false，写操作会被 NOT_WRITABLE 拦截。
时间字段同时返回原始毫秒值和 `beginTimeText` / `endTimeText`（本地可读格式）。

**注意**：`writable=true` 不代表当前就能改。策略提审后进入「待审核（status=1）」，
此时服务端拒绝任何编辑/名单修改（「审核流程中不能更新」），脚本会用 NOT_WRITABLE 提前拦截并提示
「先等审核结果，或先撤回再改」。

## 状态机与人工流转边界

线上状态枚举（query 实测）：0待提交 / 1待审核 / 2发布成功 / 4生效中 / 5已失效 / 6审核通过。

change-status 只能执行**人工流转**：
- `0 → 1`：提审
- `1 → 0`：撤回

「审核通过 → 发布成功 → 生效中」由平台审核/发布流程驱动，API 无法硬跳
（传 6/4 等目标状态会报 VALIDATION_FAILED「状态不合法」）。

## 名单增删（先指定策略 ID，再操作）

新增/删除名单是写操作，**前提是用户已经指定了扶持策略 ID**：命令的第一个位置参数就是策略 ID，
缺少 ID 时脚本直接拒绝执行，不猜测、不按名称模糊匹配。名单维度必须是
[API.md](API.md) 中「名单维度表」列出的字段，ID 支持逗号/分号分隔、可一次多个，但始终针对同一策略，
不是批量写多条策略。广告主信息模板对应 `adCluster`，媒体信息模板对应 `mediaCluster`，
两者均有 id 增删能力：

```bash
# 广告主信息模板：新增账户 ID / 产品 / 二级行业 / 推广目的 / 优化目标
uv run <skill_directory>/scripts/flow_api.py add-ids <策略ID> --dim accountId --ids 1001,1002
uv run <skill_directory>/scripts/flow_api.py remove-ids <策略ID> --dim product --ids 短剧

# 媒体信息模板：新增/删除媒体应用 ID、媒体用户 ID、广告位 ID、广告样式
uv run <skill_directory>/scripts/flow_api.py add-ids <策略ID> --dim appId --ids 2001,2002
uv run <skill_directory>/scripts/flow_api.py remove-ids <策略ID> --dim posId --ids 3001
```

增删语义是幂等集合操作：新增时已存在的 ID 会跳过（返回 `ALREADY_PRESENT`），
删除时不存在的 ID 也会跳过（`ALREADY_ABSENT`），两种情况都不提交线上。
数字维度（accountId/appId/uid/posId）会校验 ID 必须是数字，防止把账户名等写进 ID 字段；
只有确实发生变化才走 mergeEdit 提审，执行一次、不自动重试。
已删除或已失效的策略不能作为写操作对象，会返回 `NOT_WRITABLE` 拦截。

## 写操作确认

查询、详情、时间线、diff 属于只读操作，可直接执行。
新建、修改并提审、名单增删、状态流转、删除、立即推全会改变线上状态：如果用户尚未明确授权具体动作，
先按上方 AskUser 契约确认；名单增删还要求用户先给出策略 ID；确认后只执行一次，不自动重试写请求。

**快速创建例外**：用户给出「顶价方式/名单维度及ID/系数/时间」四要素时（见「快速创建」章节），
四要素齐即视为明确创建授权，直接建、不再反问；创建停在「待提交」状态，不自动提审。

## 单条能力边界

本 Skill **不支持批量写操作**。一次命令只允许新建、修改、删除或推全一个策略；
`create` 若收到顶层 `list` 会返回 `BATCH_NOT_SUPPORTED`。列表查询和导出是读取能力，
不等于对多条策略执行写操作。

## API 清单

| Method | Path | CLI |
|---|---|---|
| GET | `/flowControl/get` | `get` |
| POST | `/flowControl/query` | `query` |
| POST | `/flowControl/export` | `export` |
| POST | `/flowControl/mergeEdit` | `edit`、`add-ids`、`remove-ids` |
| POST | `/flowControl/batchAddV2` | `create`，仅单元素 list |
| POST | `/flowControl/changeStatus` | `change-status` |
| DELETE | `/flowControl/delete` | `delete`，仅单 ID |
| GET | `/flowControl/log` | `log` |
| POST | `/flowControl/showDiffInfo` | `diff` |
| POST | `/flowControl/quickPushAll` | `quick-push` |

字段、scope 和请求体契约见 [API.md](API.md)。

## API-only 失败原则

- `SSO_EXPIRED`：停止并要求用户重新完成内网认证；不打开浏览器代操作。
- `AUTH_FAILED`：停止并告知当前账号无接口权限；不借用他人 Cookie。
- `VALIDATION_FAILED`：服务端业务校验失败（如缺必填字段、名单冲突），
  错误信息里就是真实原因；对照 API.md 补字段后重试。**不要**去排查行业版本。
- `INDUSTRY_VERSION_MISMATCH`：错误信息明确提到行业版本时才是这个错；核对请求中的
  `industryVersion` 后重试；不转 UI。
- `API_TIMEOUT`：只读请求最多重试一次；写请求不自动重试，避免重复提交。
- 所有未知错误均停止并展示结构化错误，不模拟页面点击。
