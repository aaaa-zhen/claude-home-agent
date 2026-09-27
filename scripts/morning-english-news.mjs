#!/usr/bin/env node
// morning-english-news.mjs — 每天早 8:00 的英文新闻:抓英文世界新闻 RSS → 大模型揉成
// 约 5 分钟英文口播稿 → 切成 <480 字小段 → 顺序丢给客厅 HomePod 播报。
// 结构参照 ai-tech-brief.mjs(RSS 解析/代理/state/lock),区别是输出英文口播稿并走 HomePod TTS。
// 用法:  node scripts/morning-english-news.mjs [--dry-run] [--force]
// 定时:  launchd com.zhen.morning-english-news, 每天 08:00(见 CLAUDE.md「主动推送」)
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { ProxyAgent } from 'undici';
import { appendRecentContext, formatLocalMinute } from '../memory-utils.mjs';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const ROOT_DIR = path.resolve(SCRIPT_DIR, '..');
const MEMORY_DIR = path.join(ROOT_DIR, 'memory');
const STATE_FILE = path.join(MEMORY_DIR, 'morning-english-news-state.json');
const LOG_FILE = path.join(MEMORY_DIR, 'morning-english-news-cron.log');
const LOCK_FILE = path.join(MEMORY_DIR, 'morning-english-news.lock');
const HOMEPOD_SAY = path.join(ROOT_DIR, 'scripts', 'homepod-say.py');
const PYTHON = path.join(ROOT_DIR, 'venv', 'bin', 'python');

const args = new Set(process.argv.slice(2));
const DRY_RUN = args.has('--dry-run');
let LOCK_ACQUIRED = false;

// Load .env
const envPath = path.join(ROOT_DIR, '.env');
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)$/);
    if (m) process.env[m[1]] = m[2].trim();
  }
}

const AIHUBMIX_API_KEY = process.env.AIHUBMIX_API_KEY;
const AIHUBMIX_BASE_URL = process.env.AIHUBMIX_BASE_URL || 'https://aihubmix.com/v1';
const PROXY_URL = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.https_proxy || process.env.http_proxy || '';
const PROXY_AGENT = PROXY_URL ? new ProxyAgent(PROXY_URL) : null;

// 英文世界/综合新闻源。抓不到的静默跳过。
const FEEDS = [
  { source: 'BBC', feed: 'World', weight: 2, url: 'https://feeds.bbci.co.uk/news/world/rss.xml' },
  { source: 'NPR', feed: 'News', weight: 2, url: 'https://feeds.npr.org/1001/rss.xml' },
  { source: 'The Guardian', feed: 'World', weight: 1, url: 'https://www.theguardian.com/world/rss' },
  { source: 'Al Jazeera', feed: 'World', weight: 1, url: 'https://www.aljazeera.com/xml/rss/all.xml' },
];

const ITEMS_IN_BRIEF = 7;
const MAX_CHUNK_CHARS = 460; // homepod-say.py 上限 500,留余量

function log(message) {
  const line = `[${new Date().toISOString()}] ${message}`;
  fs.mkdirSync(MEMORY_DIR, { recursive: true });
  fs.appendFileSync(LOG_FILE, `${line}\n`, 'utf8');
  if (process.stdout.isTTY) console.log(line);
}

function fetchOptions(options = {}) {
  return PROXY_AGENT ? { ...options, dispatcher: PROXY_AGENT } : options;
}

function acquireLock() {
  fs.mkdirSync(MEMORY_DIR, { recursive: true });
  try {
    const fd = fs.openSync(LOCK_FILE, 'wx');
    fs.writeFileSync(fd, JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString() }));
    fs.closeSync(fd);
    LOCK_ACQUIRED = true;
    return true;
  } catch (err) {
    if (err.code === 'EEXIST') {
      try {
        const stat = fs.statSync(LOCK_FILE);
        if (Date.now() - stat.mtimeMs > 30 * 60 * 1000) {
          fs.unlinkSync(LOCK_FILE);
          return acquireLock();
        }
      } catch {}
      return false;
    }
    throw err;
  }
}

function releaseLock() {
  if (!LOCK_ACQUIRED) return;
  try { fs.unlinkSync(LOCK_FILE); } catch {}
}

function readState() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); }
  catch { return { sentItems: [] }; }
}

function writeState(state) {
  fs.mkdirSync(MEMORY_DIR, { recursive: true });
  const cutoff = Date.now() - 14 * 24 * 3600 * 1000;
  const sentItems = (state.sentItems || [])
    .filter((item) => Date.parse(item.sentAt || 0) >= cutoff)
    .slice(-1500);
  fs.writeFileSync(STATE_FILE, JSON.stringify({ ...state, sentItems }, null, 2), 'utf8');
}

function decodeXml(input = '') {
  return input
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#x([0-9a-f]+);/gi, (_, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, dec) => String.fromCodePoint(parseInt(dec, 10)));
}

function stripTags(input = '') {
  return decodeXml(input.replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ').trim();
}

function firstMatch(text, regex) {
  const match = text.match(regex);
  return match ? decodeXml(match[1].trim()) : '';
}

async function fetchText(url, timeoutMs = 12000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, fetchOptions({
      signal: controller.signal,
      headers: {
        'User-Agent': 'weixin-agent-morning-english-news/1.0 (personal daily news brief)',
        'Accept': 'application/rss+xml, application/atom+xml, text/xml, text/html;q=0.8, */*;q=0.5',
      },
    }));
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.text();
  } finally {
    clearTimeout(timer);
  }
}

function parseFeedEntries(feedConfig, xml) {
  if (xml.includes('<entry>')) return parseAtomEntries(feedConfig, xml);
  return parseRssItems(feedConfig, xml);
}

function parseAtomEntries(feedConfig, xml) {
  const entries = [...xml.matchAll(/<entry>([\s\S]*?)<\/entry>/g)].map((m) => m[1]);
  return entries.slice(0, 10).map((entry, index) => {
    const title = firstMatch(entry, /<title[^>]*>([\s\S]*?)<\/title>/);
    const linkMatch = entry.match(/<link\s+[^>]*href="([^"]+)"/);
    const content = firstMatch(entry, /<content[^>]*>([\s\S]*?)<\/content>/)
      || firstMatch(entry, /<summary[^>]*>([\s\S]*?)<\/summary>/);
    const updated = firstMatch(entry, /<updated>([\s\S]*?)<\/updated>/) || firstMatch(entry, /<published>([\s\S]*?)<\/published>/);
    return normalizeCandidate({
      ...feedConfig, title,
      url: linkMatch ? decodeXml(linkMatch[1]) : '',
      summary: stripTags(content).slice(0, 500),
      publishedAt: updated, rank: index + 1,
    });
  }).filter(Boolean);
}

function parseRssItems(feedConfig, xml) {
  const items = [...xml.matchAll(/<item>([\s\S]*?)<\/item>/g)].map((m) => m[1]);
  return items.slice(0, 10).map((item, index) => {
    const title = firstMatch(item, /<title[^>]*>([\s\S]*?)<\/title>/);
    const link = firstMatch(item, /<link[^>]*>([\s\S]*?)<\/link>/) || firstMatch(item, /<guid[^>]*>([\s\S]*?)<\/guid>/);
    const description = firstMatch(item, /<description[^>]*>([\s\S]*?)<\/description>/);
    const content = firstMatch(item, /<content:encoded[^>]*>([\s\S]*?)<\/content:encoded>/);
    const pubDate = firstMatch(item, /<pubDate[^>]*>([\s\S]*?)<\/pubDate>/);
    return normalizeCandidate({
      ...feedConfig, title, url: link,
      summary: stripTags(content || description).slice(0, 500),
      publishedAt: pubDate, rank: index + 1,
    });
  }).filter(Boolean);
}

function normalizeCandidate(candidate) {
  const title = stripTags(candidate.title || '').trim();
  const url = String(candidate.url || '').trim();
  if (!title || !url || /\b(newsletter|quiz|podcast|crossword)\b/i.test(title)) return null;
  const publishedMs = Date.parse(candidate.publishedAt || '') || 0;
  return {
    source: candidate.source, feed: candidate.feed, weight: candidate.weight || 1,
    title, url, summary: stripTags(candidate.summary || ''),
    rank: candidate.rank || 99, publishedMs,
  };
}

function scoreCandidate(candidate) {
  const recencyBoost = candidate.publishedMs ? Math.max(0, 16 - ((Date.now() - candidate.publishedMs) / 3600000)) : 0;
  return (candidate.weight * 6) - candidate.rank + recencyBoost;
}

async function fetchCandidates() {
  const batches = await Promise.allSettled(FEEDS.map(async (feed) => {
    const xml = await fetchText(feed.url);
    return parseFeedEntries(feed, xml);
  }));
  const candidates = [];
  for (const result of batches) {
    if (result.status === 'fulfilled') candidates.push(...result.value);
    else log(`feed failed: ${result.reason?.message || result.reason}`);
  }
  const byUrl = new Map();
  for (const candidate of candidates) {
    if (!byUrl.has(candidate.url)) byUrl.set(candidate.url, candidate);
  }
  return [...byUrl.values()];
}

function pickTopCandidates(candidates, state, limit) {
  const sentUrls = new Set((state.sentItems || []).map((item) => item.url));
  const fresh = candidates.filter((c) => !sentUrls.has(c.url));
  const pool = fresh.length >= limit ? fresh : candidates;
  pool.sort((a, b) => scoreCandidate(b) - scoreCandidate(a));
  const picked = [];
  const perSource = {};
  for (const c of pool) {
    perSource[c.source] = perSource[c.source] || 0;
    if (perSource[c.source] >= 3) continue;
    perSource[c.source] += 1;
    picked.push(c);
    if (picked.length >= limit) break;
  }
  return picked;
}

async function buildScriptWithAI(items) {
  if (!AIHUBMIX_API_KEY) throw new Error('AIHUBMIX_API_KEY not set in .env');

  const today = formatLocalMinute().slice(0, 10);
  const articleBlock = items.map((it, i) =>
    `${i + 1}. [${it.source}] ${it.title}\n   summary: ${it.summary || '(none)'}`
  ).join('\n');

  const prompt = `You are a radio news anchor writing a short spoken English news briefing for a listener named Zhen, to be read aloud by a text-to-speech voice on a smart speaker. Today is ${today}.

Here are today's top stories pulled from world news feeds:
${articleBlock}

Write a natural, spoken-word news briefing:
- Around 650-750 words (about 5 minutes when read aloud). Do NOT go over 800 words.
- Start with a warm, brief greeting, e.g. "Good morning, Zhen. Here's your English news briefing for ${today}." Then dive in.
- Cover the 5-7 most important and distinct stories. Skip anything trivial, duplicated, or clickbait.
- For each story: one or two clear sentences on what happened and why it matters. Conversational, plain English, easy to follow by ear. No jargon dumps.
- Use smooth spoken transitions ("In other news,", "Meanwhile,", "Turning to..."). This is meant to be HEARD, not read.
- Plain sentences only. NO markdown, NO bullet points, NO headings, NO emoji, NO URLs, NO source citations like "(BBC)". Just flowing spoken paragraphs.
- End with a short, friendly sign-off wishing Zhen a good day.
- Output ONLY the briefing text, nothing else.`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 60000);
  try {
    const response = await fetch(`${AIHUBMIX_BASE_URL}/chat/completions`, fetchOptions({
      method: 'POST',
      signal: controller.signal,
      headers: { 'Authorization': `Bearer ${AIHUBMIX_API_KEY}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'gpt-4o',
        messages: [{ role: 'user', content: prompt }],
        max_tokens: 1400,
        temperature: 0.6,
      }),
    }));
    if (!response.ok) {
      const err = await response.text();
      throw new Error(`AiHubMix API error ${response.status}: ${err}`);
    }
    const data = await response.json();
    return data.choices[0].message.content.trim();
  } finally {
    clearTimeout(timer);
  }
}

// 按句子边界切成 <=MAX_CHUNK_CHARS 的小段,给 homepod-say 顺序播。
function chunkForSpeech(text) {
  const clean = text.replace(/\s+/g, ' ').trim();
  const sentences = clean.match(/[^.!?]+[.!?]+["'”’)]?|\S+$/g) || [clean];
  const chunks = [];
  let cur = '';
  for (const s of sentences) {
    const piece = s.trim();
    if (!piece) continue;
    if ((cur + ' ' + piece).trim().length > MAX_CHUNK_CHARS) {
      if (cur) chunks.push(cur.trim());
      if (piece.length > MAX_CHUNK_CHARS) {
        // 极长单句再按逗号/空格硬切
        let rest = piece;
        while (rest.length > MAX_CHUNK_CHARS) {
          let cut = rest.lastIndexOf(',', MAX_CHUNK_CHARS);
          if (cut < 200) cut = rest.lastIndexOf(' ', MAX_CHUNK_CHARS);
          if (cut < 1) cut = MAX_CHUNK_CHARS;
          chunks.push(rest.slice(0, cut).trim());
          rest = rest.slice(cut).trim();
        }
        cur = rest;
      } else {
        cur = piece;
      }
    } else {
      cur = (cur ? cur + ' ' : '') + piece;
    }
  }
  if (cur.trim()) chunks.push(cur.trim());
  return chunks;
}

function sayOnHomePod(text) {
  return new Promise((resolve) => {
    const child = spawn(PYTHON, [HOMEPOD_SAY, text, '--provider', 'openai', '--voice', 'nova'], {
      cwd: ROOT_DIR,
    });
    let out = '';
    child.stdout.on('data', (d) => { out += d.toString(); });
    child.stderr.on('data', (d) => { out += d.toString(); });
    child.on('close', (code) => {
      let ok = false;
      const lastLine = out.trim().split('\n').filter(Boolean).pop() || '';
      try { ok = JSON.parse(lastLine).ok === true; } catch {}
      resolve({ ok: code === 0 && ok, code, tail: lastLine });
    });
    child.on('error', (err) => resolve({ ok: false, code: -1, tail: err.message }));
  });
}

async function main() {
  if (!acquireLock()) {
    log('skip: another morning-english-news run is still active');
    return;
  }

  const state = readState();
  const candidates = await fetchCandidates();
  if (!candidates.length) throw new Error('No English news candidates fetched.');
  const items = pickTopCandidates(candidates, state, ITEMS_IN_BRIEF);
  if (!items.length) throw new Error('No fresh candidates to summarize.');

  log(`building news script from ${items.length} items (of ${candidates.length} candidates)`);
  const script = await buildScriptWithAI(items);
  const chunks = chunkForSpeech(script);
  log(`script ${script.length} chars -> ${chunks.length} chunk(s)`);

  if (DRY_RUN) {
    console.log(script);
    console.log(`\n--- ${chunks.length} chunks ---`);
    chunks.forEach((c, i) => console.log(`[${i + 1}] (${c.length}) ${c.slice(0, 80)}...`));
    console.log('\n--- sources ---');
    for (const it of items) console.log(`  [${it.source}/${it.feed}] ${it.title}`);
    return;
  }

  let played = 0;
  for (let i = 0; i < chunks.length; i++) {
    const res = await sayOnHomePod(chunks[i]);
    if (res.ok) played += 1;
    else log(`chunk ${i + 1}/${chunks.length} failed: code=${res.code} tail=${res.tail.slice(0, 200)}`);
  }
  log(`played ${played}/${chunks.length} chunks on HomePod`);

  if (played === 0) throw new Error('HomePod played 0 chunks — playback failed.');

  state.sentItems = [
    ...(state.sentItems || []),
    ...items.map((it) => ({ sentAt: new Date().toISOString(), source: it.source, feed: it.feed, title: it.title, url: it.url })),
  ];
  writeState(state);

  try {
    const now = formatLocalMinute();
    const contextLine = `[${now}] [cron:morning-english-news] 客厅 HomePod 播了英文新闻(${items.length} 条,${played}/${chunks.length} 段),来源: ${[...new Set(items.map((i) => i.source))].join('、')}`;
    appendRecentContext(MEMORY_DIR, contextLine, { maxEntries: 8 });
  } catch (err) {
    log(`failed to write recent-context: ${err.message}`);
  }

  log(`done: ${items.length} items, ${played}/${chunks.length} chunks played`);
}

main()
  .catch((err) => {
    log(`error: ${err.stack || err.message || err}`);
    process.exitCode = 1;
  })
  .finally(() => { releaseLock(); });
