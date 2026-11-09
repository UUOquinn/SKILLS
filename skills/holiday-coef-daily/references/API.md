# 假期系数日更 — Operation API

基址：
```
https://ops-platform.example.corp/ops/rest
```

鉴权：优先云端 Agent **内置 SSO/OBO HTTP**；其次已登录浏览器同源 `fetch`；最后才是手工完整 `Cookie:<REDACTED> 行（须含 `accessproxy_session`）。
**禁止**把 `sso-state.json` 当 Cookie；**禁止** SSO 302 后当成功；**禁止**用 Data Agent / 番薯 NL 代替本文件接口。
成功壳常见 `status: 200`；**不能**只看 HTTP / 外层 status，见「空壳校验」。
本文件只覆盖假期系数日更用到的接口；通用 Orient 契约可对照 `operation-kwaibi` 技能（同类 Orient 契约）。

---

## 0. 策略类型前缀

| apiPrefix | 类型 | 改策略 | 新建 |
|-----------|------|--------|------|
| `orientControl` | 定向 | `mergeEditV2` | `batchAddV2` |
| `flowControl` | 扶持 | `mergeEdit` | （按模块，非本技能默认） |
| `darkControl` | 暗投 | `mergeEdit` | — |
| `mediaControl` | 媒体 | `mergeEdit` | — |
| `innerControl` | 内循环 | `mergeEdit` | — |
| `shareRatioControl` | 流量分成 | `mergeEdit` | — |

用户指定的假期系数策略优先探测 `orientControl`。仍须 `get` + 空壳校验确认，不可写死跳过探测。
探测顺序：上表自上而下；第一个通过空壳校验的即命中。

### 空壳校验（`get` 必做）

`orientControl/get` 对**不属于定向**的 ID 也常返回 `status=200` 但 `data` 空。未命中若：

- HTTP 非 2xx，或
- `data` 为 `null` / `{}` / `[]`，或
- `data` 为对象且无 `id` / `ruleId` / 非空 `name` 等标识

→ 试下一前缀。

---

## 1. 读模板

```
GET /{apiPrefix}/get?id={strategyId}
```

| 模板 ID | 固定小时段（叠到目标日） |
|---------|--------------------------|
| 用户指定策略 ID | 用户指定的开始时间 – 结束时间 |

响应常用字段：`id`, `name`, `type`, `beginTime`, `endTime`, `createTime`, `status`, `statusDesc`, `adCluster`, `mediaCluster`, `background`, `toolBar`, `displayQuickPushBtn`, …

- 时间多为 **epoch-ms**（上海墙钟）
- `status` / `statusDesc` = **策略运行态**，与审核单 `approve.status` **不是同一套**

---

## 2. 时间叠加（本地，不调接口）

时区：`Asia/Shanghai`（UTC+8，无夏令时）。

```text
beginTime = epoch_ms(目标日期 + 用户指定开始时间)
endTime   = epoch_ms(目标日期 + 用户指定结束时间)
```

规则：

1. **只改时间窗**；严格使用用户确认的目标日期、开始时间、结束时间
2. **不要**从 get 回来的旧 `beginTime`/`endTime` 做简单偏移——以输入参数重算，避免历史脏窗污染
3. 输出必须是毫秒时间戳整数；开始和结束必须同日且结束晚于开始

**直接调用脚本计算**（禁止在脑中手算 epoch-ms）：

```bash
uv run <skill_directory>/scripts/overlay_time.py --day 2026-09-26
```

输出 `data.windows` 里每个模板 ID 对应的 `beginTime` / `endTime`（epoch-ms）即为可提交值。

---

## 3. 模式 A：改原 ID 提审

### 定向

```
POST /orientControl/mergeEditV2
Content-Type: application/json
```

提交体建议严格白名单（多传只读字段易 **412**）：
`id`, `name`, `type`, `background`, `shieldType`, `shieldMediaType`, `shieldUserType`, `beginTime`, `endTime`, `adCluster`, `mediaCluster`
以及假期系数业务实际需要、且 get 里已有的同类可写字段（以平台为准；拿不准时宁缺毋滥，优先白名单）。

Sanitize 要点：

| 动作 | 说明 |
|------|------|
| 覆盖时间 | 仅替换 `beginTime` / `endTime` 为叠好的值 |
| `adCluster.industryVersion` | 补 `"6.6"`（若缺失） |
| 去掉只读壳 | 如 `adCluster.maxNum` / `mediaCluster.maxNum`、`status`、`statusDesc`、`createTime`、`updateTime`、`toolBar`、`displayQuickPushBtn`、`_apiPrefix` |
| `background` | 必填非空；GET 常缺，可用 `name` 兜底 |
| 禁止 | 手搓空 `adCluster` / `mediaCluster`；改系数/名单 |

### 非定向

```
POST /{apiPrefix}/mergeEdit
```

透传业务字段，剔除上表只读探测字段；仍补 `adCluster.industryVersion`。
**不要**对非定向误调 `mergeEditV2`。

---

## 4. 模式 B：每天新建（定向）

```
POST /orientControl/batchAddV2
Content-Type: application/json
```

- Body 以 get 模板为蓝本：拷贝业务字段 + 叠好的时间
- **去掉**模板 `id`（由平台分配新 ID）
- `type` 以 get 为准（本仓库其它场景见过 `7` 屏蔽、`9` 预算定向等；假期系数以实际 get 为准，勿写死）
- 成功响应取出新策略 id，供审核 / 汇报

非定向批量新建不在本技能默认路径；若 get 非 `orientControl`，先停下来确认模块再建。

---

## 5. 审核单

### 查询

```
POST /approve/query
Content-Type: application/json
```

按策略 id / ruleId、审核状态等查单。候选过滤字段以平台为准（常见带策略 id、分页）。

审核单 `status` 常见：

| code | 含义 |
|------|------|
| 1 | 待审核 |
| 2 | 发布成功（终态） |
| 3 | 审核驳回 |
| 6 | 审核成功 |
| 7 | 发布失败 |
| 12 | 封禁期待审核 |
| 13 | 封禁期审核通过 |
| 14 | 封禁期审核驳回 |

### 改状态

```
POST /approve/changeStatus
Content-Type: application/json
```

对**审核记录 id** 流转（不是策略 id）。普通流：

1. `1 → 6`（审核成功）
2. `6 → 2`（发布成功）

封禁期：`12 → 13` 等按文案；确认类可带 `confirm=true`。
