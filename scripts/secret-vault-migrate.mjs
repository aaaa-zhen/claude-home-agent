#!/usr/bin/env node
// One-off: move credentials that already sit in plaintext (memory files, the
// conversation database, SDK logs) into the Keychain and replace them with
// 「[已移入密码箱：<name>]」. Prints file:line, the proposed name and masked
// values only. `--dry-run` changes nothing; `--apply` must run inside the
// agent's launchd session (Keychain writes fail over SSH).
import fs from 'node:fs';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {fileURLToPath} from 'node:url';
import {detectSecret} from './secret-detect.mjs';
import {SecretVault, ROOT} from './secret-vault.mjs';

const apply = process.argv.includes('--apply');
const dryRun = !apply;
const PLACEHOLDER = name => `[已移入密码箱：${name}]`;

function mask(value) {
  const s = String(value ?? '');
  return s ? `${s[0]}${'*'.repeat(Math.min(s.length - 1, 8))}(${s.length})` : '(none)';
}

function listTargets() {
  const files = [];
  const add = (dir, pattern) => { try { for (const f of fs.readdirSync(dir)) if (pattern.test(f)) files.push(path.join(dir, f)); } catch {} };
  add(path.join(ROOT, 'memory'), /\.md$/);
  add(path.join(ROOT, 'memory', 'daily'), /\.md$/);
  add(path.join(ROOT, 'memory', 'skills'), /\.md$/);
  add('/tmp/openclaw', /^openclaw-.*\.log$/);
  return files;
}

// Rewrites one line, storing every credential found. Returns {line, hits}.
async function rewriteLine(line, where, vault, seen) {
  let text = line, hits = [];
  for (let guard = 0; guard < 5; guard++) {
    const hit = detectSecret(text);
    if (!hit) break;
    let name = hit.name;
    const known = seen.get(name);
    if (known && known !== hit.password) { let n = 2; while (seen.has(`${hit.name}-${n}`) && seen.get(`${hit.name}-${n}`) !== hit.password) n++; name = `${hit.name}-${n}`; }
    seen.set(name, hit.password);
    hits.push({where, name, user: mask(hit.user), password: mask(hit.password)});
    if (apply) await vault.add({name, user: hit.user, password: hit.password, note: `迁移自 ${where}`}, {source: 'migrate'});
    text = hit.redactedText.replace(`[已存入密码箱：${hit.name}]`, PLACEHOLDER(name));
  }
  return {line: text, hits};
}

async function migrateFile(file, vault, seen) {
  const original = fs.readFileSync(file, 'utf8');
  const lines = original.split('\n');
  const hits = [];
  for (let i = 0; i < lines.length; i++) {
    const result = await rewriteLine(lines[i], `${path.relative(ROOT, file)}:${i + 1}`, vault, seen);
    if (result.hits.length) { hits.push(...result.hits); lines[i] = result.line; }
  }
  if (apply && hits.length) {
    const mode = fs.statSync(file).mode & 0o777;
    fs.writeFileSync(file, lines.join('\n'), {mode});
  }
  return hits;
}

async function migrateDatabase(vault, seen) {
  const dbPath = path.join(ROOT, 'runtime', 'conversation-context.db');
  if (!fs.existsSync(dbPath)) return [];
  const db = new DatabaseSync(dbPath);
  const hits = [];
  try {
    const rows = db.prepare('SELECT seq, id, user_text, assistant_text FROM turns').all();
    const update = db.prepare('UPDATE turns SET user_text=?, assistant_text=? WHERE id=?');
    for (const row of rows) {
      const user = await rewriteLine(row.user_text || '', `conversation-context.db turns#${row.seq} user`, vault, seen);
      const assistant = await rewriteLine(row.assistant_text || '', `conversation-context.db turns#${row.seq} assistant`, vault, seen);
      if (user.hits.length || assistant.hits.length) {
        hits.push(...user.hits, ...assistant.hits);
        if (apply) update.run(user.line, row.assistant_text === null ? null : assistant.line, row.id);
      }
    }
  } finally { db.close(); }
  return hits;
}

const vault = new SecretVault({requireForeground: false});
const seen = new Map();
const all = [];
for (const file of listTargets()) all.push(...await migrateFile(file, vault, seen));
all.push(...await migrateDatabase(vault, seen));
console.log(`${dryRun ? '[dry-run] ' : ''}${all.length} credential(s) in ${new Set(all.map(h => h.where.split(':')[0])).size} location(s)`);
for (const h of all) console.log(`${h.where}\t→ ${h.name}\tuser=${h.user}\tpassword=${h.password}`);
if (dryRun && all.length) console.log('nothing changed; rerun with --apply inside the launchd session to store and replace');
