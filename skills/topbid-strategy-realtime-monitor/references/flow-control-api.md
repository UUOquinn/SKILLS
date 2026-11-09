# boostControl 顶价策略 API 契约

基址：`https://ops.example.corp/ops/rest/boostControl`

鉴权：`SmartAuthSession` 代表当前用户直连内网 REST API，自动复用/刷新 Auth。不保存 Cookie/Token，不走网页 UI。

## 顶价类型与维度（实测映射，勿猜）

| 字段 | 值 | 含义 |
|---|---|---|
| `type` | 3 | 顶价（CPM 加系数） |
| `type` | 5 | CPA顶价 |
| `type` | 1 / 4 / 10 | 绿通 / 打折 / 索引加白（**非顶价，本 skill 跳过**） |
| `scope` | 1 | 广告主顶价（仅 `adCluster`） |
| `scope` | 2 | 媒体顶价（仅 `mediaCluster`） |
| `scope` | 3 | 广告主*媒体顶价（双维度） |

顶价系数字段：`bonusRate`（float）。

## 读策略

```
GET /boostControl/get?id={strategyId}
```

返回详情含 `id` / `name` / `type` / `typeDesc` / `scope` / `bonusRate` /
`adCluster` / `mediaCluster` / `status` 等。策略不存在时返回成功壳但无 `id`，
需识别为 `NOT_FOUND`。

## 撤回

```
POST /boostControl/withdraw
{"id": strategyId}
```

（与 operation-bi 的 `/{prefix}/withdraw` 同源，body 带策略 id。）

## 降系数

```
POST /boostControl/submitEdit
```

先 GET 全量详情 → 合并 patch → 剔除只读字段再提交：

- 剔除：`status` / `statusDesc` / `creatorName` / `createTime` / `updateTime` /
  `isDel` / `lastStatus` / `greyStartTime` / `expireSoon` / `snapshot` /
  `toolBar` / `display*Btn` / `ruleTypes` / `rangeTime` / `reason` / `strategyDetails`
- `adCluster.maxNum` / `mediaCluster.maxNum` 只读壳剔除
- `adCluster.industryVersion` 补 `"6.6"`
- `background` 非空（GET 常缺，用 name 兜底）
- 新 `bonusRate = 原值 - 降幅`，封底 `0.01`

## 策略维度 → OLAP 维度映射

| 策略字段 | 维度名 | DS_REALTIME fieldId | DS_OFFLINE fieldId |
|---|---|---|---|
| `adCluster.accountId` | 账户ID | 2212690 | 1329545 |
| `adCluster.campaignType` | 营销目标 | 2215345 | 1329622 |
| `adCluster.ocpxActionType` | 转化目标（浅度优化目标） | 2215378 | 1329660 |
| `adCluster.deepConversionType` | 深度目标 | 2215324 | 1329592 |
| `mediaCluster.adStyle` | 广告场景（广告样式） | 2212699 | 1329561 |
| `mediaCluster.appId` | 联盟媒体ID | 2313479 | 2313961 |
| `mediaCluster.uid` | 开发者id | 2212697 | 1329558 |
| `mediaCluster.posId` | 广告位 | 2212707 | 1329667 |

## 指标 fieldId

| 指标 | DS_REALTIME | DS_OFFLINE |
|---|---|---|
| 联盟总消耗 | 2215299 | 1329556 |
| 浅度超成本率 | 2215353 | 1330672 |
| IAA广告变现ROI | 7098032 | 无 |
| 平均CPM顶价系数 | 2983666 | 无 |
| 平均CPA顶价系数 | 2983659 | 无 |
