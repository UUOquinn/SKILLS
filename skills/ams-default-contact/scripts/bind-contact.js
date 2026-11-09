#!/usr/bin/env node
/**
 * 绑定 AMS 账户联系人（页面「账户联系人 / 默认联系人」）
 *
 *   node bind-contact.js --list
 *   node bind-contact.js --dry-run
 *   node bind-contact.js
 *   node bind-contact.js --file uids.txt
 *   node bind-contact.js --json
 */
const fs = require('fs');
const path = require('path');

const CONFIG_PATH = path.join(__dirname, 'api.json');

function gtkFromCookie(cookie) {
  const m =
    cookie.match(/(?:^|;\s*)csrf_token=([^;]*)/) ||
    cookie.match(/(?:^|;\s*)session_key=([^;]*)/);
  const s = m ? decodeURIComponent(m[1]) : '';
  if (!s) throw new Error('Cookie 里没有 csrf_token 或 session_key，无法算 csrf');
  let t = 5381;
  for (let i = 0; i < s.length; i++) t += (t << 5) + s.charCodeAt(i);
  return t & 2147483647;
}

function loadConfig() {
  if (!fs.existsSync(CONFIG_PATH)) {
    throw new Error('未找到 api.json。先: cp api.example.json api.json 并填 Cookie / agencyId / contactId');
  }
  const cfg = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
  if (!cfg.cookie || /请填写|复制整行/.test(cfg.cookie)) {
    throw new Error('请在 api.json 填入浏览器 Cookie');
  }
  cfg.agencyId = Number(cfg.agencyId);
  cfg.bmId = cfg.bmId == null ? 0 : Number(cfg.bmId);
  cfg.customerId = Number(cfg.customerId || 0);
  cfg.contactId = Number(cfg.contactId || 0);
  cfg.chunkSize = Number(cfg.chunkSize || 20);
  cfg.baseUrl = (cfg.baseUrl || 'https://ads-platform.example.com').replace(/\/$/, '');
  return cfg;
}

function parseUids(cfg, argv) {
  const fileIdx = argv.indexOf('--file');
  if (fileIdx !== -1 && argv[fileIdx + 1]) {
    return fs
      .readFileSync(path.resolve(argv[fileIdx + 1]), 'utf8')
      .split('\n')
      .map((l) => l.replace(/#.*/, '').trim())
      .filter((l) => /^\d+$/.test(l))
      .map(Number);
  }
  const cli = argv.filter((a) => /^\d+$/.test(a)).map(Number);
  if (cli.length) return cli;
  const fromCfg = (cfg.uids || []).map(Number).filter((n) => n > 0);
  if (!fromCfg.length) throw new Error('没有 uid：传入数字、--file uids.txt，或在 api.json.uids 填写');
  return fromCfg;
}

function chunk(arr, size) {
  const out = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

async function request(cfg, { method, pathname, data, asJson }) {
  const csrf = gtkFromCookie(cfg.cookie);
  const url = new URL(pathname, cfg.baseUrl);
  url.searchParams.set('csrf', String(csrf));

  const payload = {
    agency_id: cfg.agencyId,
    bm_id: cfg.bmId,
    ...data,
  };

  const headers = {
    Cookie: cfg.cookie,
    Accept: 'application/json, text/plain, */*',
    Origin: cfg.baseUrl,
    Referer: `${cfg.baseUrl}/agency/advertiser-edit-account?agencyId=${cfg.agencyId}`,
  };

  let body;
  if (method === 'GET') {
    Object.entries(payload).forEach(([k, v]) => {
      if (v === undefined || v === '' || v === null) return;
      url.searchParams.set(k, String(v));
    });
  } else if (asJson) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(payload);
  } else {
    headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8';
    const sp = new URLSearchParams();
    Object.entries(payload).forEach(([k, v]) => {
      if (Array.isArray(v)) v.forEach((item) => sp.append(k, String(item)));
      else if (v !== undefined && v !== null) sp.append(k, String(v));
    });
    body = sp.toString();
  }

  const res = await fetch(url, { method, headers, body });
  const text = await res.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    throw new Error(`HTTP ${res.status} 非 JSON: ${text.slice(0, 200)}`);
  }
  return json;
}

async function listContacts(cfg) {
  if (!cfg.businessLicenseNo) {
    throw new Error('拉列表需要 api.json.businessLicenseNo（账户营业执照号）');
  }
  const data = { business_license_no: cfg.businessLicenseNo };
  if (cfg.customerId) data.customer_id = cfg.customerId;
  return request(cfg, {
    method: 'GET',
    pathname: '/vendor/advertiser/list_contacts',
    data,
  });
}

async function bindChunk(cfg, uids, asJson) {
  return request(cfg, {
    method: 'POST',
    pathname: '/vendor/advertiser/bind_contact',
    asJson,
    data: {
      customer_id: cfg.customerId,
      contact_id: cfg.contactId,
      uid_list: uids,
    },
  });
}

/** 开户页点选「账户联系人」后提交：POST /vendor/advertiser/update */
async function getAdvertiser(cfg, uid) {
  return request(cfg, {
    method: 'GET',
    pathname: '/vendor/advertiser/get',
    data: { uid },
  });
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function selectContactOnAccount(cfg, uid) {
  const got = await getAdvertiser(cfg, uid);
  if (got.code !== 0) return got;
  const d = got.data || {};
  const existing = (d.contact_list || []).find((p) => p && p.contact_id);
  if (existing) {
    return {
      code: 0,
      skipped: true,
      data: { contact_id: existing.contact_id },
      msg: `已有联系人 ${existing.contact_id}`,
    };
  }
  const buyingType =
    d.buying_type_name ||
    (d.buying_type === 1 ? 'BUYINGTYPE_AUCTION' : d.buying_type);
  // 只提交选中账户联系人需要的字段，不改资质/法人/行业
  const payload = {
    uid,
    uname: d.uname,
    buying_type: buyingType,
    registration_type: d.registration_type,
    registration_sub_type: d.registration_sub_type,
    certification_code: d.certification_code,
    website_url: d.website_url,
    license_image_url: d.license_image_url,
    license_image_id: d.license_image_id,
    license_no: d.license_no,
    area_code: d.area_code,
    legal_person_name: d.legal_person_name,
    legal_person_identity_no: d.legal_person_identity_no,
    tmall_advertiser_adtype: d.tmall_advertiser_adtype,
    is_cps: d.is_cps === 1 || d.is_cps === 'YES' ? 'YES' : 'NO',
    product_image_urls: d.product_image_urls,
    product_description: d.product_description,
    related_company: d.related_company,
    forbid_app_ads: d.forbid_app_ads,
    industry_id: d.industry_id,
    customer_id: d.customer_id || cfg.customerId,
    contact_id: cfg.contactId,
  };
  Object.keys(payload).forEach((k) => {
    if (payload[k] === undefined) delete payload[k];
  });
  return request(cfg, {
    method: 'POST',
    pathname: '/vendor/advertiser/update',
    asJson: true,
    data: payload,
  });
}

function printList(json) {
  if (json.code !== 0) {
    console.error('拉列表失败', json.code, json.msg || json.message || json);
    return;
  }
  const d = json.data || {};
  console.log(`customer_id: ${d.customer_id}`);
  const list = d.contact_list || [];
  if (!list.length) {
    console.log('(无联系人)');
    return;
  }
  list.forEach((p) => {
    console.log(
      `  contact_id=${p.contact_id}  ${p.contact_name}  ${p.mobile}  status=${p.approval_status}`
    );
  });
}

async function main() {
  const argv = process.argv.slice(2);
  const cfg = loadConfig();
  const asJson = !argv.includes('--form');

  if (argv.includes('--list') || argv.includes('--dry-run')) {
    if (cfg.businessLicenseNo) {
      console.log('GET list_contacts');
      const json = await listContacts(cfg);
      printList(json);
    } else {
      console.log('未填 businessLicenseNo，跳过拉列表');
    }
    if (argv.includes('--list')) return;
    console.log(
      `\n[--dry-run] 将 POST bind_contact  agency=${cfg.agencyId}  contact_id=${cfg.contactId}  customer_id=${cfg.customerId}`
    );
    const uids = parseUids(cfg, argv);
    console.log(`uids (${uids.length}): ${uids.join(', ')}`);
    return;
  }

  if (!cfg.contactId) throw new Error('api.json.contactId 必填（用 --list 查出）');
  if (!cfg.customerId) throw new Error('api.json.customerId 必填');

  const uids = parseUids(cfg, argv);
  const useBindOnly = argv.includes('--bind-only');
  console.log(
    `选中账户联系人 ${uids.length} 个账户，contact_id=${cfg.contactId} 路径=${useBindOnly ? 'bind_contact' : 'advertiser/update'}`
  );

  const rows = [];
  if (useBindOnly) {
    for (const group of chunk(uids, cfg.chunkSize)) {
      console.log(`\n→ ${group.join(', ')}`);
      try {
        const json = await bindChunk(cfg, group, asJson);
        const ok = json.code === 0;
        const reason = ok ? '' : json.msg || json.message || JSON.stringify(json).slice(0, 200);
        group.forEach((uid) => {
          rows.push({ uid, status: ok ? 'OK' : 'FAIL', reason });
          console.log(ok ? `  ✓ ${uid}` : `  ✗ ${uid}  ${reason}`);
        });
      } catch (err) {
        group.forEach((uid) => {
          rows.push({ uid, status: 'FAIL', reason: err.message });
          console.log(`  ✗ ${uid}  ${err.message}`);
        });
      }
    }
  } else {
    for (const uid of uids) {
      console.log(`\n→ ${uid}`);
      try {
        const json = await selectContactOnAccount(cfg, uid);
        const ok = json.code === 0;
        const skipped = !!json.skipped;
        const reason = skipped
          ? json.msg || '已有'
          : ok
            ? ''
            : json.msg || json.message || JSON.stringify(json).slice(0, 200);
        const status = ok ? (skipped ? 'SKIP' : 'OK') : 'FAIL';
        rows.push({ uid, status, reason, contactId: (json.data && json.data.contact_id) || cfg.contactId });
        console.log(ok ? `  ${skipped ? '○' : '✓'} ${uid}  ${reason}` : `  ✗ ${uid}  ${reason}`);
        await sleep(200);
      } catch (err) {
        rows.push({ uid, status: 'FAIL', reason: err.message });
        console.log(`  ✗ ${uid}  ${err.message}`);
      }
    }
  }

  console.log('\n| uid | contact_id | 状态 | 原因 |');
  console.log('|-----|----------------|------|------|');
  rows.forEach((r) => {
    console.log(`| ${r.uid} | ${r.contactId || cfg.contactId} | ${r.status} | ${r.reason} |`);
  });
  const ok = rows.filter((r) => r.status === 'OK').length;
  const skip = rows.filter((r) => r.status === 'SKIP').length;
  const fail = rows.filter((r) => r.status === 'FAIL').length;
  console.log(`\n完成: 新选中 ${ok}，已有跳过 ${skip}，失败 ${fail}，合计 ${rows.length}`);
  fs.writeFileSync(path.join(__dirname, 'last-result.tsv'), rows.map((r) => `${r.uid}\t${r.status}\t${r.reason}`).join('\n'));
  process.exit(fail ? 1 : 0);
}

main().catch((e) => {
  console.error(e.message || e);
  process.exit(1);
});
