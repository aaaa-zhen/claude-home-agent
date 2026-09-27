#!/usr/bin/env node
// reply-watch.mjs — 「盯着回复」盯梢器。设计来源:可移植设计文档 §3-§5。
//
// 用法(这些子命令就是设计里的 watch_create / watch_cancel「工具」):
//   node scripts/reply-watch.mjs create --kind preply_reply --tutor "Tutor A." \
//        --context "在等她今晚9:30那节50分钟的课"          # 替用户发完消息的同一轮调用
//   node scripts/reply-watch.mjs list                        # 看当前有哪些盯梢
//   node scripts/reply-watch.mjs cancel [<watchId>]          # 不带 id = 取消全部(用户说「不用盯了」)
//   node scripts/reply-watch.mjs check                       # 跑一轮(launchd 每 45s 调这个)
//   node scripts/reply-watch.mjs self-test                   # 不碰真数据,验证状态机不变量
//
// 命中后不发原文:起一个 claude -p 迷你会话(跟心跳同款)让大脑用自己的话说,
// 再走 outbox 发微信,并把这句写进 recent-context + last-proactive(主会话据此续接)。

import { execFileSync } from 'node:child_process';
import { appendFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { sendText } from '../weixin-send.mjs';
import { connectBrowser } from './browser-bridge-lib.mjs';
import * as store from './watch-store.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CONV = 'wechat-main';               // 单用户微信号,通知永远发给他
const SETTLE_MS = 45_000;                 // 最新一条落地不足 45s → 先不响(等这一阵说完)
const MAX_WAIT_MS = 180_000;              // 但首条已等 >180s → 不再等(连发不能一直憋)
const CLAUDE_BIN = join(ROOT, 'node_modules', '.bin', 'claude');

const now = () => Date.now();
const iso = () => new Date().toISOString();
function log(line) {
  mkdirSync(join(ROOT, 'tmp'), { recursive: true });
  appendFileSync(join(ROOT, 'tmp', 'reply-watch.log'), `[${new Date().toLocaleString('sv')}] ${line}\n`);
}

// ---- 参数解析 ---------------------------------------------------------------
function parseArgs(argv) {
  const a = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const t = argv[i];
    if (!t.startsWith('--')) { a._.push(t); continue; }
    const k = t.slice(2);
    if (k === 'json' || k === 'quiet' || k === 'dry-run') { a[k] = true; continue; }
    a[k] = argv[i + 1] || ''; i += 1;
  }
  return a;
}

// ---- 检查器:读 Preply 单个对话的逐条消息(零模型成本) ----------------------
// 返回 { income: [文本...] } —— income = 对方(老师)发的,按 DOM 顺序;
// 我们用「已推送 income 条数」当游标,天然去重、不需要解析时间戳。
async function scrapePreplyConversation(convHref) {
  const { browser, context } = await connectBrowser();
  const page = await context.newPage();
  try {
    await page.setViewportSize({ width: 1440, height: 1000 }).catch(() => {});
    await page.goto(convHref, { waitUntil: 'domcontentloaded', timeout: 60000 });
    // 等消息真渲染出来再抓,别用固定 sleep —— 否则页面没加载完就读到 0 条,
    // 盯梢会被「疑似未加载」护栏一直挡着不前进(2026-08-25 launchd 首跑踩到)。
    const loggedIn = await page.waitForSelector('[data-qa-id="message"]', { timeout: 25000 })
      .then(() => true).catch(() => false);
    if (!loggedIn) {
      const needLogin = await page.evaluate(() => !!document.querySelector('input[type="password"]'));
      return { income: [], needLogin, total: 0, renderFailed: !needLogin };
    }
    const readOnce = () => page.evaluate(() => {
      const rows = Array.from(document.querySelectorAll('[data-qa-id="message"]'));
      const income = [];
      for (const row of rows) {
        const isIncome = !!row.querySelector('[data-qa-id="message-income-main"], [data-qa-id="message-income-avatar"]');
        const isOut = !!row.querySelector('[data-qa-id="message-outcome-main"], [data-qa-id="message-outgoing-avatar"]');
        const textEl = row.querySelector('[data-qa-id="message-text"]');
        const text = (textEl ? textEl.innerText : row.innerText || '').trim();
        if (isIncome && !isOut) income.push(text || '[非文本消息]');
      }
      return { income, total: rows.length, needLogin: !!document.querySelector('input[type="password"]') };
    });
    // 稳定化:气泡是渐进渲染的,抓一次可能只读到一半。轮询到 income 条数连续两次
    // 不变(且>0)才认,最多等 12s。避免「读到 0/半数」把盯梢误判成未加载而空转。
    let last = -1, stableHits = 0, data = await readOnce();
    for (let i = 0; i < 20; i += 1) {
      if (data.income.length === last) { stableHits += 1; if (stableHits >= 1 && data.income.length > 0) break; }
      else { stableHits = 0; last = data.income.length; }
      await page.waitForTimeout(600);
      data = await readOnce();
    }
    return data;
  } finally {
    await page.close().catch(() => {});
    await browser.close().catch(() => {});
  }
}

// ---- 命中话术:起 claude -p 迷你会话让大脑用自己的话说 ----------------------
function phraseNotification({ tutorName, context, messages, fireNo }) {
  const joined = messages.map((m) => `「${m}」`).join('；');
  const nth = fireNo > 1 ? `(这是第 ${fireNo} 次推送,她又回了)` : '';
  const prompt = `[后台盯梢命中] 你是 Zhen 的微信家庭助手。你之前替他给英语老师 ${tutorName} 发了消息，${context || '在等她回复'}。她现在回了${nth}：${joined}。

自然地把这个结果告诉 Zhen——简短口语，像朋友随口说一句，别整段复述原文、别解释你是盯梢进程。如果需要他拍板下一步（比如要不要用余额订课/改时间），顺口问一句。只输出要发给他的那一条微信。`;

  const env = {
    ...process.env,
    CLAUDE_CONFIG_DIR: '/Users/zhen/home-agent/.claude-agent',
    PATH: `/opt/homebrew/bin:${process.env.PATH || ''}`,
    TZ: 'Asia/Shanghai',
    HTTP_PROXY: 'http://127.0.0.1:7897', http_proxy: 'http://127.0.0.1:7897',
    HTTPS_PROXY: 'http://127.0.0.1:7897', https_proxy: 'http://127.0.0.1:7897',
    NO_PROXY: '192.168.1.100,localhost,127.0.0.1,.weixin.qq.com,ilinkai.weixin.qq.com,.example.com,.amap.com,.gtimg.cn,.qq.com',
    no_proxy: '192.168.1.100,localhost,127.0.0.1,.weixin.qq.com,ilinkai.weixin.qq.com,.example.com,.amap.com,.gtimg.cn,.qq.com',
  };
  // 注:launchd 下由 run-with-env.sh 提供同样的 CLAUDE_CONFIG_DIR/代理/NO_PROXY;
  // 这里显式再设一遍是为了手动 `check` 时也能跑通(但手动运行会因钥匙串权限 401,
  // 真正命中发话术只在 launchd 上下文成立 —— 与心跳同源)。
  const out = execFileSync(CLAUDE_BIN, [
    '-p', prompt,
    '--model', 'sonnet',
    '--permission-mode', 'bypassPermissions',
    '--setting-sources', 'user',
  ], { env, cwd: ROOT, encoding: 'utf8', timeout: 120000, maxBuffer: 4 * 1024 * 1024 }).trim();
  if (!out) throw new Error('claude -p 返回空');
  return out;
}

// 命中后复用心跳的 proactive 通道:写 recent-context + last-proactive,
// 让主会话下次收到用户消息时知道自己刚开过口(替代「往主会话注入 turn」)。
function recordProactive(text) {
  try {
    appendFileSync(join(ROOT, 'memory', 'recent-context.md'),
      `[${new Date().toLocaleString('sv')}] [reply-watch] 主动提醒了用户: ${text.replace(/\n/g, ' ')}\n`);
    mkdirSync(join(ROOT, 'runtime', 'prompt-inject'), { recursive: true });
    writeFileSync(join(ROOT, 'runtime', 'prompt-inject', 'last-proactive.json'),
      JSON.stringify({ ts: iso(), source: 'reply-watch', text }, null, 0) + '\n');
  } catch (e) { log(`recordProactive 失败: ${e.message}`); }
}

// ---- 游标:按消息文本锚点,不按条数 -------------------------------------------
// Preply 的对话页只渲染最近约 20 条气泡,来一条新消息就挤掉一条最旧的。
// 所以「可见 income 条数」根本不是单调增的游标 —— 2026-09-24 实测:Tutor A 16:47
// 回了消息,可见 income 一直是 4 条,盯梢每轮都算出「没有新回复」,整整吞了 50 分钟。
// 改成记住最近几条对方消息的文本当锚点,靠锚点定位新消息。
const ANCHOR_KEEP = 8;
const msgKey = (t) => String(t || '').replace(/\s+/g, ' ').trim().slice(0, 160);

function freshIncome(income, p) {
  const keys = income.map(msgKey);
  const anchors = Array.isArray(p.seenIncomeAnchors) ? p.seenIncomeAnchors : null;
  if (anchors && anchors.length) {
    for (let ai = anchors.length - 1; ai >= 0; ai -= 1) {
      const idx = keys.lastIndexOf(anchors[ai]);
      if (idx !== -1) return { fresh: income.slice(idx + 1), matched: true };
    }
    // 一个锚点都没命中 = 窗口整段滑走(或渲染只出了尾巴)。
    // 保守只看最后一条,不把整段历史当新消息重播一遍。
    return { fresh: income.slice(-1), matched: false };
  }
  // 老盯梢只有计数游标,兼容一轮,之后就带锚点了
  return { fresh: income.slice(p.seenIncomeCount ?? 0), matched: true };
}

function setCursor(p, income) {
  p.seenIncomeAnchors = income.slice(-ANCHOR_KEEP).map(msgKey);
  p.seenIncomeCount = income.length;
}

// ---- 单条 preply_reply 盯梢的一轮处理 ---------------------------------------
async function processPreplyWatch(w) {
  const p = w.payload;
  let scraped;
  try {
    scraped = await scrapePreplyConversation(p.convHref);
  } catch (e) {
    log(`${w.watch_id} scrape 失败,跳过本轮: ${e.message}`);
    store.touchChecked(w.watch_id);
    return;
  }
  if (scraped.needLogin) { log(`${w.watch_id} 需要登录,跳过`); return; }
  if (scraped.renderFailed) { log(`${w.watch_id} 页面消息未渲染,跳过本轮(不动游标)`); store.touchChecked(w.watch_id); return; }

  const income = scraped.income;
  if (income.length === 0) { log(`${w.watch_id} 没抓到任何对方消息,疑似未加载完,跳过`); store.touchChecked(w.watch_id); return; }

  const { fresh, matched } = freshIncome(income, p);   // 锚点之后、还没推送给用户的对方消息
  if (!matched) log(`${w.watch_id} 锚点全部滑出渲染窗口,只按最后一条判断`);
  if (fresh.length === 0) {
    // 没有新回复:清掉可能残留的 pending,更新时间戳
    if (p.pending_first_at) { delete p.pending_first_at; delete p.pending_last_change; delete p.pending_count; store.updatePayload(w.watch_id, p); }
    store.touchChecked(w.watch_id);
    return;
  }

  // ---- 防抖打包(设计 §3.2) ----
  const t = now();
  if (!p.pending_first_at) {
    p.pending_first_at = t; p.pending_last_change = t; p.pending_count = fresh.length;
    store.updatePayload(w.watch_id, p);
    log(`${w.watch_id} 检测到 ${fresh.length} 条新回复,进入防抖等待`);
    return; // 首次检测先不响,等这一阵说完
  }
  if (fresh.length > p.pending_count) { p.pending_count = fresh.length; p.pending_last_change = t; store.updatePayload(w.watch_id, p); }
  const settled = t - p.pending_last_change >= SETTLE_MS;
  const maxed = t - p.pending_first_at >= MAX_WAIT_MS;
  if (!settled && !maxed) { log(`${w.watch_id} 防抖中(${fresh.length} 条待推)`); return; }

  // ---- 命中:fired 只从 armed 抢一次(不变量 3) ----
  if (!store.tryMarkFired(w.watch_id)) { log(`${w.watch_id} 抢 fired 失败(已被取消/并发),放弃`); return; }
  const fireNo = (w.fires || 0) + 1;

  let notif;
  try {
    notif = phraseNotification({ tutorName: p.tutorName, context: p.context, messages: fresh, fireNo });
  } catch (e) {
    log(`${w.watch_id} 话术生成失败,退回 armed 下轮重试: ${e.message}`);
    store.revertFiredToArmed(w.watch_id);       // 游标不动,下轮重推同一批(设计 §3.6)
    return;
  }

  // 到 20 次上限:这一发顺带告知已自动停(设计 §3.5)
  let capNote = '';
  if (fireNo >= store.MAX_FIRES) capNote = '\n（这条盯梢推送到上限自动停了，还要盯说一声）';
  const finalText = notif + capNote;

  // ---- outbox:先落库再发(设计 §3.4) ----
  const did = store.enqueueDelivery({ conversationId: CONV, text: finalText, watchId: w.watch_id });
  try {
    await sendText(finalText, {notification:{id:'reply-watch:'+did,source:'reply-watch',taskId:w.watch_id}});
    store.markDeliveryAcked(did);
    recordProactive(finalText);
    log(`${w.watch_id} 第 ${fireNo} 次命中已推送: ${finalText.slice(0, 60)}`);
  } catch (e) {
    log(`${w.watch_id} 发送失败,delivery 留 pending 下轮补投: ${e.message}`);
    // 发送失败不阻塞游标前移与否的判断:游标仍前移(消息确实到了),补投靠 outbox
  }

  // ---- 游标前移(只从 fired 转入,不变量 4),清 pending ----
  setCursor(p, income);
  delete p.pending_first_at; delete p.pending_last_change; delete p.pending_count;
  if (fireNo >= store.MAX_FIRES) {
    store.expireWatch(w.watch_id);
    store.updatePayload(w.watch_id, p);
    log(`${w.watch_id} 到 ${store.MAX_FIRES} 次上限,expired`);
  } else if (!store.advanceCursorAndArm(w.watch_id, iso(), p)) {
    log(`${w.watch_id} 游标前移失败(可能已被取消),不复活`);
  }
}

// ---- 一轮 check:补投 + 恢复残留 + 过期清理 + 逐条处理 -----------------------
async function commandCheck() {
  // 补投上轮 pending 的通知(设计 §3.4 步骤 3)
  for (const d of store.pendingDeliveries()) {
    try { await sendText(d.text, {notification:{id:'reply-watch:'+d.delivery_id,source:'reply-watch',taskId:d.watch_id || null}}); store.markDeliveryAcked(d.delivery_id); log(`补投 ${d.delivery_id} 成功`); }
    catch (e) { log(`补投 ${d.delivery_id} 失败: ${e.message}`); }
  }
  // 残留在 fired = 上次崩在注入中间,退回 armed 重推(设计 §3.6)
  const recovered = store.recoverFiredResidual();
  if (recovered) log(`恢复 ${recovered} 条残留 fired → armed`);
  // 过期收尾
  for (const w of store.sweepExpired()) {
    try { await sendText(`盯 ${w.payload?.tutorName || w.target} 回复的那个到 24 小时了，我先撤了；还要盯说一声。`); }
    catch { /* 忽略 */ }
    log(`${w.watch_id} 到期 expired`);
  }
  const armed = store.listArmed();
  if (armed.length === 0) return; // 没活跃盯梢,直接收工(不开浏览器)
  log(`本轮 ${armed.length} 条 armed 盯梢`);
  for (const w of armed) {
    if (w.kind === 'preply_reply') await processPreplyWatch(w);
    else log(`${w.watch_id} 未知 kind=${w.kind},跳过`);
  }
}

// ---- create:替用户发完消息的同一轮调用 -------------------------------------
async function commandCreate(a) {
  const kind = a.kind || 'preply_reply';
  if (kind !== 'preply_reply') throw new Error(`暂只支持 kind=preply_reply,收到 ${kind}`);
  const tutor = a.tutor || a.target;
  if (!tutor) throw new Error('create 需要 --tutor');

  // 定位该老师的对话 href,并把当前 income 条数作为游标基线(不推历史)
  const { browser, context } = await connectBrowser();
  let convHref, tutorName;
  try {
    const page = await context.newPage();
    await page.goto('https://preply.com/en/messages', { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForTimeout(5000);
    const tutors = await page.evaluate(() => Array.from(document.querySelectorAll('a[href*="/en/messages/"]'))
      .map((l) => ({ href: l.href, name: (l.innerText || '').trim().split('\n')[0] }))
      .filter((x) => x.href && x.name));
    const q = tutor.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
    const hit = tutors.find((x) => x.name.toLowerCase().includes(q.split(' ')[0]));
    await page.close().catch(() => {});
    if (!hit) throw new Error(`没在对话列表里找到老师「${tutor}」,候选:${tutors.map((x) => x.name).join(', ')}`);
    convHref = hit.href; tutorName = hit.name;
  } finally { await browser.close().catch(() => {}); }

  const scraped = await scrapePreplyConversation(convHref);
  const baseline = scraped.income.length;
  const w = store.createWatch({
    conversationId: CONV, kind: 'preply_reply', target: tutorName,
    payload: { convHref, tutorName, context: a.context || '', seenIncomeCount: baseline, seenIncomeAnchors: scraped.income.slice(-ANCHOR_KEEP).map(msgKey) },
  });
  console.log(JSON.stringify({ status: 'created', watchId: w.watch_id, tutor: tutorName, baselineIncome: baseline, expiresAt: w.expires_at }, null, 2));
}

function commandList() {
  const rows = store.listAll().map((w) => ({
    watchId: w.watch_id, kind: w.kind, target: w.target, status: w.status,
    fires: w.fires, since: w.since_ts, expiresAt: w.expires_at, context: w.payload?.context,
  }));
  console.log(JSON.stringify(rows, null, 2));
}

function commandCancel(a) {
  const id = a._[1];
  const n = id ? (store.cancelWatch(id) ? 1 : 0) : store.cancelAll();
  console.log(JSON.stringify({ status: 'cancelled', count: n, watchId: id || 'ALL' }, null, 2));
}

// ---- self-test:内存库验证状态机不变量,不碰真数据 ---------------------------
function commandSelfTest() {
  process.env.WATCH_DB_PATH = ':memory:';
  // 重新拿一个内存库:直接用底层 API 走一遍关键路径
  const w = store.createWatch({ conversationId: CONV, kind: 'preply_reply', target: 'T', payload: { seenIncomeCount: 0 } });
  const checks = [];
  checks.push(['createWatch armed', store.getWatch(w.watch_id).status === 'armed']);
  checks.push(['tryMarkFired 首次成功', store.tryMarkFired(w.watch_id) === true]);
  checks.push(['tryMarkFired 再次失败(非armed)', store.tryMarkFired(w.watch_id) === false]);
  checks.push(['advanceCursorAndArm 从fired成功', store.advanceCursorAndArm(w.watch_id, iso(), { seenIncomeCount: 3 }) === true]);
  checks.push(['游标已前移', store.getWatch(w.watch_id).payload.seenIncomeCount === 3]);
  checks.push(['回到armed', store.getWatch(w.watch_id).status === 'armed']);
  // 取消赢赛跑:fired 后取消,再 advance 应失败
  store.tryMarkFired(w.watch_id);
  checks.push(['cancel 能取消fired', store.cancelWatch(w.watch_id) === true]);
  checks.push(['取消后advance失败(不复活)', store.advanceCursorAndArm(w.watch_id, iso(), {}) === false]);
  checks.push(['终态cancelled', store.getWatch(w.watch_id).status === 'cancelled']);
  // outbox
  const did = store.enqueueDelivery({ conversationId: CONV, text: 'hi', watchId: w.watch_id });
  checks.push(['delivery pending', store.pendingDeliveries().some((d) => d.delivery_id === did)]);
  store.markDeliveryAcked(did);
  checks.push(['delivery acked 后不在pending', !store.pendingDeliveries().some((d) => d.delivery_id === did)]);
  const pass = checks.every(([, ok]) => ok);
  for (const [name, ok] of checks) console.log(`${ok ? '✓' : '✗'} ${name}`);
  console.log(pass ? '\nALL PASS' : '\nFAILED');
  process.exit(pass ? 0 : 1);
}

// ---- 入口 -------------------------------------------------------------------
const args = parseArgs(process.argv.slice(2));
const cmd = args._[0] || 'check';
try {
  if (cmd === 'check') await commandCheck();
  else if (cmd === 'create') await commandCreate(args);
  else if (cmd === 'list') commandList();
  else if (cmd === 'cancel') commandCancel(args);
  else if (cmd === 'self-test') commandSelfTest();
  else throw new Error(`未知命令: ${cmd}`);
} catch (e) {
  console.error(JSON.stringify({ status: 'error', error: e.message }));
  process.exit(1);
}
