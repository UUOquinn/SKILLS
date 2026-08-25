# 联盟诊断工作台 · HTTP API 契约

> **Skill：** `alliance-workbench-api`  
> **源码：** 工作台 `server/app.py` + `server/modules/*`  
> **同步自：** [docs/API.md](https://github.com/UUOquinn/alliance-advertiser-funnel-web/blob/main/docs/API.md)  
> **更新说明：** 与公开仓库实现同步；**策略效果监控**为占位壳（`shell: true`）。上游真实域名不入库。

**目录：** [约定](#1-约定) · [索引](#2-快速索引) · [系统](#3-系统) · [日志](#4-工作台日志) · [查询](#5-策略查询--行业映射) · [详情/延期](#6-策略详情--延期提交--撤回) · [审核](#7-策略审核) · [延期托管](#8-策略延期托管) · [游戏定投](#9-游戏优质媒体定投) · [定向屏蔽](#10-定向屏蔽) · [数据集](#11-数据集kwaibi) · [DataAgent](#12-dataagent旁路) · [效果监控壳](#13-策略效果监控占位壳) · [示例](#14-调用示例)

---

## 1. 约定

| 项 | 说明 |
|----|------|
| Base URL | `http://127.0.0.1:3000`（浏览器 / Agent 只访问本机；内网可用部署机 `IP:3000`） |
| 内容类型 | `Content-Type: application/json`（POST） |
| 鉴权 | Orient 类由服务端 **Playwright / Cookie 代理**，**无用户 Bearer Token** |
| CORS | 支持 `OPTIONS`；常用头见下表 |

### 常用请求头

| 头 | 用途 |
|----|------|
| `Content-Type` | JSON POST |
| `X-Postpone-Operator` | 延期托管写操作 / 日志操作人 |
| `X-Kwabi-Cookie` | 覆盖 KwaiBI Cookie |
| `X-DataAgent-Cookie` | 覆盖 DataAgent Cookie |

### 响应包络

成功：

```json
{ "success": true, "data": { } }
```

失败（常见）：

```json
{ "success": false, "error": "ERROR_CODE", "message": "可读说明" }
```

部分上游业务失败仍返回 **HTTP 200**，以 `success` / `error` 为准（如 `ORIENT_FAILED`、`STRATEGY_NOT_FOUND`）。

### 常见错误码

| HTTP | error | 含义 |
|------|-------|------|
| 400 | （文案） | 参数缺失 / 非法 |
| 401 | `COOKIE_EXPIRED` | Orient / 数据平台 Cookie 失效，需服务端重新登录 |
| 403 | `FORBIDDEN` | 延期托管无编辑权限 |
| 404 | `Not found` / `NOT_FOUND` | 路径或资源不存在 |
| 502 | （文案） | 上游代理失败 |
| 503 | `SSO_RECOVERING` | SSO 自愈中，稍后重试 |

### POST Body

- 空 body → `400` `Empty body`
- 非法 JSON → `400` `Invalid JSON`
- 部分「触发类」接口不读字段，但仍需非空 JSON（如 `{}`）

---

## 2. 快速索引

| Method | Path | 模块 |
|--------|------|------|
| GET | `/api/health` | 系统 |
| GET | `/api/cookie/status` | 系统 |
| GET/POST | `/api/workbench/logs` | 工作台日志 |
| GET | `/api/strategy/meta` | 策略查询 |
| POST | `/api/strategy/query` | 策略查询 |
| POST | `/api/strategy/industry/map` | 行业映射 |
| POST | `/api/strategy/renew/get` | 策略详情 |
| POST | `/api/strategy/renew/submit` | 策略延期提交 |
| POST | `/api/strategy/audit/get` | 同 renew/get |
| POST | `/api/strategy/audit/query` | 同 renew/get |
| POST | `/api/strategy/audit/withdraw` | 撤回审核 |
| POST | `/api/strategy/audit/changeStatus` | 改审核状态 |
| POST | `/api/strategy/audit/batchPass` | 批量改状态 |
| POST | `/api/strategy/audit/flow` | 审核编排 |
| POST | `/api/strategy/audit/pushFull` | 强制推全 |
| GET | `/api/strategy/audit/users` | 审核人列表 |
| GET | `/api/strategy/audit/realtime-whitelist` | 自动审白名单 |
| GET | `/api/strategy/audit/auto-approve/status` | 自动审状态 |
| POST | `/api/strategy/audit/auto-approve/trigger` | 触发自动审 |
| POST | `/api/strategy/audit/queryByCreator` | 按提交人查审核单 |
| POST | `/api/strategy/audit/queryByStrategy` | 按策略查审核单 |
| GET | `/api/strategy/postpone/registry` | 延期托管表 |
| GET | `/api/strategy/postpone/editors` | 延期编辑人 |
| GET | `/api/strategy/postpone/status` | 延期守护状态 |
| POST | `/api/strategy/postpone/registry/upsert` | 托管增改 |
| POST | `/api/strategy/postpone/registry/delete` | 托管删除 |
| POST | `/api/strategy/postpone/editors/upsert` | 编辑人增改 |
| POST | `/api/strategy/postpone/editors/delete` | 编辑人删除 |
| POST | `/api/strategy/postpone/trigger` | 触发延期扫描 |
| POST | `/api/strategy/game-premium/media-by-accounts` | 游戏定投·按账户 |
| POST | `/api/strategy/game-premium/media-by-name` | 游戏定投·按名称 |
| POST | `/api/strategy/game-premium/create` | 游戏定投·创建 |
| POST | `/api/strategy/shield-platform/create` | 定向屏蔽·创建 |
| GET | `/api/dataset/list` | KwaiBI 数据集列表 |
| POST | `/api/dataset/query` | KwaiBI 查数 |
| POST | `/api/dataset/metadata` | KwaiBI 元数据 |
| GET | `/api/dataagent/status` | DataAgent（旁路） |
| POST | `/api/dataagent/chat` | DataAgent 对话（旁路） |
| POST | `/api/dataagent/cookie` | 保存 DataAgent Cookie |
| GET | `/api/dataagent/scheduled-tasks` | 定时任务列表 |
| GET | `/api/dataagent/scheduled-tasks/detail` | 定时任务详情 |
| GET | `/api/dataagent/scheduled-tasks/instances` | 定时任务执行实例 |
| POST | `/api/dataagent/scheduled-tasks/create` | 创建定时任务 |
| POST | `/api/dataagent/scheduled-tasks/update-or-delete` | 编辑或删除定时任务 |
| GET/POST | `/api/strategy/effect-monitor*` | **占位壳** |

---

## 3. 系统

### `GET /api/health`

轻量健康检查（不打 Orient）。

**响应 `data`：**

| 字段 | 说明 |
|------|------|
| `ok` | Playwright 已初始化且已登录且线程存活 |
| `playwrightReady` | 已登录 |
| `threadAlive` | 代理线程存活 |
| `queueDepth` | Playwright 队列深度 |
| `ssoWaiting` | 是否在等 SSO |
| `cookieUpdatedAt` / `cookieSource` | cookie 元信息 |

```bash
curl -sS http://127.0.0.1:3000/api/health
```

### `GET /api/cookie/status`

Cookie / 代理模式诊断。

**响应 `data` 要点：** `source`（`playwright` / `server` / `playwright-needs-login` 等）、`playwrightReady`、`threadAlive`、`hint`。

---

## 4. 工作台日志

### `GET /api/workbench/logs`

| 参数 | 必填 | 说明 |
|------|------|------|
| `type` | 是 | `query` \| `error` \| `audit` \| `postpone` \| `shield` \| `game-premium` |
| `limit` | 否 | 默认 500 |
| `since` | 否 | ms 时间戳，只返回之后条目 |

**响应：** `{ type, items[], count }`；条目含 `id, type, ts, operator, text, meta`。

### `POST /api/workbench/logs`

**追加：**

```json
{ "type": "error", "text": "说明", "meta": {}, "operator": "optional" }
```

**清理：**

```json
{ "action": "clear", "type": "error" }
```

```json
{ "action": "delete", "type": "error", "ids": ["..."] }
```

操作人也可走请求头 `X-Postpone-Operator`。

---

## 5. 策略查询 / 行业映射

### `GET /api/strategy/meta`

最近一次实时查询的元信息（未查过则 `dataAsOfText` 为空）。

### `POST /api/strategy/query`

按开发者 / 广告位 / 应用 ID 实时查定向策略。

```json
{
  "message": "可选自然语言",
  "developerIds": ["123"],
  "posIds": [],
  "appIds": []
}
```

至少一类 ID 非空（可由 `message` 解析）。

**成功 `data`：** `parsed`、`matchedBy`、`total`、`rows[]`、`meta`。

### `POST /api/strategy/industry/map`

行业 ID → 名称（本地表）。

```json
{ "ids": "1,2,3", "prefer": "auto" }
```

`prefer` / `level`：`auto`（默认）\| `first` \| `second`。也可用 `text` / `message`。

---

## 6. 策略详情 / 延期提交 / 撤回

详情探测顺序：`orientControl` → `flowControl` → `darkControl` → `mediaControl` → `innerControl` → `shareRatioControl`。

### `POST /api/strategy/renew/get`

别名（同实现）：

- `POST /api/strategy/audit/get`
- `POST /api/strategy/audit/query`

```json
{ "id": 10001 }
```

成功返回上游 payload，并带 `_strategyType` / `_apiPrefix`。未命中：`success:false, error:"STRATEGY_NOT_FOUND"`（HTTP 仍可能为 200）。

### `POST /api/strategy/renew/submit`

延期 / 改策略提交（`mergeEditV2` 或按类型 `mergeEdit`）。

```json
{
  "id": 10001,
  "body": { },
  "apiPrefix": "orientControl"
}
```

`apiPrefix` / `api_prefix` 可选，默认 `orientControl`。

### `POST /api/strategy/audit/withdraw`

```json
{ "id": 10001 }
```

---

## 7. 策略审核

### `GET /api/strategy/audit/users`

提交审核人列表（约 90s 缓存）。SSO 恢复中可能 `503 SSO_RECOVERING`。

### `GET /api/strategy/audit/realtime-whitelist`

自动审核白名单（工作台解析 `realtime-check.js`）。**仅约束后台自动审**，手动查询接口不拦。

### `GET /api/strategy/audit/auto-approve/status`

返回 `running`、`interval`、`lastResult`、`log[]`、卡单队列等。

### `POST /api/strategy/audit/auto-approve/trigger`

手动触发一轮（异步）。Body 可用 `{}`。若已在跑：`ALREADY_RUNNING`。

### `POST /api/strategy/audit/changeStatus`

```json
{
  "id": 10001,
  "status": 6,
  "reason": "可选",
  "creatorId": 0,
  "approveId": 0
}
```

### `POST /api/strategy/audit/batchPass`

```json
{ "ids": [10001, 10002], "status": 6, "reason": "可选" }
```

任一条 Cookie 失效则整请求 `401`。

### `POST /api/strategy/audit/flow`

审核编排。

```json
{
  "id": 10001,
  "mode": "normal",
  "reason": "",
  "creatorId": 0,
  "approveId": 0
}
```

`mode`：`normal`（默认）\| `ban` \| `publish_replay`。  
HTTP `success` 表示请求层成功；业务看 `data.ok` / `stuckAt`。

### `POST /api/strategy/audit/pushFull`

```json
{ "id": 10001, "mode": "normal" }
```

`mode`：`normal` \| `ban`。

### `POST /api/strategy/audit/queryByCreator`

```json
{ "creatorId": 20000001, "status": 1, "pageSize": 100 }
```

### `POST /api/strategy/audit/queryByStrategy`

```json
{ "ruleId": 10001, "status": 1, "pageSize": 100 }
```

也可用字段 `id` 代替 `ruleId`。

#### 审核状态码速查（工作台常用）

| code | 含义（概要） | 常见下一步 |
|------|--------------|------------|
| 1 | 待审核 | → 6 通过 / → 3 驳回 |
| 2 | 发布成功 | 终态 |
| 3 | 审核驳回 | 终态 |
| 6 | 审核成功 | → 2 同意发布 / → 7 拒绝发布 |
| 7 | 发布失败 | 终态 |
| 12 | 封禁期待审核 | → 13 / → 14 |
| 13 | 封禁期审核通过 | → 封禁期推全 |
| 14 | 封禁期审核驳回 | 终态 |

完整真值表以工作台 Orient 契约为准；调用时以接口返回的 `statusDesc` 为准。

---

## 8. 策略延期托管

写接口权限：请求头 `X-Postpone-Operator` 或 body `operator` + editors 白名单。  
`admins` 为空时为开放编辑模式。

### `GET /api/strategy/postpone/registry`

返回 `{ items[], perms }`。

### `GET /api/strategy/postpone/editors`

返回 `{ admins[], editors[], perms }`。

### `GET /api/strategy/postpone/status`

守护状态：`running`、`scheduleHour`/`scheduleMinute`、`nextRunAt`、`dueWithinDays`、`lastResult`、`log[]` 等。

### `POST /api/strategy/postpone/registry/upsert`

```json
{
  "strategy_id": 10001,
  "api_prefix": "orientControl",
  "enabled": true,
  "owner": "optional",
  "renew_months": 1,
  "note": "",
  "operator": "optional"
}
```

`api_prefix` / `apiPrefix` **必填**。

### `POST /api/strategy/postpone/registry/delete`

```json
{ "strategy_id": 10001, "operator": "optional" }
```

### `POST /api/strategy/postpone/editors/upsert`

```json
{ "name": "operator_example", "asAdmin": false, "operator": "optional" }
```

需 `canManageEditors`。

### `POST /api/strategy/postpone/editors/delete`

```json
{ "name": "operator_example", "operator": "optional" }
```

### `POST /api/strategy/postpone/trigger`

手动触发一轮扫描（后台线程）。Body：`{}`。可能 `ALREADY_RUNNING`。

---

## 9. 游戏优质媒体定投

### `POST /api/strategy/game-premium/media-by-accounts`

```json
{ "accountIds": ["..."], "status": 4, "hydrate": true }
```

### `POST /api/strategy/game-premium/media-by-name`

```json
{
  "nameKeyword": "游戏",
  "nameContains": "优质媒体",
  "status": 4
}
```

### `POST /api/strategy/game-premium/create`

创建定投（`batchAdd`）。主要字段：`accountIds`、`mediaIds`、`name`、`mediaDim`（`appId`\|`uid`\|`posId`）、`beginTime`/`endTime`、`dryRun` 等。

---

## 10. 定向屏蔽

### `POST /api/strategy/shield-platform/create`

新建 type=7 屏蔽策略（`batchAddV2`）。

| 字段 | 说明 |
|------|------|
| `name` / `background` | 名称 / 背景 |
| `shieldType` | 默认 2 |
| `shieldMediaType` | `appId` \| `uid` \| `posId` |
| `mediaValues` / `mediaIds` | 媒体名单（必填） |
| `shieldUserTypes` / `shieldUserType` | 广告主维度（必填） |
| `adValues` | 各维度名单 object |
| `placements` | 默认 1 |
| `beginTime` / `endTime` | ms |
| `dryRun` | 可选试跑 |

---

## 11. 数据集（KwaiBI）

Cookie 解析顺序：`X-Kwabi-Cookie` → `Cookie` 头 → 服务端本地 cookie 文件。  
上游真实基址由本机环境变量注入，本契约不写死。

### `GET /api/dataset/list`

内置数据集目录（如离线 `85587`、实时 `129496`）。

### `POST /api/dataset/query`

```json
{
  "datasetId": "129496",
  "metrics": ["..."],
  "dimensions": ["..."],
  "filters": { "__time": { "start": "...", "end": "..." } },
  "limit": 100
}
```

### `POST /api/dataset/metadata`

```json
{ "datasetId": "129496" }
```

---

## 12. DataAgent（旁路）

不经 Orient Playwright。Cookie：`X-DataAgent-Cookie` → `X-Kwabi-Cookie` → 服务端文件。  
前端默认主路径不依赖本模块。

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/dataagent/status` | 登录态 / agent 信息 |
| POST | `/api/dataagent/chat` | NL 问答：`message` / `question` / `text` |
| POST | `/api/dataagent/cookie` | 保存 Cookie：`cookie` / `value` |
| GET | `/api/dataagent/scheduled-tasks` | 定时任务列表 |
| GET | `/api/dataagent/scheduled-tasks/detail?id=` | 定时任务详情 |
| GET | `/api/dataagent/scheduled-tasks/instances` | 执行实例列表 |
| POST | `/api/dataagent/scheduled-tasks/create` | 创建定时任务 |
| POST | `/api/dataagent/scheduled-tasks/update-or-delete` | 编辑或删除（`action=delete`） |
| POST | `/api/dataagent/scheduled-tasks/pause` · `resume` · `trigger` | 暂停 / 启用 / 立即执行 |

---

## 13. 策略效果监控（占位壳）

当前实现为壳：`shell: true`，文案「策略效果监控暂为占位，功能未开放」。  
分小时警报 **不启动**；清单不落真实业务。

| Method | Path | 壳行为 |
|--------|------|--------|
| GET | `/api/strategy/effect-monitor` | 空清单 + `shell` |
| POST | `/api/strategy/effect-monitor/add` | 空清单 |
| POST | `/api/strategy/effect-monitor/remove` | 空清单 |
| POST | `/api/strategy/effect-monitor/resolve` | 空清单 |
| POST | `/api/strategy/effect-monitor/query` | 通常 `400` 监控清单为空 |
| GET | `/api/strategy/effect-monitor/alert` | `enabled:false` + `shell` |
| GET | `/api/strategy/effect-monitor/alert/status` | `running:false` + `shell` |
| POST | `/api/strategy/effect-monitor/alert` | 忽略 body，返回壳配置 |
| POST | `/api/strategy/effect-monitor/alert/run` | `skipped:true, reason:"shell"` |

---

## 14. 调用示例

```bash
# 健康
curl -sS http://127.0.0.1:3000/api/health | python3 -m json.tool

# 策略查询
curl -sS http://127.0.0.1:3000/api/strategy/query \
  -H 'Content-Type: application/json' \
  -d '{"appIds":["123456"]}' | python3 -m json.tool

# 自动审核状态
curl -sS http://127.0.0.1:3000/api/strategy/audit/auto-approve/status | python3 -m json.tool

# 延期托管状态
curl -sS http://127.0.0.1:3000/api/strategy/postpone/status | python3 -m json.tool

# 触发自动审核（需非空 JSON）
curl -sS http://127.0.0.1:3000/api/strategy/audit/auto-approve/trigger \
  -H 'Content-Type: application/json' -d '{}'
```

---

## 15. 相关链接

| 资源 | 说明 |
|------|------|
| [SKILL.md](./SKILL.md) | Agent 入口 · 模块路由 · 调用清单 |
| [工作台 docs/API.md](https://github.com/UUOquinn/alliance-advertiser-funnel-web/blob/main/docs/API.md) | 同源实现文档 |
| [工作台 README](https://github.com/UUOquinn/alliance-advertiser-funnel-web) | 产品定位 · 启动约定 |

KwaiBI / DataAgent 上游路径细节以本机环境与模块实现为准；本 Skill 不写真实基址。
