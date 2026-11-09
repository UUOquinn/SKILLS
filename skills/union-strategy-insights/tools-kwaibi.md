# KwaiBI API

基址：

```
https://bi-platform.example.corp
```

鉴权：Cookie 须含 KwaiBI 会话（常见 `kwaibi-user-token` / accessproxy）。  
**已验证**：番薯/DataAgent 侧 Cookie 可用；纯 Orient Cookie 调本接口常 **401**。

请求建议头：`Accept: application/json`，`Origin`/`Referer` 指向 KwaiBI 域名。

---

## 1. 数据集字段定义

```
GET /api/v1/dataset/detail?id={dataSetId}
```

用途：查数前解析字段 `id` / `name` / `type`（metric | dimension）。

成功：`result` 为真，`data.fields[]` 含字段元数据。  
查数 VO 的 `fields[].id` **必须**来自本接口，不要臆造。

CLI 默认精简输出（`id`/`name`/`type`/`valueType`/`urn`），加 `--full` 才输出原始完整字段；
部分扩展字段（如按账户分组业务类型的 `functionExpression`）可能达数 MB，不要整包回显到上下文。

---

## 2. 结构化查数（主路径）

```
POST /api/v1/data/query
Content-Type: application/json
```

**注意路径**：是 `/api/v1/data/query`。  
**不要**打 `/api/v1/dataset/data/query`（已验证 404）。

### DataQueryVO（已验证形状）

```json
{
  "dataSetId": 85587,
  "type": 1,
  "fields": [
    {
      "id": 123,
      "name": "产品名称",
      "type": "dimension",
      "dataSetId": 85587,
      "urn": ""
    },
    {
      "id": 456,
      "name": "联盟总消耗",
      "type": "metric",
      "dataSetId": 85587,
      "urn": ""
    }
  ],
  "filters": [
    {
      "field": {
        "id": 789,
        "name": "开发者id",
        "type": "dimension",
        "dataSetId": 85587,
        "urn": ""
      },
      "operator": "IN",
      "value": ["111", "222"]
    }
  ],
  "timeFilter": {
    "fieldName": "__time",
    "startTime": 1719936000000,
    "endTime": 1720022399999
  },
  "limit": 100,
  "rowLimit": 100
}
```

| 字段 | 说明 |
|------|------|
| `dataSetId` | 见下表 |
| `type` | 固定 `1`（本仓库主路径） |
| `fields` | 维度 + 指标；先 detail 再填 id |
| `filters` | 可空数组；筛选用 `IN` + `value` 字符串数组 |
| `timeFilter` | epoch-ms；字段名 `__time`；建议 Asia/Shanghai |
| `limit` / `rowLimit` | 行数上限，两者都传 |

### 响应归一（本仓库理解）

上游成功时大致：

- `result` 非 0  
- `data.columns` / `data.dataList`（或 `rows`）  
- `data.rowCount`  

业务失败：`result` 为 `0`，看 `error_msg` / `message`（含登录文案则当 Cookie 失效）。

---

## 3. 常用数据集

| dataSetId | 类型 | 用途 |
|-----------|------|------|
| `85587` | 离线 | T-1 与历史主效果 |
| `129496` | 实时 | 当天实时；仅明确要「实时/当天」时用 |

默认不指定时用离线 `85587`。

---

## 4. 字段别名与筛选映射（本仓库约定）

### 4.1 友好名 → KwaiBI 真实字段名

| 别名 | 真实名 | 说明 |
|------|--------|------|
| CPM | 素材CPM | 无裸字段名 `CPM`，用 `素材CPM` |
| CPC | 素材CPC | 同理 |
| 成本率 | 暗投消耗占比 | 实际口径；另有 `暗投成本率`（预期口径），见下方歧义说明 |
| 消耗 | 联盟总消耗 | |
| 明投消耗 | 明投总消耗 | 85587 id=2344707 |
| 暗投消耗 | 暗投总消耗 | 85587 id=2344704 |
| 填充率 | 广告填充率 | |
| 曝光率 | 广告曝光率 | |
| CTR | CTR | 85587 id=1329816；⚠️ 与 `素材CPM` 不同，CTR 是独立字段 |
| CVR | CVR | 85587 id=1330674 |
| CPA | CPA | 85587 id=1330673 |

**⚠️ "暗投成本率"歧义说明**：

85587 里有两个名字相近的字段，含义完全不同：
- `暗投消耗占比`（id=2344701）：实际暗投消耗 / 联盟总消耗 → **这是日常说的"暗投成本率"**
- `暗投成本率`（id=2414296）：预期口径，基于预期消耗计算 → 用于预算/预测场景

用户说"暗投成本率"时默认用 `暗投消耗占比`（id=2344701），除非明确要预期口径。

### 4.2 筛选 key → 字段名

> ⚠️ 字段名是 KwaiBI detail 接口返回的 `name`，**必须逐字匹配**，不能臆造。

| filters key | 字段名 | 说明 |
|-------------|--------|------|
| `uid` | 开发者id | |
| `app_id` | 联盟媒体ID | ❗ 原"应用id"已纠正，KwaiBI 无"应用id"字段 |
| `pos_id` | 广告位 | ❗ 原"广告位id"已纠正，KwaiBI 无"广告位id"字段 |
| `ad_style` | 广告场景 | |
| `product_name` | 产品名称 | |
| `cooperation_mode` | 合作模式 | 策略侧高频维度 |
| `union_ad_type` | 联盟业务线 | |
| `flow_type` | 流量类型 | 85587 id=1427124 |
| `medium_inner_out_type` | 媒体类型_内外部 | |
| `business_type` | 联盟运营分类 | 85587 id=2346000 |
| `sdk_type` | sdk类型 | |
| `device_os` | 设备操作系统 | |
| `is_internal_account` | 是否内部客户 | |
| `industry_name` | 新媒体行业名称 | |
| `sub_industry_name` | 新媒体子行业名称 | |
| `account_id` | 账户ID | 广告主侧 |
| `campaign_id` | 计划id | |
| `unit_id` | 单元ID | |

### 4.3 策略→KwaiBI 桥接映射

从 Operation 策略字段到 KwaiBI 筛选字段的映射关系：

| Operation 策略字段路径 | KwaiBI 筛选字段名 | 说明 |
|----------------------|-----------------|------|
| `adCluster.uid` / `mediaCluster.mediaUid` | 开发者id | 定向/屏蔽开发者 |
| `adCluster.posId` | 广告位 | |
| `adCluster.appId` | 联盟媒体ID | 策略里叫 appId，KwaiBI 叫联盟媒体ID |
| `adCluster.adStyle` | 广告场景 | |
| `mediaCluster.industryVersion` | — | 不做筛选维度，但影响 industryVersion 参数 |
| `mediaCluster.mediaId` | 联盟媒体ID | 媒体侧 ID 与 app_id 同字段 |
| `adCluster.cooperationMode` | 合作模式 | |
| `adCluster.flowType` | 流量类型 | |
| `adCluster.sdkType` | sdk类型 | |

**注意**：Operation 策略中 `adCluster.appId` 对应 KwaiBI 的 `联盟媒体ID`（不是"应用id"），`adCluster.posId` 对应 `广告位`（不是"广告位id"）。这是最常出错的映射点。

---

## 5. 推荐调用顺序

1. `GET /api/v1/dataset/detail?id=` → 建 `name → {id,type,…}` 表  
2. 把用户指标/维度名（含别名）解析成 fields  
3. 拼 filters + timeFilter  
4. `POST /api/v1/data/query`  
5. 按 columns + dataList 制表  

---

## 5. ⚠️ 跨数据集字段 id 不同

**85587 和 129496 同名字段的 id 完全不同**，绝对不能混用。高频同名字段对照：

| 字段名 | 85587 id | 129496 id |
|--------|----------|-----------|
| 联盟总消耗 | 1329556 | 2215299 |
| 素材CPM | 1329819 | 2215373 |
| CTR | 1329816 | 2215323 |
| CVR | 1330674 | 2215386 |
| CPA | 1330673 | 2215371 |
| 开发者id | 1329558 | 2212697 |
| 广告位 | 1329667 | 2212707 |
| 产品名称 | 1329640 | 2215361 |
| 广告场景 | 1329561 | 2212699 |
| 联盟媒体ID | 2313961 | 2313479 |
| __time | 1329534 | 2211389 |

**规则**：构造 DataQueryVO 时，`fields[].id` 和 `filters[].field.id` 必须与 `dataSetId` 对应。不确定时先调 detail 接口确认。

---

## 6. 高频查询模板

以下模板可直接替换 `@placeholder` 使用，省去从零构造的出错概率。

### 模板 A：按开发者维度查消耗/CPM/成本率（7 日）

```json
{
  "dataSetId": 85587,
  "type": 1,
  "fields": [
    {"id": 1329558, "name": "开发者id", "type": "dimension", "dataSetId": 85587},
    {"id": 1329556, "name": "联盟总消耗", "type": "metric", "dataSetId": 85587},
    {"id": 1329819, "name": "素材CPM", "type": "metric", "dataSetId": 85587},
    {"id": 2344701, "name": "暗投消耗占比", "type": "metric", "dataSetId": 85587},
    {"id": 1329816, "name": "CTR", "type": "metric", "dataSetId": 85587}
  ],
  "filters": [
    {"field": {"id": 1329558, "name": "开发者id", "type": "dimension", "dataSetId": 85587}, "operator": "IN", "value": ["@uid1", "@uid2"]}
  ],
  "timeFilter": {"fieldName": "__time", "startTime": "@7d_ago_ms", "endTime": "@today_end_ms"},
  "limit": 100,
  "rowLimit": 100
}
```

### 模板 B：按广告位维度查填充/曝光率

```json
{
  "dataSetId": 85587,
  "type": 1,
  "fields": [
    {"id": 1329667, "name": "广告位", "type": "dimension", "dataSetId": 85587},
    {"id": 1329556, "name": "联盟总消耗", "type": "metric", "dataSetId": 85587},
    {"id": 1330671, "name": "广告填充率", "type": "metric", "dataSetId": 85587},
    {"id": 1373145, "name": "广告曝光率", "type": "metric", "dataSetId": 85587}
  ],
  "filters": [
    {"field": {"id": 1329667, "name": "广告位", "type": "dimension", "dataSetId": 85587}, "operator": "IN", "value": ["@pos1"]}
  ],
  "timeFilter": {"fieldName": "__time", "startTime": "@7d_ago_ms", "endTime": "@today_end_ms"},
  "limit": 100,
  "rowLimit": 100
}
```

### 模板 C：按产品维度查消耗趋势（日粒度）

```json
{
  "dataSetId": 85587,
  "type": 1,
  "fields": [
    {"id": 1329534, "name": "__time", "type": "dimension", "dataSetId": 85587},
    {"id": 1329640, "name": "产品名称", "type": "dimension", "dataSetId": 85587},
    {"id": 1329556, "name": "联盟总消耗", "type": "metric", "dataSetId": 85587}
  ],
  "filters": [],
  "timeFilter": {"fieldName": "__time", "startTime": "@7d_ago_ms", "endTime": "@today_end_ms"},
  "limit": 500,
  "rowLimit": 500
}
```

---

## 7. Cookie 失效恢复

KwaiBI 401 时的排查路径：

1. 确认 Cookie 域名：KwaiBI 需要 `bi-platform.example.corp` 域的 Cookie，**不是** Operation 的 `ops-platform.example.corp`
2. 确认 Cookie 含 `kwaibi-user-token` 或 accessproxy 相关字段
3. 仍 401 → SSO 登录已过期，需重新登录（调 `kuaishou-sso-login-client` skill）
4. **绝对不要**用 Operation/Orient 的 Cookie 调 KwaiBI，必然 401

Operation 401 时的排查路径：

1. Cookie 域名须为 `ops-platform.example.corp`
2. 仍 401 → 同上，重新 SSO 登录

---

## 已知坑

1. 错路径 `/api/v1/dataset/data/query` → 404
2. Orient Cookie 调 KwaiBI → 401
3. fields 缺真实 `id` → 上游失败
4. 实时集 `129496` 勿用于纯历史 T-1 口径
5. **85587 与 129496 同名字段 id 不同** → 混用必报错（见 §5）
6. **"应用id"和"广告位id"不存在** → 正确字段名是 `联盟媒体ID` 和 `广告位`（见 §4.2）
7. **"暗投成本率"有歧义** → 日常用 `暗投消耗占比`，预期口径用 `暗投成本率`（见 §4.1）
