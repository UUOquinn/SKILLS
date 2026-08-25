# AMS 账户联系人绑定 · API 契约

> Host：`https://e.qq.com`  
> 来源：服务商前端 `ams_agency_fe_js`（`gR("advertiser", …)` → `SITE.smbUrl` = `/agp/`）  
> 凭证：浏览器登录 Cookie + 查询参数 `g_tk`（由 `gdt_protect` 或 `skey` 计算）。**真实 Cookie 不上库。**

## 鉴权

`g_tk` 算法（与前端 `getACSRFToken` 一致）：

```
t = 5381
for ch in skey_or_gdt_protect:
    t += (t << 5) + ord(ch)
g_tk = t & 2147483647
```

请求会自动带 `agency_uid`、`bm_id`（没有则 `0`）。

从已登录页任意 `/agp/advertiser/` 请求复制 `Cookie` 整行到本机 `scripts/api.json`。常见字段：`uin` / `skey` / `gdt_protect` / `gdt_token`。

---

## 1. 拉可选账户联系人

`GET /agp/advertiser/get_binding_link_person_list?g_tk={g_tk}`

| 参数 | 说明 |
|------|------|
| `agency_uid` | 服务商 uid（占位例：`10000001`） |
| `reg_certification_no` | 营业执照号 / 个人证件号（下拉打开时用当前账户证件号去拉） |
| `customer_business_id` | 可选；已有绑定时带上 |
| `bm_id` | 商务经理 id，没有填 `0` |

成功：`code === 0`

| 字段 | 说明 |
|------|------|
| `data.customer_business_id` | 客户业务 id，写入 `api.json` |
| `data.link_person_list[]` | `link_person_id` / `link_person_name` / `mobile` / `approval_status` |

下拉展示形态：`{name}({mobile})`。

---

## 2. 选中账户联系人（默认路径）

`POST /agp/advertiser/update?g_tk={g_tk}`

对应开户页点选「账户联系人」后提交。脚本会先 `GET /agp/advertiser/get`，再只提交选中联系人所需字段（不改资质 / 法人 / 行业）。

要点字段：`uid`、`link_person_id`、`customer_business_id`，以及账户详情带回的注册 / 执照等原值。

`Content-Type`：`application/json`。

---

## 3. 绑定默认 / 账户联系人（`--bind-only`）

`POST /agp/advertiser/bind_link_person?g_tk={g_tk}`

前端调用（编辑联系人弹窗点确定，且当前是「选择已有」）：

```js
{
  agency_uid,
  customer_business_id,
  link_person_id,
  uid_list: [uid1, uid2, /* … */]   // 可一次多个账户
}
```

`Content-Type`：前端未指定 JSON 时 jQuery 默认 `application/x-www-form-urlencoded`。脚本默认按 form 发送；`uid_list` 重复键：`uid_list=a&uid_list=b`。若服务端拒解析，加 `--json`。

成功：`code === 0`，页面 toast「编辑联系人成功」。该路径会校验账号状态，比 `advertiser/update` 更严。

---

## 4. 不要用的接口

| URL | 原因 |
|-----|------|
| `POST /data-report-hub/api` | 行为上报，响应约 25 字节 |
| `GET /agp/advertiser/get_verify_qr_code` | 开户验证码 |
| 「新增联系人」提交 | 另一条开户链路，会下发短信 |

---

## 本机配置（占位）

```bash
cp api.example.json api.json   # 勿提交 api.json
```

见 [`scripts/api.example.json`](scripts/api.example.json)。字段：`cookie`、`agencyUid`、`linkPersonId`、`customerBusinessId`、`regCertificationNo`、`uids`、`chunkSize`。
