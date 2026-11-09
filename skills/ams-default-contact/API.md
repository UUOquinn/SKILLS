# AMS 账户联系人绑定 · API 契约

> Host：`https://ads-platform.example.com`  
> 来源：服务商前端 `ams_agency_fe_js`（`gR("advertiser", …)` → `SITE.smbUrl` = `/vendor/`）  
> 凭证：浏览器登录 Cookie + 查询参数 `csrf`（由 `csrf_token` 或 `session_key` 计算）。**真实 Cookie 不上库。**

## 鉴权

`csrf` 算法（与前端 `getCsrfToken` 一致）：

```
t = 5381
for ch in skey_or_csrf_token:
    t += (t << 5) + ord(ch)
csrf = t & 2147483647
```

请求会自动带 `agency_id`、`bm_id`（没有则 `0`）。

从已登录页任意 `/vendor/advertiser/` 请求复制 `Cookie` 整行到本机 `scripts/api.json`。常见字段：`uin` / `session_key` / `csrf_token` / `session_token`。

---

## 1. 拉可选账户联系人

`GET /vendor/advertiser/list_contacts?csrf={csrf}`

| 参数 | 说明 |
|------|------|
| `agency_id` | 服务商 uid（占位例：`10000001`） |
| `business_license_no` | 营业执照号 / 个人证件号（下拉打开时用当前账户证件号去拉） |
| `customer_id` | 可选；已有绑定时带上 |
| `bm_id` | 商务经理 id，没有填 `0` |

成功：`code === 0`

| 字段 | 说明 |
|------|------|
| `data.customer_id` | 客户业务 id，写入 `api.json` |
| `data.contact_list[]` | `contact_id` / `contact_name` / `mobile` / `approval_status` |

下拉展示形态：`{name}({mobile})`。

---

## 2. 选中账户联系人（默认路径）

`POST /vendor/advertiser/update?csrf={csrf}`

对应开户页点选「账户联系人」后提交。脚本会先 `GET /vendor/advertiser/get`，再只提交选中联系人所需字段（不改资质 / 法人 / 行业）。

要点字段：`uid`、`contact_id`、`customer_id`，以及账户详情带回的注册 / 执照等原值。

`Content-Type`：`application/json`。

---

## 3. 绑定默认 / 账户联系人（`--bind-only`）

`POST /vendor/advertiser/bind_contact?csrf={csrf}`

前端调用（编辑联系人弹窗点确定，且当前是「选择已有」）：

```js
{
  agency_id,
  customer_id,
  contact_id,
  uid_list: [uid1, uid2, /* … */]   // 可一次多个账户
}
```

`Content-Type`：前端未指定 JSON 时 jQuery 默认 `application/x-www-form-urlencoded`。脚本默认按 form 发送；`uid_list` 重复键：`uid_list=a&uid_list=b`。若服务端拒解析，加 `--json`。

成功：`code === 0`，页面 toast「编辑联系人成功」。该路径会校验账号状态，比 `advertiser/update` 更严。

---

## 4. 不要用的接口

| URL | 原因 |
|-----|------|
| `POST /telemetry-hub/api` | 行为上报，响应约 25 字节 |
| `GET /vendor/advertiser/get_verify_qr_code` | 开户验证码 |
| 「新增联系人」提交 | 另一条开户链路，会下发短信 |

---

## 本机配置（占位）

```bash
cp api.example.json api.json   # 勿提交 api.json
```

见 [`scripts/api.example.json`](scripts/api.example.json)。字段：`cookie`、`agencyId`、`contactId`、`customerId`、`businessLicenseNo`、`uids`、`chunkSize`。
