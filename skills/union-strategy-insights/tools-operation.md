# Operation（ops-platform）API

基址：

```
https://ops.example.corp/ops/rest
```

鉴权：内网 Auth Cookie（浏览器会话 / 代理注入）。成功壳常见 `status: 200`；**不能**只看 status，见「空壳」节。

行业版本：提交/查询常需 `industryVersion: "6.6"`（放顶层或 `adCluster` 内）。

---

## 0. 策略类型前缀

| apiPrefix | 类型 | 提审接口 |
|-----------|------|----------|
| `targetingControl` | 定向 | `submitEdit` |
| `boostControl` | 扶持 | `submitEdit` |
| `shadowControl` | 暗投 | `submitEdit` |
| `placementControl` | 媒体 | `submitEdit` |
| `internalControl` | 内循环 | `submitEdit` |
| `shareControl` | 流量分成（页路由 `/stream`） | `submitEdit` |

- **无** `generalControl`。综合页 `/general` 走 `allControl/query` 聚合，**无**独立 get/submitEdit。
- 探测顺序：上表自上而下；第一个通过「空壳校验」的即命中。

### 空壳校验（`get` 必做）

`targetingControl/get` 对**不属于定向**的 ID 也常返回 `status=200`，但 `data` 为空。未命中若：

- HTTP 非 2xx，或  
- `data` 为 `null` / `{}` / `[]`，或  
- `data` 为对象且无 `id` / `ruleId` / 非空 `name` 等标识  

---

## 1. 按 ID 取详情

```
GET /{apiPrefix}/get?id={strategyId}
```

用途：查询、延期提审前读全量、审核侧补策略信息、类型探测。

响应：`{ status, data: { id, name, beginTime, endTime, createTime, status, statusDesc, adCluster, mediaCluster, toolBar, displayQuickPushBtn, … } }`  
时间多为 epoch-ms。`status`/`statusDesc` 是**策略运行态**，与审核单 `approve.status` **不是同一套**。

---

## 2. 定向列表查询

```
POST /targetingControl/query
Content-Type: application/json
```

与定向管理页「查询」同源。多值用英文 `;` 拼接。

候选 body（按兼容性试）：

```json
{
  "pager": { "pageNum": 1, "pageSize": 50 },
  "industryVersion": "6.6",
  "status": 4,
  "uid": "开发者id或多值;分隔",
  "posId": "...",
  "appId": "..."
}
```

变体：`industryVersion` 放进 `adCluster`；或过滤放 `mediaCluster`；或 `uid` 写成 `mediaUid`。

分页列表可能缺字段，重要操作建议再 `GET …/get?id=`。

---

## 3. 综合聚合查询（可选）

```
POST /allControl/query
```

综合策略页聚合；**不能**当单策略 get/submitEdit 前缀用。

---

## 4. 提审 / 改策略

### 定向

```
POST /targetingControl/submitEdit
Content-Type: application/json
```

提交体建议严格白名单（多传只读字段易 **412**）：

`id`, `name`, `type`, `background`, `shieldType`, `shieldMediaType`, `shieldUserType`, `beginTime`, `endTime`, `adCluster`, `mediaCluster`

- `adCluster.industryVersion` 补 `"6.6"`  
- 去掉 `adCluster.maxNum` / `mediaCluster.maxNum` 这类只读壳  
- `background` 必填非空（GET 常缺，可用 name 兜底）  
- 延期场景：通常 **只改 `endTime`**，其余从 GET 原样裁剪后提交  

### 非定向（扶持/暗投/…）

```
POST /{apiPrefix}/submitEdit
```

透传业务字段（含 `scope` 等），剔除只读：`status`, `statusDesc`, `createTime`, `updateTime`, `toolBar`, `displayQuickPushBtn`, `_apiPrefix` 等。仍补 `adCluster.industryVersion`。

---

## 5. 批量新建（定向）

```
POST /targetingControl/batchCreate
Content-Type: application/json
```

本仓库已用：

| type | 场景 |
|------|------|
| `7` | 定向屏蔽 |
| `9` | 游戏优质媒体 / 预算定向（预算侧） |

Body 以各业务模块为准；鉴权同 Auth。

---

## 6. 撤回

```
POST /targetingControl/withdraw
```

（工作台代理路径对应此上游；body/query 以抓包为准，常见带策略 id。）

---

## 7. 立即推全

```
POST /{apiPrefix}/quickPushAll?id={strategyId}&type={0|1|2}[&confirm=true]
```

- Body：空 `{}`（参数走 **querystring**）  
- 支持前缀（前端契约）：`targetingControl` / `shadowControl` / `boostControl`  
- `type`：`0` 普通立即推全；`1` 封禁期审批；`2` 封禁期立即推全  
- 二次确认文案时带 `confirm=true`  
- HTTP 成功只表示**受理**；策略侧可能停在 `status=10`「推全中」，需轮询至 `4` 生效 / `11` 失败 / 超时  

推全门闩优先看 `*/get` 的 `displayQuickPushBtn` / `toolBar`。

---

## 8. 审核单

基址同 Operation rest。

### 查询

```
POST /approve/query
```

按审核状态、策略 id / ruleId 等查审核单。`status` 枚举与策略运行态不同，常见：

| code | 含义（Targeting 文案） |
|------|---------------------|
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
```

对**审核记录 id** 流转（如 `1→6`、`6→2`）。封禁期确认可带 `confirm=true`（body + query）。  
灰度旁路：策略已灰度/推全中时，`6→2` 可能被拒，仍可按推全标志走 `quickPushAll`。

---

## 9. 用户列表

```
GET /common/listUser
```

- **无 body**（不要 POST）  
- 常见：`data.userList[]`，含 `id`、`name`  
- 用于提交人 / 创建人映射  

---

## 10. 策略运行态（get）摘记

与审核单分离。常见策略侧：`4` 生效中、`8`/`lastStatus=8` 灰度相关、`10` 推全中、`11` 失败等（以平台文案为准）。

---

## 已知坑

1. `get` 空壳 + 误判定向 → 必须二次校验再试下一前缀  
2. 定向必须 `submitEdit`，其它类型 `submitEdit`（非 V2）  
3. 定向提交字段过多 → 412  
4. `quickPushAll` 成功 ≠ 已生效  
5. Operation Cookie ≠ BI Cookie  
