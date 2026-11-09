---
name: holiday-coef-daily
description: 短剧假期系数策略时间窗更新：可自定义输入策略 ID、目标日期、开始时间和结束时间，更新 beginTime/endTime 后提交并审核。当用户说「假期系数日更」「改假期系数时间」「策略 ID 加日期时间」「叠时间窗」「跑 26558/26559」时使用。不支持通用策略延期、屏蔽策略创建、非短剧假期系数场景。
---

# 短剧假期系数日更

可按用户给定的策略 ID、目标日期、开始时间、结束时间更新策略时间窗：

1. `get` 指定策略，探测其策略模块
2. 用输入的日期与起止时间计算 `beginTime` / `endTime`（禁止手算 epoch-ms）
3. 只更新这两个时间字段后写回原策略，并按需流转审核

**不要改**：系数、账户/产品维、`adCluster` / `mediaCluster` 业务字段、策略名（除非用户明确要求）。

接口细节见 [references/API.md](references/API.md)；执行报错先查 [references/error-handling.md](references/error-handling.md)。鉴权优先走云端 Auth/OBO HTTP；禁止把 `sso-state.json` 当 Cookie。

<!-- skill-creator: askuser-with-text-fallback-v1 -->

## 用户交互：AskUser 优先与文本降级

凡需向用户收集信息、请求确认或要求选择时，优先调用 `askUser` 工具，并以结构化问题和可选项取得答复；关联问题应合并提问。

若 `askUser` 在当前运行环境不存在、调用失败或返回无效结果，才允许降级为普通聊天文本提问。降级时必须明确说明“当前 AskUser 不可用，以下为兼容提问”，并在收到用户明确答复前停止后续依赖该答复的操作；不得猜测、默认代填或把沉默视为同意。

---

## 输入参数

| 参数 | 必填 | 格式 | 示例 |
|------|------|------|------|
| 策略 ID | 是 | 整数 | `26558` |
| 目标日期 | 是 | `YYYY-MM-DD` | `2026-10-10` |
| 开始时间 | 是 | `HH:MM` 或 `HH:MM:SS` | `04:00:00` |
| 结束时间 | 是 | `HH:MM` 或 `HH:MM:SS` | `08:00:00` |

示例：「把策略 `26558` 改到 `2026-10-10 04:00:00` 至 `08:00:00`」。时间按 `Asia/Shanghai` 计算，开始和结束必须在同一天，且结束晚于开始。

---

## 运行模式

| 模式 | 写接口 | 何时用 | 默认 |
|------|--------|--------|------|
| **A 改原 ID** | `submitEdit`（定向）/ `submitEdit`（其它） | 更新用户指定策略的时间窗 | **是** |
| **B 每天新建** | `batchCreate`（定向） | 要保留历史窗口、每天出新 ID | 否（需用户明确） |

未指定模式 → 走 **A**。

---

## 触发

| 来源 | 行为 |
|------|------|
| 定时 Cron `0 0 * * *`（上海）或公司调度等价时刻 | 采用预先配置的策略 ID 与时间窗，立即执行 |
| 用户：「假期系数日更」「叠今天」「跑 26558」 | 缺参数时收集策略 ID、日期、开始/结束时间 |
| 用户：「把策略 26558 改到 2026-10-10 04:00 至 08:00」 | 按输入的完整参数执行 |

人工触发时先回一行确认参数再执行：

```text
假期系数：策略 ID=26558，日期=YYYY-MM-DD，开始=HH:MM:SS，结束=HH:MM:SS，模式=A
```

日期、开始时间、结束时间任缺一项时，优先用 AskUser 一次性收集；定时任务使用已有配置，不再二次确认。

---

## 执行流程（按步跑）

### 0. 收集参数并计算时间窗

收集策略 ID、目标日期、开始时间、结束时间。用脚本计算，**禁止脑中手算 epoch-ms**：

```bash
uv run <skill_directory>/scripts/overlay_time.py --strategy-id 26558 --day 2026-10-10 --start-time 04:00:00 --end-time 08:00:00
```

脚本输出 `data.beginTime` / `data.endTime`（epoch-ms）。时区固定 `Asia/Shanghai`（UTC+8）；开始和结束必须同日，且结束晚于开始。

### 1. 鉴权

用云端内置带 Auth/OBO 的 HTTP 工具打 Operation。失败（302/401/HTML login）→ **停**，报告「未登录 / 鉴权失败」，不要空转重试、不要改用 Data Agent。

### 2. `get` 指定策略

```
GET /{apiPrefix}/get?id={strategyId}
```

- 策略模块探测顺序与空壳校验见 [references/API.md](references/API.md)
- 记录 `_apiPrefix`、`type`、完整 `data`
- 未命中或失败 → 停止，报告策略 ID 和失败原因

### 3. 叠时间（本地）

对成功 get 的策略详情，用第 0 步算出的窗口**覆盖 `beginTime` / `endTime` 仅这两项**，其余字段从 get 原样带入后再做 sanitize（见 API 白名单）。**禁止**手搓空 `adCluster` / `mediaCluster`。

### 4. 写策略

**模式 A：**

```
POST /targetingControl/submitEdit   # 若 apiPrefix=targetingControl
POST /{apiPrefix}/submitEdit       # 其它前缀
```

**模式 B：**

```
POST /targetingControl/batchCreate
```

- Body 去掉原策略 `id`（新建）；时间已叠好；业务字段从原策略拷贝
- 成功则记下新 ID，供后续审核

成功判定：HTTP 2xx 且业务 `status == 200`（或平台约定成功码）。412 → 按 [references/API.md](references/API.md) 收紧字段后**同条最多再试 1 次**；仍失败则记失败，不改模式硬切。

### 5. 审核 / 发布（尽量赶在 04:00 前）

提交成功后：

1. `POST /approve/query` 按策略 id（模式 B 用新 id）找待审单
2. `POST /approve/changeStatus`：普通流常见 `1→6`（审核成功）再 `6→2`（发布成功）
3. 若工作台/白名单已自动审，本步可跳过，但仍应用 `get` 确认运行态

封禁期码（12/13）按平台文案走，不要和策略运行态混用。

### 6. 可选立即推全

仅当需要窗口一到就生效，且 `get` 显示可推全（如 `displayQuickPushBtn` / 灰度态）：

```
POST /{apiPrefix}/quickPushAll?id={id}&type=0
```

- 受理成功 ≠ 已生效；可短轮询 `get` 至生效/失败/超时
- 默认定时任务：**不推全**，除非配置或用户明确要求

### 7. 汇报

每条一行，整轮汇总：

```text
[假期系数] 策略 ID=... 日期=YYYY-MM-DD 模式=A
策略 ID OK begin=... end=... approve=...
或：策略 ID FAIL get空壳 / 412 ...
```

有失败 → 告警（Kim/调度失败通道按宿主配置）；**不要**静默吞掉。

---

## 批量能力评估

本技能**不支持批量处理**：一次只更新一条用户指定的策略。若需要改多条策略，必须逐条输入并逐条确认；不提供多日期、多账户或多策略的一键批量写入。

---

## 硬约束

1. **只改 beginTime / endTime**；严格使用用户确认的日期与具体时间
2. **不要改系数与定向/媒体名单**
3. 定向必须 `submitEdit`；非定向 `submitEdit`
4. 若设置的窗口临近开始时间，要在汇报里标明审核可能来不及生效的风险
5. 不把本流程当成通用延期/屏蔽创建；仅用于短剧假期系数策略的时间窗更新

---

## 失败策略

| 情况 | 处理 |
|------|------|
| 鉴权失败 | 整轮中止 + 告警 |
| 单条 get 空壳/失败 | 跳过该条，继续 |
| 412 | 收紧白名单重试 1 次，仍败则记失败 |
| 审核流转被拒但已灰度 | 可旁路 `quickPushAll`（仅开启推全时）；否则记「待人工」 |
| 部分成功 | 汇报成功/失败明细，不算整轮成功 |

错误码 → 动作的完整映射见 [references/error-handling.md](references/error-handling.md)。

---

## 与其它技能边界

| 技能 | 关系 |
|------|------|
| `operation-bi` | 上游 API / 鉴权契约；本技能是业务编排 |
| `strategy-postpone` | 到期 +1 月改 `endTime`；**不要**用延期逻辑改假期窗 |
| 白名单自动审 | 可选加速；本技能不替代审核状态机 |
