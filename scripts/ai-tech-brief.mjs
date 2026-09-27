#!/usr/bin/env node
// ai-tech-brief.mjs — 每日 AI/科技晨报:抓 RSS → 取当天热门 → 大模型揉成一条中文晨报 → 发微信。
// 结构照抄 reading-digest.mjs(RSS 解析/代理/state/lock 工具),区别是聚合多条而非挑一篇。
// 用法:  node scripts/ai-tech-brief.mjs [--dry-run] [--force]
// cron:  0 8 * * *  (见 CLAUDE.md「主动推送」;纯 node 脚本,不调 claude,cron 即可)
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { ProxyAgent } from 'undici';
import { sendText } from '../weixin-send.mjs';
import { appendRecentContext, formatLocalMinute } from '../memory-utils.mjs';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const ROOT_DIR = path.resolve(SCRIPT_DIR, '..');
const MEMORY_DIR = path.join(ROOT_DIR, 'memory');
const STATE_FILE = path.join(MEMORY_DIR, 'ai-tech-brief-state.json');
const LOG_FILE = path.join(MEMORY_DIR, 'ai-tech-brief-cron.log');
const LOCK_FILE = path.join(MEMORY_DIR, 'ai-tech-brief.lock');

const args = new Set(process.argv.slice(2));
const DRY_RUN = args.has('--dry-run');
const FORCE = args.has('--force');
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

// AI 专向优先(weight 2),通用科技(weight 1)。抓不到的源静默跳过。
// 砍掉 HN 前页(纯噪音,混进暗物质/影评)和 VentureBeat(feed 308 跳转已废)。
const FEEDS = [
  { source: 'TechCrunch', feed: 'AI', weight: 2, url: 'https://techcrunch.com/category/artificial-intelligence/feed/' },
  { source: 'MIT Tech Review', feed: 'AI', weight: 2, url: 'https://www.technologyreview.com/topic/artificial-intelligence/feed' },
  { source: 'The Verge', feed: 'Tech', weight: 1, url: 'https://www.theverge.com/rss/index.xml' },
  { source: 'Ars Technica', feed: 'Tech', weight: 1, url: 'https://feeds.arstechnica.com/arstechnica/index' },
  { source: 'BBC', feed: 'Technology', weight: 1, url: 'https://feeds.bbci.co.uk/news/technology/rss.xml' },
];

const ITEMS_IN_BRIEF = 8;

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
  try {
    fs.unlinkSync(LOCK_FILE);
  } catch {}
}

function readState() {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
  } catch {
    return { sentItems: [] };
  }
}

function writeState(state) {
  fs.mkdirSync(MEMORY_DIR, { recursive: true });
  const cutoff = Date.now() - 30 * 24 * 3600 * 1000;
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
        'User-Agent': 'weixin-agent-ai-tech-brief/1.0 (personal daily tech digest)',
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
  return entries.slice(0, 8).map((entry, index) => {
    const title = firstMatch(entry, /<title[^>]*>([\s\S]*?)<\/title>/);
    const linkMatch = entry.match(/<link\s+[^>]*href="([^"]+)"/);
    const content = firstMatch(entry, /<content[^>]*>([\s\S]*?)<\/content>/)
      || firstMatch(entry, /<summary[^>]*>([\s\S]*?)<\/summary>/);
    const updated = firstMatch(entry, /<updated>([\s\S]*?)<\/updated>/) || firstMatch(entry, /<published>([\s\S]*?)<\/published>/);
    return normalizeCandidate({
      ...feedConfig,
      title,
      url: linkMatch ? decodeXml(linkMatch[1]) : '',
      summary: stripTags(content).slice(0, 420),
      publishedAt: updated,
      rank: index + 1,
    });
  }).filter(Boolean);
}

function parseRssItems(feedConfig, xml) {
  const items = [...xml.matchAll(/<item>([\s\S]*?)<\/item>/g)].map((m) => m[1]);
  return items.slice(0, 8).map((item, index) => {
    const title = firstMatch(item, /<title[^>]*>([\s\S]*?)<\/title>/);
    const link = firstMatch(item, /<link[^>]*>([\s\S]*?)<\/link>/) || firstMatch(item, /<guid[^>]*>([\s\S]*?)<\/guid>/);
    const description = firstMatch(item, /<description[^>]*>([\s\S]*?)<\/description>/);
    const content = firstMatch(item, /<content:encoded[^>]*>([\s\S]*?)<\/content:encoded>/);
    const pubDate = firstMatch(item, /<pubDate[^>]*>([\s\S]*?)<\/pubDate>/);
    return normalizeCandidate({
      ...feedConfig,
      title,
      url: link,
      summary: stripTags(content || description).slice(0, 420),
      publishedAt: pubDate,
      rank: index + 1,
    });
  }).filter(Boolean);
}

function normalizeCandidate(candidate) {
  const title = stripTags(candidate.title || '').replace(/\s+-\s+BBC News$/i, '').trim();
  const url = String(candidate.url || '').trim();
  if (!title || !url || /\b(live|updates|newsletter)\b/i.test(title)) return null;
  const publishedMs = Date.parse(candidate.publishedAt || '') || 0;
  return {
    source: candidate.source,
    feed: candidate.feed,
    weight: candidate.weight || 1,
    title,
    url,
    summary: stripTags(candidate.summary || ''),
    rank: candidate.rank || 99,
    publishedMs,
  };
}

// 越新、越靠前、AI 专向源(weight)得分越高。
function scoreCandidate(candidate) {
  const recencyBoost = candidate.publishedMs ? Math.max(0, 12 - ((Date.now() - candidate.publishedMs) / 3600000)) : 0;
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

// 去掉最近发过的,按分数取前 N 条,尽量不让单一来源霸屏(每源最多 3 条)。
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

async function buildBriefWithAI(items) {
  if (!AIHUBMIX_API_KEY) {
    throw new Error('AIHUBMIX_API_KEY not set in .env');
  }

  const today = formatLocalMinute().slice(0, 10);
  const articleBlock = items.map((it, i) =>
    `${i + 1}. [${it.source}/${it.feed}] ${it.title}\n   摘要: ${it.summary || '(无)'}`
  ).join('\n');

  const prompt = `你是一个中文科技晨报编辑。下面是今天抓到的 ${items.length} 条 AI/科技英文新闻,请帮 Zhen 揉成一条微信晨报。

今天日期: ${today}
新闻列表:
${articleBlock}

输出要求:
- 开头一行: 「☀️ ${today} AI科技晨报」
- 然后分两块: 先「🤖 AI」再「💡 科技」,每块下面把相关的新闻各写一条
- 每条: 一句话中文,先给最关键的信息/为什么值得看,再括号标来源。控制在 40 字内,别翻译腔
- 总共 6-8 条,挑最有信息量的,重复/无聊的丢掉
- 不要 markdown 符号(#、*、-),微信里不渲染;用 emoji 和换行分隔即可
- 结尾一行轻松的话收尾,别啰嗦
- 只输出晨报正文,不要任何解释`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 40000);
  try {
    const response = await fetch(`${AIHUBMIX_BASE_URL}/chat/completions`, fetchOptions({
      method: 'POST',
      signal: controller.signal,
      headers: {
        'Authorization': `Bearer ${AIHUBMIX_API_KEY}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        model: 'gpt-4o-mini',
        messages: [{ role: 'user', content: prompt }],
        max_tokens: 1200,
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

async function main() {
  if (!acquireLock()) {
    log('skip: another ai-tech-brief run is still active');
    return;
  }

  const state = readState();
  const candidates = await fetchCandidates();
  if (!candidates.length) throw new Error('No AI/tech candidates fetched.');
  const items = pickTopCandidates(candidates, state, ITEMS_IN_BRIEF);
  if (!items.length) throw new Error('No fresh candidates to summarize.');

  log(`building brief from ${items.length} items (of ${candidates.length} candidates)`);
  const message = await buildBriefWithAI(items);

  if (DRY_RUN) {
    console.log(message);
    console.log('\n--- 选用来源 ---');
    for (const it of items) console.log(`  [${it.source}/${it.feed}] ${it.title}`);
    return;
  }

  const result = await sendText(message);

  state.sentItems = [
    ...(state.sentItems || []),
    ...items.map((it) => ({
      sentAt: new Date().toISOString(),
      source: it.source,
      feed: it.feed,
      title: it.title,
      url: it.url,
    })),
  ];
  writeState(state);

  try {
    const now = formatLocalMinute();
    const contextLine = `[${now}] [cron:ai-tech-brief] 推送了 AI/科技晨报(${items.length} 条),来源: ${[...new Set(items.map((i) => i.source))].join('、')}`;
    appendRecentContext(MEMORY_DIR, contextLine, { maxEntries: 10 });
  } catch (err) {
    log(`failed to write recent-context: ${err.message}`);
  }

  log(`sent brief: ${items.length} items to=${result.toUserId}`);
}

main()
  .catch((err) => {
    log(`error: ${err.stack || err.message || err}`);
    process.exitCode = 1;
  })
  .finally(() => {
    releaseLock();
  });
