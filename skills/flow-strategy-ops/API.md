# flowControl API 契约

基址：`https://ops-platform.example.corp/ops/rest`

## 鉴权

仅使用 `SmartSSOSession` 代表当前用户直连内网 REST API。它会复用有效 SSO，必要时刷新认证。
Skill 不保存 Cookie、Token 或 `api.json`，不使用网页 UI 和浏览器自动化。

## 接口

| Method | Path | 参数位置 | 说明 |
|---|---|---|---|
| GET | `/flowControl/get` | query `id` | 单条详情 |
| POST | `/flowControl/query` | JSON body | 分页查询；需顶层 `industryVersion` |
| POST | `/flowControl/export` | JSON body | 仅返回受理回执（JSON 壳）；真实导出由脚本 query+本地生成 xlsx |
| POST | `/flowControl/mergeEdit` | JSON body | 单条修改并提审；名单增删也复用此接口 |
| POST | `/flowControl/batchAddV2` | `{list:[item]}` | 服务端批量接口，本 Skill 限制为单元素 |
| POST | `/flowControl/changeStatus` | query | 单条状态流转 |
| DELETE | `/flowControl/delete` | JSON body `[id]` | 本 Skill 限制为单 ID |
| GET | `/flowControl/log` | query `id` | 状态时间线 |
| POST | `/flowControl/showDiffInfo` | query `id` | 版本 diff |
| POST | `/flowControl/quickPushAll` | query `id,type,confirm?` | 单条立即推全 |

## 查询请求

`query` 的分页参数必须放在 `pager` 内，行业版本放在顶层：

```json
{
  "pager": {"pageNum": 1, "pageSize": 20},
  "industryVersion": "6.6",
  "status": 4
}
```

缺少顶层 `industryVersion` 时，服务端可能返回业务状态 `411`。

## 详情与提交

详情常见字段：`id`、`name`、`type`、`background`、`beginTime`、`endTime`、
`status`、`adCluster`、`mediaCluster`、`scope`。

`scope`：
- `1`：广告主维度，仅提交 `adCluster`
- `2`：媒体维度，仅提交 `mediaCluster`
- 其他：双维度

**create 时 scope 与 cluster 必须对齐**：scope=1 只传 adCluster，scope=2 只传 mediaCluster，
scope=3 两个都传。多传一个**空对象**（如 scope=2 时附 `"adCluster":{}`）会让服务端报
「生效范围不能为空」，这是实测踩过的坑。已生效线上策略的 cluster 写法（可作参照）：
- scope=1：`adCluster` 含 `placements/placementsArr`（如 [1]）+ 名单字段
- scope=2：`mediaCluster` 含 `appId`/`uid`/`posId` 之一，配 `"isAllMedia":false,"osType":0`
- scope=3：两个 cluster 都带名单字段（如 mediaCluster.appId + adCluster.product）

`type=3` 为顶价（CPM），`bonusRate` 即定价系数，如 1.2 表示系数 1.2。
名称不允许重复：服务端对同名新建报「名称已存在」，改名（追加 -2/-3）后重试。

写操作前提：`get` 返回的详情存在且 `status != 5`（未失效）、`isDel != true`（未删除）。
delete 是逻辑删除，get 仍能查到 `isDel=true` 的记录；对这类记录执行 mergeEdit/add-ids 会被
`NOT_WRITABLE` 拦截。

修改采用“先 GET 全量，再合并 patch”的方式。提交前删除状态、快照、工具栏、时间范围等只读字段；
保证 `adCluster.industryVersion` 和非空 `background`。不得用局部空 body 直接覆盖，避免维度丢失。

## 名单增删

新增/删除名单要求用户先指定明确的策略 ID（`add-ids <策略ID>` / `remove-ids <策略ID>`），
执行前先 `GET /flowControl/get` 核对策略存在，再对名单做集合增删，最后复用 `mergeEdit` 提审。
幂等：增删目标已满足（新增的已存在 / 删除的不存在）时不提交，直接返回
`ALREADY_PRESENT` / `ALREADY_ABSENT`；只有名单实际变化才写线上。

### 名单维度表

| 维度 | 所属模板 | 说明 | 值类型 |
|---|---|---|---|
| `accountId` | 广告主信息（adCluster） | 广告主账户 ID | 数字，逗号分隔 |
| `product` | 广告主信息（adCluster） | 产品 | 字符串，逗号分隔 |
| `secondIndustryV6` | 广告主信息（adCluster） | 二级行业 | 字符串，逗号分隔 |
| `campaignType` | 广告主信息（adCluster） | 推广目的 | 字符串，逗号分隔 |
| `ocpxActionType` | 广告主信息（adCluster） | 优化目标 | 字符串，逗号分隔 |
| `appId` | 媒体信息（mediaCluster） | 媒体应用 ID | 数字，逗号分隔 |
| `uid` | 媒体信息（mediaCluster） | 媒体用户 ID | 数字，逗号分隔 |
| `posId` | 媒体信息（mediaCluster） | 广告位 ID | 数字，逗号分隔 |
| `adStyle` | 媒体信息（mediaCluster） | 广告样式 | 字符串，逗号分隔 |

`accountId` / `appId` / `uid` / `posId` 为纯数字维度，输入含非数字会返回 `INVALID_ARGUMENT`，
避免把账户名、产品名误写进 ID 字段。

## 业务响应

成功壳常见为：

```json
{"status": 200, "message": "成功", "data": {}}
```

主要错误：
- `411/412`：行业版本不匹配或升级；核对 `industryVersion`，API-only 模式不转 UI。
- `401/302`：SSO 失效。
- `403`：当前用户无权限。
- `5xx`：服务端异常，写请求不自动重试。

## 立即推全

```text
POST /flowControl/quickPushAll?id={strategyId}&type={0|1|2}[&confirm=true]
```

`type=0` 普通立即推全，`1` 提交封禁期审批，`2` 封禁期立即推全。
接口成功只代表请求被受理，需要再用 `get` 检查最终状态。
