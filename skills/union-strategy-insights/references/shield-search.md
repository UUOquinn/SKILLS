# 屏蔽反查与行业字典语义

`operation_client.py search` 与 `industry_lookup.py` 的设计依据与已知坑。查询前建议先读本文档理解字段语义。

## 1. 反查目标

回答「某个 appId / uid / posId 被哪些屏蔽策略覆盖，屏蔽了什么行业 / 产品」。

- 输入：一个维度（appId | uid | posId）+ 一个或多个同维度 ID（≤50）
- 输出：每条命中的屏蔽策略：id、name、type（含中文标签）、status、shieldUserType、行业 codes、产品名、命中 ID 列表

## 2. 数据路径

### orientControl（主）

`POST /orientControl/query` 支持**顶层**筛选 key：

| 维度 | query key | mediaCluster 字段 |
|------|-----------|-------------------|
| appId | `appId` | `mediaCluster.appId` |
| uid | `uid` | `mediaCluster.uid` |
| posId | `posId` | `mediaCluster.posId` |

- 只传 key 不传其他字段时，服务端仍可能忽略筛选（实测 `mediaUid` 等拼写变体被忽略，返回全量）；正确 key 验证有效。
- `status=4` 可与维度 key 组合过滤生效策略（但 search 默认返回全部状态，按 status=4 优先排序，避免漏掉待审/灰度版本）。

### mediaControl（兜底）

`POST /mediaControl/query` **忽略所有筛选参数**（实测任意 key 都返回全量 356 条）。
因此 search 对 mediaControl 走「全量拉取 + 本地匹配 mediaCluster」：一次拉全量（约 356 条，≤2 页），
本地对 `mediaCluster.<dim>` 做逗号分隔匹配。mediaControl 里的媒体屏蔽（type=2，老版）是重要的覆盖来源，不能跳过。

### 屏蔽类型

- 屏蔽类：type 2（媒体屏蔽老版）/ 5（标签屏蔽）/ 7（并集屏蔽新屏蔽）/ 10（交集屏蔽）——都有媒体维度
- 非屏蔽类：type 1/3/6/8/9（定投、人群、预算等）——无 mediaCluster，search 直接跳过

## 3. 行业与产品字段语义

策略详情 `adCluster` 中的行业/产品描述字段**不统一**，按 type 与 shieldUserType 变化：

| shieldUserType | 行业/产品字段 | 形态 |
|----------------|--------------|------|
| `secondIndustryV6` | `adCluster.secondIndustryV6` | 逗号分隔的**数字 code**（如 `1028,1029,1030`） |
| `firstIndustry` | `adCluster.firstIndustry` | 逗号分隔的数字 code |
| `product` | `adCluster.defineProduct` / `adCluster.product` | 逗号分隔的**产品名文本** |
| `accountId` / `creativeId` 等 | 对应字段 | 数字 ID（非行业/产品） |

`search` 输出把两者分开：`industryCodes`（数字 code 数组）+ `products`（文本名数组）。
**行业 code 不会自动翻译**，需要名称时用：

```bash
uv run <skill_directory>/scripts/industry_lookup.py lookup --codes 1028,1029,1030
```

注意：标签屏蔽（type=5，如快应用、互动权益卡）通常 `shieldUserType` 是 `shieldFastApplication*` / `shieldEquityCard` 这类标签，既无行业 code 也无产品名，输出为空数组是正常现象——它们屏蔽的是「标签命中的广告」，不是具体行业/产品。

## 4. 行业字典

- 内置：`references/industry-code-name.json`，353 个二级行业 code→名称，提取自联盟策略平台 seed 字典（44 一级 / 364 二级行业体系）。
- 刷新：`industry_lookup.py refresh` 从 `http://10.0.0.1:3000/data/industry-map.json` 拉取合并。该源在内网部分网段不可达，超时/失败时继续用内置字典，不阻塞主流程。
- code 缺失（如新增行业）：lookup 返回 `missing` 列表，把 code 原样展示并标注「字典缺失」。

## 5. 维度分离规则

用户给「不同维度 ID」时**分开调用**，每个维度一次 search：

```bash
uv run <skill_directory>/scripts/operation_client.py search --dim appId --ids 111,222
uv run <skill_directory>/scripts/operation_client.py search --dim uid --ids 333
```

单次调用混入不同维度 ID 会导致误匹配（一个 appId 可能恰好等于某 uid 的值）。这是本 Skill 的强制约束：`--dim` 每次一个维度。

## 6. 已知坑

1. mediaControl 筛选无效 → 全量拉取本地匹配（约 356 条，开销小，可接受）
2. orientControl 顶层筛选 key 拼错（如 `mediaUid`）会被静默忽略，返回全量 —— search 内部用的 key 已验证，不要自行改动
3. 行业 code 与产品名混在同一策略里可能都有（如 type=7 同时 shield product + secondIndustryV6），输出两个字段并列，不要假设互斥
4. status 语义：4 生效中；5 已失效；0 待提交。search 返回全部状态并排序（生效在前），汇报时**必须标注 status**，用户问「被屏蔽」默认指 status=4 的生效策略，但也可能关心待审版本
5. 幂等与只读：search 纯只读，可安全重跑；不存在副作用
