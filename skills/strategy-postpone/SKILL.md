---
name: strategy-postpone
description: >
  从「@快手联盟」到期提醒消息中自动识别策略 ID，将 endTime 顺延 1 个月并提审。
  Use when 策略延期、还有N天到期、请及时关注处理、id=、mergeEdit、mergeEditV2、
  托管延期、快手联盟到期提醒、短剧/小说、开屏屏蔽、orientControl、flowControl、
  darkControl、mediaControl。
  不唤醒：只查询策略详情（用 operation-kwaibi-1-1）、手动指定新结束时间、
  非到期提醒场景、加入托管自动续期（另行告知）。
---

# 策略延期（到期提醒 → 识别 ID → +1 月 → 提审）

用户把 Kim / 消息里的**到期提醒原文**贴过来时（包括 `@快手联盟 策略延期` 命令），  
自动抽 ID → GET 详情 → `endTime +1 月` → 直接调策略平台 API 提审。  
**默认立即执行，不询问"是否延期"。**

<!-- skill-creator: askuser-with-text-fallback-v1 -->

## 用户交互：AskUser 优先与文本降级

凡需向用户收集信息、请求确认或要求选择时，优先调用 `askUser` 工具，并以结构化问题和可选项取得答复；关联问题应合并提问。

若 `askUser` 在当前运行环境不存在、调用失败或返回无效结果，才允许降级为普通聊天文本提问。降级时必须明确说明"当前 AskUser 不可用，以下为兼容提问"，并在收到用户明确答复前停止后续依赖该答复的操作；不得猜测、默认代填或把沉默视为同意。

## 执行流程

1. **抽 ID**：从用户输入中提取策略 ID（见下方规则），去重保序，输出确认行：`将延期并提审：15392, 382`
2. **逐条处理**：调用脚本对每个 ID 执行 GET → 改 endTime → 提交
3. **汇总输出**：以表格形式展示每条结果

## 批量能力评估结论

本技能已完成批量能力评估（`enable_orchestrated`），结论如下：

- **execution_mode**：`orchestrated`（存在写副作用，串行逐条处理）
- **上限**：`max_items=50`，超出请拆分提交
- **Normal/Batch 分流**：
  - 普通模式（Normal，单条）：`--item <id>` 直接处理，不走编排
  - 批量模式（Batch，多条）：`--ids <id1,id2,...>` 串行编排，带检查点与汇总
- **单项重试**：支持 `--item <id>` 独立补跑失败条目
- **幂等回查**：支持 `--check-action <action_id>` 向策略平台确认是否已成功提审
- **运行参数**：`--concurrency`（默认 1，串行）、`--batch-size`（默认 10）、`--max-runtime`（默认 300s）、`--authoritative-source`（默认 ops-platform）

## 调用脚本

```bash
# 批量模式（多条）
uv run <skill_directory>/scripts/postpone.py --ids "15392,382"

# 普通模式（单条 / 补跑）
uv run <skill_directory>/scripts/postpone.py --item 15392

# 回查是否已提审
uv run <skill_directory>/scripts/postpone.py --item 15392 --check-action <action_id>

# 只查不提交
uv run <skill_directory>/scripts/postpone.py --ids "15392,382" --dry-run
```

脚本直接调用策略平台 REST API（`https://ops-platform.example.corp`），SSO 认证自动管理。

## 抽 ID 规则（AI 执行，不靠脚本）

| 优先级 | 规则 | 示例 |
|--------|------|------|
| 1 | `id\s*=\s*(\d+)` | `id = 15392` |
| 2 | `策略[ID#＃:\s]*(\d{3,})` | `策略ID 382` |
| 3 | 用户直接给数字列表 | `15392 382` |

忽略「还有 5 天」里的天数数字。抽完先回确认行，再执行。

## 输出格式

脚本已把输出模板写死（制表符分隔），stdout 直接输出：

```
策略	原结束	新结束
16042 点众快看指定屏蔽	2026-11-08	2026-12-08
16040 短剧小说_2231_uid	2026-11-08	2026-12-08
```

非成功条目会在「新结束」后追加「状态：原因」备注。AI 拿到脚本 stdout 后直接转发表格即可，无需自行拼装。

## 失败处理

详见 `references/error-handling.md`。关键规则：
- 401/SSO 过期 → 停止，引导重新登录
- STRATEGY_NOT_FOUND → 记失败，继续下一条
- 单条连续失败 2 次 → 跳过，记入失败表
- API 412（行业版本校验）→ 已通过 `adCluster.industryVersion` 解决；若平台升级导致重现，需同步调整脚本中 `INDUSTRY_VERSION` 常量

## 提交体关键处理（务必保留）

`sanitize_body` 中四个必需步骤，缺一个就会提交失败：

1. **定向策略只保留白名单字段** —— 多传只读字段容易 412
2. **删除 `adCluster.maxNum` / `mediaCluster.maxNum`** —— 这些是服务端 `Kconf<T>` 抽象类型，GET 能输出但 POST 无法反序列列化（500）
3. **写入 `adCluster.industryVersion = "6.6"`** —— 不带则 412；GET 不返回此字段，也无单独查询接口，只能本地硬编码维护
4. **`background` 为空时补值** —— 用策略名或 "延期续期" 兑底，避免必填校验失败

## 边界说明

- 本技能**不要求**策略已在托管表
- 用户说「加进托管 / 以后自动延期」→ 告知另有托管接口，询问是否追加操作
- **本技能仅通过 REST API 提交，不使用浏览器自动化**
