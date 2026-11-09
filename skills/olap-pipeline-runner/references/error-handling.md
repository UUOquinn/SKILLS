# Error Handling — 错题本

**执行任何脚本遇到报错，先在这里查表，按 `code` 和 `next_action` 执行。禁止跳过查表自行发挥**——特别是：不要改写脚本绕过报错、不要从别的 Skill 拷代码、不要降级到 `python3` / `pip`。

两层结果都要看：

- **进程退出码**：`0` 成功 / `1` 失败 / `2` 权限或需询问 / `3` 输入数据问题 / `130` SIGINT
- **stdout 最后一行 JSON**：`ok` / `code` / `error` / `msg` / `next_action`

两者冲突时**以非零退出码为准**，不要拿 `ok=true` 覆盖进程失败。

## 速查表

| error | 退出码 | 动作 |
|---|---|---|
| `AUTH_EXPIRED` | 2 | 引导用户重新完成内网认证，认证后重跑该段。不要重试 |
| `AUTH_FAILED` | 2 | 账号无权限。告知用户需要申请看板 / 策略平台权限，停止 |
| `CONFIG_MISSING` | 1 | 配置文件路径不对，确认后重跑 |
| `CONFIG_INVALID` | 1 | 配置字段非法，按 `msg` 修正 `config-schema.md` 对应字段 |
| `TEMPLATE_MISSING` | 1 | 找不到 OLAP 模板，确认 `fetch.template` 路径（相对配置文件目录） |
| `EMPTY_WINDOW` | 1 | 窗口内无数据。**先查是不是日期 filter 问题**，见下 |
| `INPUT_MISSING` / `INPUT_EMPTY` | 1 | 上一段没产出，回去看上一段的返回 |
| `DUPLICATE_KEY` | 3 | 主键重复。先按主键聚合再重跑，否则会给同一对象写两条 |
| `BAND_GAP` | 1 | 有对象落不进任何档位，规则有洞。补全 `evaluate.bands` 再跑 |
| `WRITE_REJECTED` | 1 | 平台拒绝写入。该对象**未记台账**，修正后下一轮会重试 |
| `UNEXPECTED_ERROR` | 1 | 未预期异常，读 stderr 的类型和栈，不要盲目重试 |

`evaluate.py` 返回 `code=2` 且 `ok=true` 是**正常情况**：表示有对象落在护栏外（`HOLD_NEEDS_CONFIRM`）。这些对象不要写入，在报告里单列等确认。

## 具体场景

### AUTH_EXPIRED

隔夜、长时间空闲后第一次跑几乎必然遇到。

**动作**：调用 `kuaishou-sso-login-client` 完成认证，成功后重跑那一段。不要在同一段上反复重试——认证没恢复重试多少次都是同一个错。

### EMPTY_WINDOW — 先排除日期 filter 问题

看到「窗口内没有任何数据」时，**不要第一反应认为是数据延迟**。按顺序查：

1. **看原始响应里的日期**。带 `--raw-output` 重跑，检查 `__time` 落在哪天：

   ```bash
   uv run <skill_directory>/scripts/fetch.py --config <cfg> \
     --output /tmp/x.csv --raw-output /tmp/raw.json
   ```

   如果 `__time` 全是**昨天或更早**，说明日期 filter 没被正确重写——这是配置或模板结构问题，不是数据延迟。

2. **确认模板里有日期 filter**。`filters[].filterValue.startTime/endTime` 被删掉或改过结构时，脚本会抛错而不是静默取错日期；如果没抛错但日期不对，检查是不是有多组 filter。

3. **加大 `lag_minutes` 试试**。确认日期正确后，才考虑数据回补延迟：

   ```bash
   uv run <skill_directory>/scripts/fetch.py --config <cfg> --lag-minutes 20 ...
   ```

4. **连续两轮取不到数就暂停并报告**，不要静默跳过继续跑。这类问题不解决，后面每轮都是空转。

### WRITE_REJECTED

平台拒绝写入时，脚本**不会记台账**。这是刻意的：记了下一轮会当成已处理而跳过，那条策略就永久漏掉了。

常见原因看 `msg`：

| 平台报错 | 原因 |
|---|---|
| `size must be between 0 and N` | 策略名或 background 超长，中文按字符计。压缩 `name_template` |
| 策略名已存在 | 同名冲突，在名称末尾加序号 |
| 参数校验失败 | `write.fields` 映射对不上该策略类型，抓包核对字段名 |

单个对象失败就记下原始报错、继续下一个，最后在报告的「未执行」里列出。**不要卡在那里反复重试**。

### DUPLICATE_KEY

第 1 段输出里同一主键出现多次。原因通常是聚合口径配错——比如漏了 `columns.key`，或数据集本身有多个维度导致同一主键多行。

不要手动删重复行，回去修 `fetch.columns` 让聚合真正按主键收敛。

### BAND_GAP

`evaluate.bands` 有断层，某些值落不进任何档位。

**为什么这是错误而不是警告**：不校验的话，落进空隙的对象会静默变成「不动」，跟观察区（`delta: 0`）的表现完全一样。你会以为规则在正常工作，实际上有一整段值域被漏掉了。

按 `msg` 里给的区间补全档位，相邻档位的 `max` 和下一档的 `min` 写相等。

## 什么时候必须暂停整个流程

这几种情况不解决，后面每轮都是空转，不要静默跳过：

- Auth 失效
- 连续两轮 `EMPTY_WINDOW`
- 平台连续写入失败
- 大量 `HOLD_NEEDS_CONFIRM`——这是护栏在拦，**不要改护栏参数让它通过**，那是在拆安全网
