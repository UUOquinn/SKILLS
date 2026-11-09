#!/usr/bin/env node
/**
 * audit-trace.js — 联盟审核溯源记录器（v1）
 *
 * 目标：给「谁在什么时候对哪个策略做了什么」留一条 skill 侧可查的日志。
 * 背景：平台侧 audit 日志只记 system:auto-approve，手工审核与发布重放无痕，
 *       49043 那次审核至今查不出操作者，此文件补上这个盲区。
 *
 * 设计原则：
 *   - 只追加（append-only JSONL），不覆盖、不删除、不修改
 *   - 日志写 skill 目录之外（默认 <工作目录>/audit-trace/），skill 重装不丢
 *   - 零依赖，仅 Node 标准库
 *
 * 用法：
 *   # 记录一次执行结果（从 stdin 读 union_audit.py 输出的最后一行 JSON）
 *   node scripts/audit-trace.js record --action audit --auditor 张三 --ids 25747 < result.json
 *
 *   # 或显式传输入文件
 *   node scripts/audit-trace.js record --action audit --ids 25747 --input-file result.json
 *
 *   # 查询历史
 *   node scripts/audit-trace.js query --date 2026-09-22
 *   node scripts/audit-trace.js query --strategy-id 25747
 *   node scripts/audit-trace.js query --auditor wb_example_user --limit 20
 *
 * 环境变量：
 *   UNION_AUDIT_TRACE_DIR  日志目录（默认 <cwd>/audit-trace）
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const VERSION = '1';
const TRACE_DIR = process.env.UNION_AUDIT_TRACE_DIR || path.join(process.cwd(), 'audit-trace');
const TZ = 'Asia/Shanghai';

// ---------- 工具 ----------

function localDate(d) {
  return new Intl.DateTimeFormat('sv-SE', { timeZone: TZ }).format(d); // YYYY-MM-DD
}

function traceFiles() {
  if (!fs.existsSync(TRACE_DIR)) return [];
  return fs.readdirSync(TRACE_DIR).filter(f => /^audit-\d{4}-\d{2}-\d{2}\.jsonl$/.test(f)).sort();
}

function parseArgs(argv) {
  const out = { op: null, _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === 'record' || a === 'query') { out.op = a; continue; }
    const m = a.match(/^--([a-z-]+)$/);
    if (m) {
      const key = m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      if (i + 1 < argv.length && !argv[i + 1].startsWith('--')) { out[key] = argv[i + 1]; i += 1; }
      else { out[key] = true; }
      continue;
    }
    out._.push(a);
  }
  return out;
}

function fail(msg, code = 1) {
  console.error(msg);
  process.exit(code);
}

// ---------- record ----------

function readPayload(opts) {
  let raw = '';
  if (opts.inputFile) raw = fs.readFileSync(opts.inputFile, 'utf8');
  else raw = fs.readFileSync(0, 'utf8');
  const lines = raw.trim().split(/\r?\n/).filter(l => l.trim());
  if (!lines.length) fail('stdin/输入文件为空，无法记录');
  // 与 SKILL.md 约定一致：stdout 最后一行是业务 JSON
  try { return JSON.parse(lines[lines.length - 1]); }
  catch (e) { fail('最后一行不是合法 JSON: ' + e.message); }
}

function slimItem(it) {
  if (!it) return null;
  const slim = {
    id: it.id, ok: it.ok, skipped: it.skipped, skipReason: it.skipReason,
    strategyTypeDesc: it.strategyTypeDesc, message: it.message,
    auditOk: it.auditOk, publishOk: it.publishOk, pushStatus: it.pushStatus,
    deferredInvalidCandidate: it.deferredInvalidCandidate,
  };
  return Object.fromEntries(Object.entries(slim).filter(([, v]) => v !== undefined && v !== null));
}

function record(opts) {
  const payload = readPayload(opts);
  const d = new Date();
  const ids = String(opts.ids || '').split(/[,，\s]+/).filter(Boolean).map(Number);
  const line = {
    v: VERSION,
    ts: d.toISOString(),
    tsMs: d.getTime(),
    tz: TZ,
    operator: opts.auditor || null,                       // 审核人（业务口径）
    systemUser: process.env.USER || process.env.USERNAME || null, // 系统执行者（落地口径）
    action: opts.action || null,
    actionId: payload.actionId || null,
    strategyIds: ids,
    source: { host: os.hostname(), pid: process.pid, cwd: process.cwd() },
    result: payload.ok === undefined ? null : {
      ok: payload.ok, code: payload.code,
      okCount: payload.data && payload.data.okCount,
      failCount: payload.data && payload.data.failCount,
      skipCount: payload.data && payload.data.skipCount,
      items: (payload.data && payload.data.items || []).map(slimItem),
    },
    error: payload.error || null,
    msg: payload.msg || null,
  };
  fs.mkdirSync(TRACE_DIR, { recursive: true });
  const file = path.join(TRACE_DIR, `audit-${localDate(d)}.jsonl`);
  fs.appendFileSync(file, JSON.stringify(line) + '\n', 'utf8');
  console.log(JSON.stringify({ ok: true, code: 0, line: line.tsMs, file }));
}

// ---------- query ----------

function matches(line, opts) {
  if (opts.strategyId && !(line.strategyIds || []).includes(Number(opts.strategyId))) return false;
  if (opts.auditor && line.operator !== opts.auditor) return false;
  if (opts.action && line.action !== opts.action) return false;
  return true;
}

function query(opts) {
  const files = opts.date
    ? [`audit-${opts.date}.jsonl`].filter(f => fs.existsSync(path.join(TRACE_DIR, f)))
    : traceFiles();
  const lines = [];
  for (const f of files) {
    const text = fs.readFileSync(path.join(TRACE_DIR, f), 'utf8');
    for (const l of text.split(/\r?\n/)) {
      if (!l.trim()) continue;
      try {
        const line = JSON.parse(l);
        if (matches(line, opts)) lines.push({ file: f, line });
      } catch (e) { /* 跳过坏行，不影响查询 */ }
    }
  }
  lines.sort((a, b) => b.line.tsMs - a.line.tsMs);
  const out = lines.slice(0, Number(opts.limit || 50));
  if (opts.json) { console.log(JSON.stringify(out, null, 2)); return; }
  for (const { file, line } of out) {
    console.log(`[${file}] ${line.ts}  operator=${line.operator}(${line.systemUser})  action=${line.action}  ids=${(line.strategyIds || []).join(',') || '-'}`);
    for (const it of (line.result && line.result.items) || []) {
      console.log(`    #${it.id} ok=${it.ok} skipped=${it.skipped} typeDesc=${it.strategyTypeDesc || '-'} ${it.message || ''}`);
    }
  }
  console.log(`\n共 ${out.length} 条${opts.limit ? '（limit=' + opts.limit + '）' : ''}，日志目录: ${TRACE_DIR}`);
}

// ---------- main ----------

function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (!opts.op) fail('用法: audit-trace.js <record|query> [选项]\n' +
    '  record --action <动作> [--auditor <审核人>] [--ids <1,2,3>] [--input-file <json>]\n' +
    '  query  [--date <YYYY-MM-DD>] [--strategy-id <id>] [--auditor <名>] [--action <动作>] [--limit <n>] [--json]');
  if (opts.op === 'record') record(opts);
  else if (opts.op === 'query') query(opts);
}

main();
