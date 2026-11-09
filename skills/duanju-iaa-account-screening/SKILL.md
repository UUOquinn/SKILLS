---
name: duanju-iaa-account-screening
description: 筛选短剧 IAA 账户（含非番茄与番茄两套口径），三步/五步只读取数均直连 OLAP 并固化为脚本。非番茄：第一步按可投范围七猫、爱奇艺、番茄筛账户；第二步从第一步账户与 ConfigService 暗投白名单之外的账户里，按客户维度条件筛出账户清单；第三步把第二步账户放入实时效果数据集123403与客户看板68012103。番茄：第一步大池子（同非番茄）；第二步排除ConfigService与客户维度；第三步累加排除消耗10000到30000且超成本率低于0.15；第四步123403实时数据消耗大于500；第五步客户看板链出。当用户说非番茄短剧账户筛选、番茄短剧账户筛选、筛选番茄、205802取账户、暗投白名单排除、客户维度数据筛选、账户放123403或客户看板时使用。不触发短剧IAA后续取数/写回、实时滚动窗口、调价、其他数据集或看板修改。
---

# 短剧 IAA 账户筛选（第一步 + 第二步）

> 业务称谓：**非番茄短剧账户ID筛选**。本链路产出非七猫/爱奇艺/番茄版权方（其他版权方/版权方为空）、客户名排除番茄系与书读的短剧 IAA 账户。

只读查询数据集 `205802`，直连 `https://app.example.corp/api/v1/data/query`。请求模板从用户给定的 [BI 看板](https://bi.example.corp/pc/dashboard/preview?dashboardId=2060503&insight-chart=7288287&sheetId=140171&tabIds=864253)（第一步 insightConfigId=66613605，第二步 insightConfigId=67782222）捕获并实测；不使用对话式取数，也不改看板。

## 第一步：筛选账户

```bash
uv run <skill_directory>/scripts/screen_accounts.py
```

默认窗口为**北京时间上一完整自然日**（00:00:00—23:59:59.999）。历史对账可传 `--date YYYY-MM-DD`，日期必须为北京时间的自然日；只补跑单个可投范围用 `--item 七猫`、`--item 爱奇艺` 或 `--item 番茄`，不接受任意标签或账户 ID。脚本只调用一次只读查询并在本地处理全部结果；正常模式一次覆盖三个范围，`--item` 是一个范围的独立补跑入口。

筛选口径沿用看板：`可投范围` 是由版权方、销售主体、运营定义客户名映射的计算列，不是简单的“产品名称”筛选；同时限定营销目标为“快手号推广-短剧推广”、转化目标为“首日变现ROI（短剧IAA专用）”。`总消耗 > 0.1` 是按「可投范围×账户ID」聚合后的结果过滤，**不是**原始明细行的筛选。模板在 `references/olap_query_template.json`，脚本每次重写日期过滤并生成新查询 ID，不要手工删改计算列。

输出至工作区 `tmp/duanju_iaa_<日期>_accounts.csv`（账户 ID 去重清单）和 `tmp/duanju_iaa_<日期>_details.csv`（范围×账户、原始精度消耗）；单范围补跑在文件名中增加范围拼音。以脚本标准 JSON 最后一行的 `data` 获取路径、行数、去重账户数、范围分布和总消耗。数值单元格取原始值 `r`，不可用带格式或四舍五入的 `v` 做比较与合计；接口若截断、行数不符、字段变化或条件不满足，则报错而非交付不完整名单。

## 第二步：客户维度筛选（含暗投白名单排除）

```bash
uv run <skill_directory>/scripts/screen_step2.py
```

窗口同样为**北京时间上一完整自然日**，可传 `--date YYYY-MM-DD` 对账。脚本按固定顺序串行执行：

1. 用第一步模板查询同一自然日，得到第一步账户集；
2. 读 ConfigService `ad.demo.chainGrayConfig` 的 production 配置，解析出 `ab_key=enable_chain_antou_30_990_0` 的 `account_white_list`；
3. 两者取并集，以 `账户ID NOT IN` 注入第二步查询（模板 `references/step2_query_template.json`），一次只读查询；
4. 本地复核每一行满足全部条件，任一不满足即报错，不交付不完整名单。

第二步固定条件（与看板一致）：时间=上一完整自然日；转化目标 IN 首日变现ROI（短剧IAA专用）；可投范围 IN 其他版权方、版权方为空；集团 NOT IN 硕才；运营定义客户名 NOT IN 番茄、番茄漫剧、阿里非电商、漫森长沙、漫森二部、漫森三部、山河、美偲、亿汇、云栖、鑫杰光年 且 NOT CONTAIN 书读；产品名称 NOT IN 月光阅读、青青剧场微短剧、快乐说漫；结果过滤：总消耗 >2000 且 <20000、变现ROI-iaa >0.85、超成本率 <0.15。ConfigService 配置内容带 `//` 注释，脚本会剥离注释后解析，每次运行记录 ConfigService version、snapshotId 用于审计。

输出至工作区 `tmp/duanju_iaa_<日期>_step2_accounts.csv`（去重账户）与 `tmp/duanju_iaa_<日期>_step2_details.csv`（客户维度明细）。排除集为空时报错，防止误把口径保护当成功。第二步可以合法返回 0 行（排除后无命中），此时以标准 JSON 中 `row_count: 0` 为准。

## 第三步：账户 → 实时数据 + 客户看板

```bash
uv run <skill_directory>/scripts/screen_step3.py
```

默认窗口为**北京时间上一完整自然日**，可传 `--date YYYY-MM-DD` 对账。账户来源默认在脚本内重跑第二步（保证口径一致），也可用 `--accounts-file` 指定已生成的第二步账户 CSV。脚本固定执行两个只读查询：

1. 数据集 `123403`（效果广告-流量-投放效果(实时)）：把账户以 `账户ID IN` 注入保存分享（shareId=67209414）的模板，保留模板内固定条件（转化目标=首日变现ROI（短剧IAA专用）、客户一级行业=短剧小说、总消耗>250）；输出含时间、账户ID、总消耗、联盟总消耗、联盟占比、超成本率、平均浅度出价、(浅度)转化数、联盟转化数、当日广告变现ROI。
2. 看板 `68012103`（数据集 205802，客户维度）：同样以 `账户ID IN` 注入模板，保留看板固定条件（营销目标=快手号推广-短剧推广、转化目标、可投范围=其他版权方/版权方为空、集团≠硕才、产品/客户名排除、总消耗 2000 至 20000、ROI>0.85、超成本率<15%）；输出集团、运营定义客户名、产品名称、账户ID、总消耗、变现ROI-iaa、超成本率，**该表即发给客户看的清单**。

模板在 `references/step3_123403_template.json` 与 `references/step3_68012103_template.json`；每次运行重写时间与账户列表并生成新查询 ID。两个查询都会本地复核：返回账户必须全部来自输入账户集，数值与筛选边界必须满足，重复的账户×时间行视为异常。个别账户在数据集中无数据属正常，标准 JSON 的 `missing_accounts` 列出未覆盖账户。

输出至工作区 `tmp/duanju_iaa_<日期>_step3_123403.csv` 与 `tmp/duanju_iaa_<日期>_step3_client_dashboard.csv`（中文表头，UTF-8 BOM，可直接发给客户）。

### 交付样式（固定）

脚本成功时在 stdout 打印 `===DELIVERY_SUMMARY===` 至 `===END_DELIVERY_SUMMARY===` 之间的固定汇总文本（第一步/第二步行数与排除口径、第三步两个查询的行数/账户数/总消耗与缺数、交付文件名）。Agent 把该块内容转述给用户，**不需要看板回转**：不要新建 insight 分享、不要改看板筛选器、不要回传看板链接（2026-10-20 用户确认）。末尾的标准 JSON 仍是机器契约，勿以交付块替代。交付文件用 JSON `delivery` 字段或交付块内写死的 `<FILE>tmp/xxx.csv</FILE>` 链接，照抄即可，禁止输出绝对路径。

## 番茄链路：五步筛选（新增能力）

> 业务称谓：**番茄短剧账户ID筛选**。从七猫/爱奇艺/番茄大池子逐步排除后，按高消耗+低超成本率筛选出符合客户看板口径的账户清单。看板 insightConfigId：第一步 66613605、第二步 67460889、第三步 68016585、第四步 adsdata shareId=67461641、第五步 68183973。

### 番茄第一步（`scripts/tomato_screen_step1.py`）

与大池子筛账户相同（复用非番茄第一步口径），产出 `tmp/tomato_iaa_<日期>_accounts.csv`。

```bash
uv run <skill_directory>/scripts/tomato_screen_step1.py [--date YYYY-MM-DD] [--item 七猫|爱奇艺|番茄]
```

### 番茄第二步（`scripts/tomato_screen_step2.py`）

排除第一步账户 + ConfigService 暗投白名单（`ab_key=enable_chain_antou_30_990_0` 的 `account_white_list`）后，按固定客户维度条件筛选：营销目标=快手号推广-短剧推广、转化目标=首日变现ROI（短剧IAA专用）、可投范围=其他版权方/版权方为空、产品名称排除青青剧场微短剧/快乐说漫/月光阅读、客户名排除番茄/番茄漫剧/阿里非电商/漫森长沙/漫森二部/漫森三部/山河/美偲/亿汇/云栖/鑫杰光年+不含书读、总消耗大于0.1。产出 `tmp/tomato_iaa_<日期>_step2_accounts.csv`。

```bash
uv run <skill_directory>/scripts/tomato_screen_step2.py [--date YYYY-MM-DD]
```

### 番茄第三步（`scripts/tomato_screen_step3.py`）

累加排除第二步账户，按总消耗 10000~30000、超成本率小于0.15 筛选高消耗账户。产出 `tmp/tomato_iaa_<日期>_step3_accounts.csv`。

```bash
uv run <skill_directory>/scripts/tomato_screen_step3.py [--date YYYY-MM-DD] [--accounts-file <step2 csv>]
```

### 番茄第四步（`scripts/tomato_screen_step4.py`）

将第三步账户注入数据集 123403 实时数据查询，保留总消耗大于500 的账户。**必须传 --accounts-file**。产出 `tmp/tomato_iaa_<日期>_step4_123403.csv`。

```bash
uv run <skill_directory>/scripts/tomato_screen_step4.py [--date YYYY-MM-DD] --accounts-file <step3 csv>
```

### 番茄第五步（`scripts/tomato_screen_step5.py`）

将第四步账户注入看板 68183973（数据集 205802）全字段查询（集团、客户名、产品名、账户ID、总消耗、变现ROI-iaa、超成本率等），**该表即发给客户看的清单**。**必须传 --accounts-file**。产出 `tmp/tomato_iaa_<日期>_step5_client_dashboard.csv`。

```bash
uv run <skill_directory>/scripts/tomato_screen_step5.py [--date YYYY-MM-DD] --accounts-file <step4 csv>
```

番茄模板在 `references/tomato_step1~5_template.json`；番茄脚本共用 `scripts/tomato_common.py`（含 ConfigService 暗投白名单解析、公共查询函数）。交付样式与非番茄一致（固定汇总文本 + 看板不回转）。

## 批量能力评估

用户已确认需要一次筛选**所有命中账户**。三步均只读、单查询接口调用；第一步按三个可投范围（最多 3 个枚举项）支持 `--item` 补跑，第二步为单次查询无单项维度，第三步按账户集注入两个查询。不做并发写入、策略调整或编排续接。若将来某范围的记录数达到模板行上限而被截断，本流程停止并报告，不宣称已筛全。

执行失败时先读 [错误处理](references/error-handling.md)，按脚本退出码及最后一行 JSON 处理；权限问题先处理目标 OLAP 域名的 Auth，不降级到另一个查询接口。

<!-- skill-creator: askuser-with-text-fallback-v1 -->

## 用户交互：AskUser 优先与文本降级

凡需向用户收集信息、请求确认或要求选择时，优先调用 `askUser` 工具，并以结构化问题和可选项取得答复；关联问题应合并提问。

若 `askUser` 在当前运行环境不存在、调用失败或返回无效结果，才允许降级为普通聊天文本提问。降级时必须明确说明“当前 AskUser 不可用，以下为兼容提问”，并在收到用户明确答复前停止后续依赖该答复的操作；不得猜测、默认代填或把沉默视为同意。
