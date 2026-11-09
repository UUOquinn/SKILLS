---
name: olap-pipeline-runner
description: 通用「OLAP 直连取数 → 本地聚合 → 规则决策 → 策略平台逐条写入」四段流水线框架，绕过对话式取数的 LLM 思考开销，取数与聚合在秒级完成。业务规则不写在代码里，由调用方提供 JSON 配置（数据集模板、聚合口径、决策档位、写入字段映射），框架只负责执行管道。当用户提到搭建 OLAP 取数流水线、直连数据集取数不走对话式取数、把取数聚合决策写入串成脚本链路、复用 SSO 直连内网接口取数、或要为某个数据集做定时判断并回写策略平台时使用。不支持批量并发写入（写入为单条接口，循环由调用方驱动）；不内置任何具体业务口径、指标阈值或档位规则；不做策略提审与推全；不替代对话式取数的探索性分析场景。
---

# OLAP Pipeline Runner

把「直连 OLAP 取数 → 本地聚合 → 规则决策 → 逐条写策略平台」这条链路做成可复用管道。**框架不含任何业务逻辑**——数据集、指标口径、决策档位、写入字段全部来自调用方的 JSON 配置。

## 为什么要有这个框架

对话式取数每次调用都要经历一轮 LLM 思考，一个循环跑下来大部分时间花在「想」而不是「取」。直连 OLAP + 本地 Python 聚合把这段压到秒级。

实测（2026-09-16，真实接口，290 个账户 / 26209 原始行）：

| 段 | 耗时 | 是否随数据量增长 |
|---|---|---|
| OLAP 取数 + 本地聚合 | 2.63s | 不增长（一次全量请求） |
| 平台批量查现值建索引 | 1.69s | 不增长（一次全量请求） |
| 规则决策 | 0.08s | 纯本地，可忽略 |
| 单条写入 API 往返 | 中位数 78ms | 每条一次 |

**前三段是约 4.4 秒的固定成本。** 瓶颈只在写入条数。

<!-- skill-creator: askuser-with-text-fallback-v1 -->

## 用户交互：AskUser 优先与文本降级

凡需向用户收集信息、请求确认或要求选择时，优先调用 `askUser` 工具，并以结构化问题和可选项取得答复；关联问题应合并提问。

若 `askUser` 在当前运行环境不存在、调用失败或返回无效结果，才允许降级为普通聊天文本提问。降级时必须明确说明“当前 AskUser 不可用，以下为兼容提问”，并在收到用户明确答复前停止后续依赖该答复的操作；不得猜测、默认代填或把沉默视为同意。

## 不支持批量

写入是**单条接口**，框架不提供并发或脚本内批量循环。需要写 N 条就由调用方驱动 N 次，每次写完立刻记台账。

这不是能力缺失而是刻意约束：写策略是高影响生产操作，逐条落台账才能保证中断可恢复、不重复写。代价是量大时慢——Agent 逐条驱动约 10~30 秒/条，1000 条要数小时。真需要跑到千级，先想清楚是否该走离线批处理而不是这个框架。

## 四段管道

四段相互独立，靠文件传递。每段都能单独跑、单独排查。

```
fetch.py    OLAP 取数 + 本地聚合   →  data/metrics_<时间戳>.csv
enrich.py   拉平台现值建索引     →  回填 CSV 的 current_value 列
evaluate.py 按配置档位决策       →  决策表 + 退出码
write.py    单条写入策略平台     →  平台策略 ID + 台账一行
```

### 1 取数

```bash
uv run <skill_directory>/scripts/fetch.py \
  --config <你的配置>.json \
  --output data/metrics.csv --raw-output data/raw.json
```

窗口默认 `[now-lag-window, now-lag)`，`window_minutes` / `lag_minutes` 在配置里定。右端留白是为了避开实时数据回补延迟——不留会拿到半成品数据。

**`--raw-output` 建议总是带上**：事后对账只能靠它，聚合后的 CSV 验不了自己。

### 2 拉平台现值

```bash
uv run <skill_directory>/scripts/enrich.py \
  --config <你的配置>.json \
  --input data/metrics.csv --output data/bases.json
```

全量拉一次生效策略在本地建索引（实测 1.7 秒扔 474 条），比逐个查快一个量级。

一个对象被多条生效策略同时覆盖是常态，`multi_hit_policy` 控制怎么选：`latest`（按创建时间取最新）、`ask`（停下来问）、`fail`（直接报错）。选 `latest` 时 `chosen_strategy_id` 会记下用的是哪条——**报告里必须带上，否则事后无法回溯**。

### 3 决策

```bash
uv run <skill_directory>/scripts/evaluate.py \
  --config <你的配置>.json \
  --input data/metrics.csv --ledger data/ledger.csv
```

退出码：`0` 全部可执行；`2` 有对象落在护栏外（`HOLD_NEEDS_CONFIRM`，**不要写入**，单列等确认）；`3` 主键重复（先按主键聚合再重跑）。

只有 `status=OK` 才写入。其余状态一律不碰平台：`NO_ACTION`（落在观察区）、`SKIP_NO_DATA`（指标空值）、`SKIP_NOT_TARGET`（汇总行等非业务主键）、`SKIP_COOLDOWN`（开了冷却期且未满）、`HOLD_NEEDS_CONFIRM`。

### 4 写入

```bash
uv run <skill_directory>/scripts/write.py \
  --config <你的配置>.json \
  --target 114368154 --value 0.68 --base 0.70 \
  --name "<策略名>" --ledger data/ledger.csv \
  --metric 0.6714 --window "2026-09-16 11:53~12:08"
```

**先跑 `--dry-run`**：只打印将提交的 body 不真写，确认字段无误再去掉该参数。

写成功后脚本自己记台账，不要另外手写。**写入失败的对象不记台账**——记了下一轮会当成已处理而跳过。

## 配置文件

一份 JSON 定完整业务。框架不读任何硬编码的字段名或阈值。

```json
{
  "name": "my-pipeline",
  "fetch": {
    "template": "references/my_dataset_template.json",
    "window_minutes": 15,
    "lag_minutes": 5,
    "columns": {
      "key": "账户ID",
      "time": "__time",
      "weight": "联盟总消耗",
      "metric": "IAA广告变现ROI"
    },
    "aggregation": "weighted_mean"
  },
  "enrich": {
    "multi_hit_policy": "latest",
    "default_value": 0.6
  },
  "evaluate": {
    "bands": [
      {"max": 0.75, "delta": -0.10},
      {"min": 0.75, "max": 0.80, "delta": -0.02},
      {"min": 0.84, "max": 0.85, "delta": 0},
      {"min": 1.00, "delta": 0.03}
    ],
    "guardrail": {"min": 0.30, "max": 1.00},
    "cooldown_minutes": 0
  },
  "write": {
    "name_template": "ai短剧_iAA_{value}_{window_end:%y.%-m.%-d.%H:%M}",
    "name_max_length": 32,
    "end_time": "end_of_day"
  }
}
```

字段含义、可选值、档位区间的边界语义看 [references/config-schema.md](references/config-schema.md)。

几个容易踩的点：

- **档位区间是左闭右开** `[min, max)`。相邻档位的 `max` 和下一档的 `min` 写相等就不会漏。脚本会检重叠和空隙并报错。
- **`delta: 0` 不等于没配档位**。前者是「有意不动」（`NO_ACTION`，观察区），后者是「规则有洞」（报错）。两者别混。
- **`aggregation` 默认 `weighted_mean`，不是算术平均**。例：`7800@0.89` 与 `2400@0.71` 加权得 `0.8476`（观察区，不动），算术平均得 `0.80`（会动）——**结论相反**。口径选错不会报错，只会静默算错。
- **`name_template` 必须带长度上限**。平台对策略名有硬限制，超了直接报 `size must be between 0 and N`，中文按字符计。

## 硬约束

- **模板里的日期 filter 必须让框架重写，不要手改**。从网页 HAR 抓下来的模板里写死了抓包当天的日期，不重写就会从次日零点开始静默取到旧数据——**接口照样返回 200、数据完整、格式正确**，你完全看不出来。这个坑真实发生过。`fetch.py` 每次都按取数窗口重写它，模板里找不到该字段就直接报错而不是静默继续。
- **取单元格的 `r` 而不是 `v`**。OLAP 返回 `{"v": "25元", "r": 24.997}`，`v` 是带单位的展示值且被四舍五入过。拿 `v` 去算要么因为「元」「%」后缀直接崩，要么精度被污染。
- **每轮重新拉平台现值，不要复用上一轮**。上一轮给无基数对象新建了基数，这一轮它们已经有了；拿旧文件跑会再次尝试新建，平台报重复。
- **台账是跨轮次唯一的状态**。只追加不改写，不要手写。中断后重跑先读台账跳过已成功的，否则会给同一对象建出两条。靠台账，不靠记忆或聊天记录。
- **护栏撞了就停，不要改护栏参数让它通过**。看到大量 `HOLD_NEEDS_CONFIRM` 是安全网在拦，不是参数设小了。
- **SSO 失效、连续两轮取不到数、平台连续写入失败——立即暂停并报告**，不要静默跳过。这类问题不解决，后面每轮都是空转。
- **窗口长度大于触发间隔时必须开冷却期**。两轮数据重叠时不开，同一批数据会被重复消费，对同一对象反复下手。等长或更短时零重叠，`cooldown_minutes: 0` 即可。

## 参考文档

| 文件 | 何时读 |
|---|---|
| [references/config-schema.md](references/config-schema.md) | 写或改 pipeline 配置时 |
| [references/olap-api.md](references/olap-api.md) | 抓新数据集模板、字段对不上、响应结构有疑问时 |
| [references/error-handling.md](references/error-handling.md) | **任何脚本报错先读这里**，不要自行发挥 |
