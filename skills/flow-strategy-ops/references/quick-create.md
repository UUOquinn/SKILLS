# 快速创建：四要素映射参考

用户给出「顶价方式 / 名单维度及 ID / 系数 / 时间」四要素时，按本表直接映射成一条 create 命令。

## 顶价方式 → type/scope

| 用户说法 | 映射 | create 参数 |
|---|---|---|
| 媒体顶价 | type=3（顶价），scope=2 | `"type":3,"scope":2` |
| 广告主顶价 | type=3（顶价），scope=1 | `"type":3,"scope":1` |
| 广告主×媒体顶价 | type=3（顶价），scope=3 | `"type":3,"scope":3` |

## 名单维度及 ID → cluster 字段

| 用户说法 | 所属模板 | create 参数 |
|---|---|---|
| 应用ID | 媒体信息 mediaCluster.appId | `"mediaCluster":{"isAllMedia":false,"appId":"1003400006","osType":0}` |
| 媒体用户ID | 媒体信息 mediaCluster.uid | `"mediaCluster":{"isAllMedia":false,"uid":"..."}` |
| 广告位ID | 媒体信息 mediaCluster.posId | `"mediaCluster":{"isAllMedia":false,"posId":"..."}` |
| 账户ID | 广告主信息 adCluster.accountId | `"adCluster":{"industryVersion":"6.6","accountId":"..."}` |

## 系数与时间

| 用户说法 | 映射 | create 参数 |
|---|---|---|
| 系数：1.2 | bonusRate | `"bonusRate":1.2,"strategyDetails":"1.2"` |
| 时间：1月 / 一周 / 一天 | 起始=当前时间，结束=按语义推算 | `"beginTime":<now_ms>,"endTime":<calc_ms>` |

时间语义：月=30天、周=7天、日=24小时，从当前时刻起算；目标日期按当地时区推算。
用 `date -d` 计算相对时间，禁止心算。

## 创建规则（踩坑固化的硬约束）

- **不要带空的 cluster 对象**：scope=2 只传 mediaCluster，不要附 `"adCluster":{}`，
  否则服务端报「生效范围不能为空」；scope=1 同理只传 adCluster；scope=3 才两个都传。
- 名称默认按 `媒体顶价<系数>-AI创建-<ID>` 拼，用户指定名称则用用户的。
- 创建后把策略 ID 和「待提交」状态告知用户，**不自动提审**；是否继续提审由用户决定。
- 名称冲突（「名称已存在」）时改名重试：追加 -2、-3 递增。

## 完整示例

```bash
# 媒体顶价 + 应用ID 1003400006 + 系数1.2 + 一个月
uv run <skill_directory>/scripts/flow_api.py create \
  '{"name":"媒体顶价1.2-AI创建-1003400006","type":3,"scope":2,"beginTime":<now_ms>,"endTime":<now+30d_ms>,"bonusRate":1.2,"strategyDetails":"1.2","mediaCluster":{"isAllMedia":false,"appId":"1003400006","osType":0},"background":"AI创建：媒体顶价，应用ID 1003400006，系数1.2，时长一个月"}'
```
