// watch-store.mjs — 盯梢机制的 SQLite 持久层(零依赖,用 Node 26 内置 node:sqlite)。
// 设计来源:「盯着回复」可移植设计文档 §1-§2。所有状态都在库里,进程重启自动续上。
//
// 两张表:
//   watches    —— 持久意图(standing intent),状态机 armed/fired/delivered/cancelled/expired
//   deliveries —— 通知 outbox,投递可靠性与 watch 状态解耦
//
// 关键不变量(每条都对应设计文档里的一次真实 bug):
//   3. fired 只从 armed 转入(WHERE status='armed',看 changes),防并发重复触发
//   4. 游标前移只从 fired 转入(WHERE status='fired');取消赢了赛跑就不复活
//   2. stale 丢弃:cancelled/expired 的 watch 即使查出新消息也不处理(靠 listArmed 过滤)

import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
// 惰性解析:测试可在首次 db() 前设 WATCH_DB_PATH=:memory: 而不污染真库。
function resolveDbPath() {
  return process.env.WATCH_DB_PATH || join(ROOT, 'runtime', 'watches.db');
}

export function nowIso() {
  return new Date().toISOString();
}

function isoPlus(baseIso, ms) {
  return new Date(new Date(baseIso).getTime() + ms).toISOString();
}

let _db = null;
export function db() {
  if (_db) return _db;
  const path = resolveDbPath();
  if (path !== ':memory:') mkdirSync(dirname(path), { recursive: true });
  const d = new DatabaseSync(path);
  d.exec('PRAGMA journal_mode = WAL');
  d.exec('PRAGMA busy_timeout = 4000');
  d.exec(`
    CREATE TABLE IF NOT EXISTS watches(
      watch_id        TEXT PRIMARY KEY,
      conversation_id TEXT NOT NULL,
      kind            TEXT NOT NULL,
      target          TEXT NOT NULL,
      since_ts        TEXT NOT NULL,
      status          TEXT NOT NULL,
      fires           INTEGER NOT NULL DEFAULT 0,
      last_checked_at TEXT,
      expires_at      TEXT NOT NULL,
      created_at      TEXT NOT NULL,
      payload         TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_watches_status ON watches(status, expires_at);

    CREATE TABLE IF NOT EXISTS deliveries(
      delivery_id     TEXT PRIMARY KEY,
      conversation_id TEXT NOT NULL,
      text            TEXT NOT NULL,
      watch_id        TEXT,
      status          TEXT NOT NULL,
      created_at      TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status);
  `);
  _db = d;
  return d;
}

// ---- watches ----------------------------------------------------------------

const DEFAULT_TTL_MS = 24 * 60 * 60 * 1000; // 24h
export const MAX_FIRES = 20;

export function createWatch({ conversationId, kind, target, payload = {}, ttlMs = DEFAULT_TTL_MS, sinceTs }) {
  const now = nowIso();
  const id = `w_${randomUUID().slice(0, 12)}`;
  db().prepare(`
    INSERT INTO watches(watch_id, conversation_id, kind, target, since_ts, status, fires,
                        last_checked_at, expires_at, created_at, payload)
    VALUES (?, ?, ?, ?, ?, 'armed', 0, NULL, ?, ?, ?)
  `).run(id, conversationId, kind, target, sinceTs || now, isoPlus(now, ttlMs), now, JSON.stringify(payload));
  return getWatch(id);
}

function hydrate(row) {
  if (!row) return null;
  let payload = {};
  try { payload = row.payload ? JSON.parse(row.payload) : {}; } catch { payload = {}; }
  return { ...row, payload };
}

export function getWatch(id) {
  return hydrate(db().prepare('SELECT * FROM watches WHERE watch_id = ?').get(id));
}

export function listArmed() {
  return db().prepare("SELECT * FROM watches WHERE status = 'armed' ORDER BY created_at").all().map(hydrate);
}

export function listActive() {
  return db().prepare("SELECT * FROM watches WHERE status IN ('armed','fired') ORDER BY created_at").all().map(hydrate);
}

export function listAll() {
  return db().prepare('SELECT * FROM watches ORDER BY created_at DESC').all().map(hydrate);
}

// 不变量 3:fired 只从 armed 转入。返回是否抢到(changes===1)。
export function tryMarkFired(id) {
  const r = db().prepare(`
    UPDATE watches SET status = 'fired', fires = fires + 1, last_checked_at = ?
    WHERE watch_id = ? AND status = 'armed'
  `).run(nowIso(), id);
  return r.changes === 1;
}

// 不变量 4:游标前移(+回 armed)只从 fired 转入。取消赢了赛跑则 changes===0,放弃前移。
export function advanceCursorAndArm(id, newSinceTs, newPayload) {
  const r = db().prepare(`
    UPDATE watches SET since_ts = ?, payload = ?, status = 'armed', last_checked_at = ?
    WHERE watch_id = ? AND status = 'fired'
  `).run(newSinceTs, JSON.stringify(newPayload), nowIso(), id);
  return r.changes === 1;
}

// 注入/投递失败:退回 armed,游标不动,下一轮重试同一批(设计 §3.6)。
export function revertFiredToArmed(id) {
  const r = db().prepare("UPDATE watches SET status = 'armed' WHERE watch_id = ? AND status = 'fired'").run(id);
  return r.changes === 1;
}

// 启动/每轮开头:残留在 fired 的 = 上次崩在注入中间,退回 armed 重推(宁重不丢)。
export function recoverFiredResidual() {
  const r = db().prepare("UPDATE watches SET status = 'armed' WHERE status = 'fired'").run();
  return r.changes;
}

export function touchChecked(id) {
  db().prepare('UPDATE watches SET last_checked_at = ? WHERE watch_id = ?').run(nowIso(), id);
}

export function updatePayload(id, payload) {
  db().prepare('UPDATE watches SET payload = ? WHERE watch_id = ?').run(JSON.stringify(payload), id);
}

// 取消:armed 或 fired 都能取消(取消赢赛跑);已终态的不动。
export function cancelWatch(id) {
  const r = db().prepare("UPDATE watches SET status = 'cancelled' WHERE watch_id = ? AND status IN ('armed','fired')").run(id);
  return r.changes === 1;
}

export function cancelAll() {
  const r = db().prepare("UPDATE watches SET status = 'cancelled' WHERE status IN ('armed','fired')").run();
  return r.changes;
}

export function expireWatch(id) {
  db().prepare("UPDATE watches SET status = 'expired' WHERE watch_id = ? AND status IN ('armed','fired')").run(id);
}

// 把所有 armed 里已过 expires_at 的标 expired,返回被过期的行(用于最后收尾通知)。
export function sweepExpired() {
  const now = nowIso();
  const rows = db().prepare("SELECT * FROM watches WHERE status = 'armed' AND expires_at <= ?").all(now).map(hydrate);
  if (rows.length) {
    db().prepare("UPDATE watches SET status = 'expired' WHERE status = 'armed' AND expires_at <= ?").run(now);
  }
  return rows;
}

// ---- deliveries (outbox) ----------------------------------------------------

export function enqueueDelivery({ conversationId, text, watchId }) {
  const id = `d_${randomUUID().slice(0, 12)}`;
  db().prepare(`
    INSERT INTO deliveries(delivery_id, conversation_id, text, watch_id, status, created_at)
    VALUES (?, ?, ?, ?, 'pending', ?)
  `).run(id, conversationId, text, watchId || null, nowIso());
  return id;
}

export function pendingDeliveries() {
  return db().prepare("SELECT * FROM deliveries WHERE status = 'pending' ORDER BY created_at").all();
}

export function markDeliveryAcked(id) {
  db().prepare("UPDATE deliveries SET status = 'acked' WHERE delivery_id = ?").run(id);
}
