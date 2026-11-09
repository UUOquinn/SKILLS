# API Reference — 实时审核接口清单

> 来源：联盟诊断工作台 `http://10.0.0.1:3000/` 前端 `strategy-audit.js`（v5.89）+ 实测验证。
> 统一响应包装：`{"success": true, "data": {...}}` 或 `{"success": false, "error": "..."}`。

## 接口清单

| 接口 | 方法 | 用途 | 关键入参 |
|------|------|------|---------|
| `/api/strategy/audit/get` | POST | 拉取策略详情 | `{id}` |
| `/api/strategy/audit/flow` | POST | 审核全流程（通过→发布→条件推全），含封禁期流程 | `{id, mode}`；mode: `normal`/`ban`/`publish_replay`；可选 `creatorId`/`approveId` |
| `/api/strategy/audit/changeStatus` | POST | 单步状态流转 | `{id, status, reason}`；可选 `creatorId`/`approveId` |
| `/api/strategy/audit/pushFull` | POST | 立即推全（含收敛轮询） | `{id, mode}` |
| `/api/strategy/audit/users` | GET | 审核人列表（约 400 人） | 无 |
| `/api/strategy/audit/queryByCreator` | POST | 按创建人查待审单 | `{creatorId, status}`（status=1 待审） |
| `/api/cookie/status` | GET | 服务端 Playwright 状态 | 无（本 skill 不依赖） |

## 状态流转码（changeStatus）

| status 码 | 含义 |
|-----------|------|
| 6 | 审核通过 |
| 3 | 审核驳回 |
| 2 | 同意发布 |
| 7 | 拒绝发布 |
| 13 | 封禁期通过 |
| 14 | 封禁期驳回 |

## 策略生效状态（get 返回的 status）

| status | 含义 |
|--------|------|
| 1 | 待审核 |
| 2 | 待发布 |
| 3 | 审核中 |
| 4 | 生效中 |
| 5 | 已失效/已撤回；若策略已提交延期，强制审核会直接尝试发起，最终以服务端状态校验结果为准 |
| 6 | 已结束 |
| 7 | 草稿 |

## flow 响应语义

```json
{
  "ok": false,
  "mode": "normal",
  "steps": [
    {"step": "check_pass", "ok": false, "error": "OPS_FAILED",
     "message": "操作[审核通过]无法应用于状态[发布成功]", "approve_id": 2630}
  ],
  "push": null,
  "auditOk": false,
  "publishOk": false,
  "pushStatus": null,
  "stuckAt": null
}
```

- `ok=true` → 审核/发布成功（推全结果另看 `push` / `pushStatus`）。
- `stuckAt=6` → 卡在审核成功、发布未完成。
- `auditOk=true 且 publishOk=false` → 审核成功但发布失败。
- `pushStatus`：`pushed`（已推全）/ `not_settled`（推全未收敛，仍在推全中）/ `ban_submitted` / `failed`。
- 脚本会额外输出 `elapsedSec`（本次全流程总耗时）与 `stepTimings`（若 steps 含时间戳，则按相邻步骤计算环节耗时）；这是展示层派生字段，不会额外请求接口。
- 单条超时（脚本 180s）后可能仍在推全中，先查 Targeting 再决定是否重试。

## 幂等性

后端按策略状态校验，重复执行不允许的动作（如对已发布成功的策略再审核）会返回 `无法应用于状态[...]` 的业务失败，不会重复生效。
