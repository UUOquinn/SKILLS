---
name: kconf-generic-api
description: 封装快手 KConf 配置中心（kconf.example.corp）的通用 REST API：找配置（目录浏览/关键词搜索）、任意 key 的配置详情读取、历史记录、使用监控、写配置（直改/Review）、分级发布推进/取消、回滚、元信息与权限收藏管理。支持对多个 key 批量执行同类操作（批量查询、批量加白、批量推进灰度），写操作带幂等去重与写后回查。用户说「查 kconf 配置」「改 kconf 配置」「找 xx 配置」「kconf 灰度推进/回滚」「批量给多个 kconf key 加白」「查 kconf 历史/谁在用」时触发。不支持单个具体业务 key 的语义封装（如联盟短剧暗投配置的专用流程，见 `kconf-universe-add-chain-gray`）；不做非 KConf 的配置系统操作；不支持网页 UI 或浏览器自动化。
---

# KConf 通用配置中心 API

面向任意 KConf key 的通用读写封装，按**真实使用场景**组织：找配置 → 查配置 → 改配置 → 灰度/回滚 → 历史监控 → 管理。不绑定具体业务语义；业务专属流程（如联盟短剧暗投）用专用 Skill。

<!-- skill-creator: askuser-with-text-fallback-v1 -->

## 用户交互：AskUser 优先与文本降级

凡需向用户收集信息、请求确认或要求选择时，优先调用 `askUser` 工具，并以结构化问题和可选项取得答复；关联问题应合并提问。

若 `askUser` 在当前运行环境不存在、调用失败或返回无效结果，才允许降级为普通聊天文本提问。降级时必须明确说明"当前 AskUser 不可用，以下为兼容提问"，并在收到用户明确答复前停止后续依赖该答复的操作；不得猜测、默认代填或把沉默视为同意。

## 前置依赖

本 Skill 依赖 `batch-task-orchestrator`。运行前确认：

```bash
uv run <skill_directory>/scripts/check_install_dep.py ensure batch-task-orchestrator
```

## 鉴权

内网 SSO 自动处理（脚本内嵌 `SmartSSOSession`）。**禁止**在任何地方粘贴完整 Cookie。

## 场景一：找配置（目录浏览 / 搜索）

用户只知道业务名或关键词、不知道完整 key 时，按此链路定位：

```bash
# 一级业务目录名列表（根）
uv run <skill_directory>/scripts/kconf_client.py dir-path

# 指定目录下的子 path 名 / 目录详情（分页，带描述）
uv run <skill_directory>/scripts/kconf_client.py dir-list --dir ad [--page 1] [--size 20]
uv run <skill_directory>/scripts/kconf_client.py dir-get --dir ad

# 目录路径片段模糊搜索（只搜一级加 --dir-search-first）
uv run <skill_directory>/scripts/kconf_client.py dir-search --path adtarget
uv run <skill_directory>/scripts/kconf_client.py dir-search-first --path ad

# 目录下配置列表（liteList 轻量推荐；list 带全量 subConfigs 较重；list-keys 只要 key）
uv run <skill_directory>/scripts/kconf_client.py config-lite-list --dir ad.adtarget3 [--page 1] [--size 20]
uv run <skill_directory>/scripts/kconf_client.py config-list --dir ad.adtarget3
uv run <skill_directory>/scripts/kconf_client.py config-list-keys --dir ad.adtarget3

# 直接搜配置 key 片段（V2 搜索，返回 key/描述/类型/创建人；旧搜索兜底无描述）
uv run <skill_directory>/scripts/kconf_client.py search-config --keyword universeAddChainGrayConfig [--size 20]
uv run <skill_directory>/scripts/kconf_client.py search-config-legacy --keyword universeAddChainGrayConfig
```

注意：目录接口只走旧前缀 `/api/dir/*`（`/api/w/dir/*` 实测 404）；搜索接口频控 `result=104` 时退避重试。找到完整 key 后转入场景二。

## 场景二：查配置（只读）

```bash
# 核心：meta + 各环境 data + 灰度步骤 + workflow；--stage 只看某环境
uv run <skill_directory>/scripts/kconf_client.py get --key <kconf_key> [--stage production]

# 变更前检查：管控期 / 能否直改 / 提交前校验 / 使用方 P0P1 / 稳定性巡检
uv run <skill_directory>/scripts/kconf_client.py maintenance --key <kconf_key>
uv run <skill_directory>/scripts/kconf_client.py direct-change-check --key <kconf_key>   # update 内置调用，一般不需手动
uv run <skill_directory>/scripts/kconf_client.py precheck --key <kconf_key> [--stage production]   # 无副作用提交前校验
uv run <skill_directory>/scripts/kconf_client.py service-level-count --key <kconf_key>
uv run <skill_directory>/scripts/kconf_client.py stability-result --inspection-task-id <id> --key <kconf_key> [--env PROD]

# 旧链路 / Schema（页面仍会打一份旧链路）
uv run <skill_directory>/scripts/kconf_client.py get-legacy --key <kconf_key>
uv run <skill_directory>/scripts/kconf_client.py schema-get --key <kconf_key>
```

`get` 返回的 `subConfigs[].curConfig.data` 是带 `//` 注释的 JSON 字符串，读取时必须先剥注释再解析（脚本已处理）。

## 场景三：改配置 / 加白（写，重点）

改配置前**禁止手搓空 content**：先 `get` 读现有 data → 本地改 JSON → 再提交。`update` 内置完整链路：取 snapshotId/version → directChangeCheck → precheck(fullCheck) → 可直改走 `subconfig/update`，否则走 `review/submit` → 写后自动回查生效。

```bash
# 一条龙改配置（自动选择直改或 Review）
uv run <skill_directory>/scripts/kconf_client.py update --key <kconf_key> --stage production \
  --content-file <local_json_file> --comment "<变更说明>" [--emergency]

# 强制走 Review（带审批人，逗号分隔）
uv run <skill_directory>/scripts/kconf_client.py review-submit --key <kconf_key> --stage production \
  --content-file <local_json_file> --comment "<变更说明>" --assignees "<user1,user2>"

# 可选审批人（先拉再填 assignees）
uv run <skill_directory>/scripts/kconf_client.py get-reviewers --key <kconf_key>

# Review 细分：启动审批流 / 灰度推进走 Review / 删除走 Review（body 不确定时用 --body-file 传完整 JSON）
uv run <skill_directory>/scripts/kconf_client.py review-start-flow --key <kconf_key> --stage production
uv run <skill_directory>/scripts/kconf_client.py review-gray-next-submit --key <kconf_key> --stage production --comment "<说明>" --assignees "<u1,u2>"
uv run <skill_directory>/scripts/kconf_client.py review-delete-submit --key <kconf_key> --stage production --comment "<说明>" --assignees "<u1,u2>"

# 尾号类配置（json 类型一般不用）
uv run <skill_directory>/scripts/kconf_client.py tailnumber-update --key <kconf_key> --stage production --content-file <json> --comment "<说明>"
```

加白名单的完整做法：`get` → 剥注释解析 → 修改目标 `ab_key` 的 `account_white_list` / `product_name_white_list` / `second_industry_id_white_list`（只追加去重）→ 写回本地文件 → `update`。

## 场景四：分级发布 / 回滚

```bash
# 推至下一步 / 取消分级发布
uv run <skill_directory>/scripts/kconf_client.py gray-next --key <kconf_key> --stage production
uv run <skill_directory>/scripts/kconf_client.py gray-cancel --key <kconf_key> --stage production

# 回滚到历史版本（旧链路；先 history-detail 确认目标版本内容）
uv run <skill_directory>/scripts/kconf_client.py rollback --key <kconf_key> --version <target_version>
```

## 场景五：历史与监控

```bash
uv run <skill_directory>/scripts/kconf_client.py history-list --key <kconf_key> [--page 1] [--size 20]
uv run <skill_directory>/scripts/kconf_client.py history-detail --key <kconf_key> --version <version>
uv run <skill_directory>/scripts/kconf_client.py history-version --key <kconf_key>     # 最新 version（自动解析 changeId）
uv run <skill_directory>/scripts/kconf_client.py history-change-env --key <kconf_key>  # 环境变化过滤项
uv run <skill_directory>/scripts/kconf_client.py snapshot-get --id <snapshot_id>

uv run <skill_directory>/scripts/kconf_client.py monitor-ksn --key <kconf_key>         # 订阅服务 KSN / P0~P4
uv run <skill_directory>/scripts/kconf_client.py monitor-machine --key <kconf_key>     # 订阅机器
```

## 场景六：管理（元信息 / 权限 / 收藏）

```bash
# 管理态（描述/强制 Review/订阅人）
uv run <skill_directory>/scripts/kconf_client.py get-manage-data --key <kconf_key>
uv run <skill_directory>/scripts/kconf_client.py meta-update --key <kconf_key> --meta-file <json>  # 改描述等，非 content

uv run <skill_directory>/scripts/kconf_client.py permission-apply --key <kconf_key> [--reason "<申请原因>"]
uv run <skill_directory>/scripts/kconf_client.py favorite-add --key <kconf_key>
uv run <skill_directory>/scripts/kconf_client.py favorite-remove --key <kconf_key>

# 页面壳（无 key）：登录态 / 收藏 / 最近访问 / 最近修改 / 待办 / 提示条
uv run <skill_directory>/scripts/kconf_client.py islogin
uv run <skill_directory>/scripts/kconf_client.py favorite-list
uv run <skill_directory>/scripts/kconf_client.py visited-list
uv run <skill_directory>/scripts/kconf_client.py last-modified
uv run <skill_directory>/scripts/kconf_client.py pending-count
uv run <skill_directory>/scripts/kconf_client.py alertbox
```

## 批量能力评估

创建阶段完成批量能力评估，结论：**启用批量（编排模式 enable_orchestrated）**——存在写副作用、需跨多项汇总、要求幂等去重与写后回查。

- **Normal 模式**：单个 key 直接调用 `kconf_client.py`（exec 执行）
- **Batch 模式**：多个 key 统一走 `kconf_batch.py run`，两者共享 `process_one(key, action, params)` 契约；`--item <key>` 可单项补跑

## 批量操作（Orchestrated 模式）

> ⚠️ **执行路由**：`kconf_batch.py` 是长耗时任务（整批上限 10 分钟），**必须用 `call_remote_agent`（method=CALLBACK）执行**，禁止直接 `exec`。脚本内置保护：非 call_remote_agent 启动立即返回 `REMOTE_AGENT_REQUIRED`。单 key 命令是秒级短任务，用 `exec`。

```bash
uv run <skill_directory>/scripts/kconf_batch.py run \
  --action <get|update|gray-next|gray-cancel|rollback> \
  --keys-file <local_file_一行一个key或JSON数组> \
  --params-file <local_json_每个key对应的操作参数> \
  --concurrency 2 --batch-size 50 --max-runtime 600 --authoritative-source kconf-get

# 单项补跑 / 幂等回查
uv run <skill_directory>/scripts/kconf_batch.py run --action update --item <kconf_key> --keys-file <f> --params-file <json>
uv run <skill_directory>/scripts/kconf_batch.py check-action --key <kconf_key> --action <action> --params-file <json>
```

批量契约（用户确认固化，不可运行时更改）：

- **并发度固定 2**（`--concurrency` 被钳制）；**单批最多 50 key**（`--batch-size`）；**整批最长 10 分钟**（`--max-runtime`），超时直接失败，不做检查点续跑
- **写后回查权威数据源**（`--authoritative-source`，默认 kconf-get）：重新 `GET /api/w/config/get` 确认生效
- **敏感操作**（`update`、`review-submit`、`gray-next`、`gray-cancel`、`rollback`）：派生稳定 `action_id`（key+操作+内容摘要 sha256），写前 `--check-action` 防重复提交，写后自动回查
- **只读操作**（`get`、`history-list`、`history-detail`、`monitor-ksn`、`monitor-machine`）无需 action_id 保护，直接并发
- 结果按输入 key 顺序汇总：`key`、`status`、`action_id`（敏感操作）、错误原因

## 脚本报错处理

遇到任何脚本报错，先读 `references/error-handling.md` 按错误码表处理，禁止自由发挥。
