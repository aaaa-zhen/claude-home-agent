#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { sendText } from './weixin-send.mjs';
import { appendRecentContext, formatLocalMinute } from './memory-utils.mjs';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const MEMORY_DIR = path.join(SCRIPT_DIR, 'memory');
const STATE_FILE = path.join(MEMORY_DIR, 'reading-digest-state.json');
const LOG_FILE = path.join(MEMORY_DIR, 'reading-digest-cron.log');
const LOCK_FILE = path.join(MEMORY_DIR, 'reading-digest.lock');

const args = new Set(process.argv.slice(2));
const DRY_RUN = args.has('--dry-run');
const FORCE = args.has('--force');
let LOCK_ACQUIRED = false;

// Load .env
const envPath = path.join(SCRIPT_DIR, '.env');
if (fs.existsSync(envPath)) {
  for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)$/);
    if (m) process.env[m[1]] = m[2].trim();
  }
}

const AIHUBMIX_API_KEY = process.env.AIHUBMIX_API_KEY;
const AIHUBMIX_BASE_URL = process.env.AIHUBMIX_BASE_URL || 'https://aihubmix.com/v1';

const FEEDS = [
  { source: 'Reddit', feed: 'r/todayilearned', url: 'https://www.reddit.com/r/todayilearned/top/.rss?t=day' },
  { source: 'Reddit', feed: 'r/explainlikeimfive', url: 'https://www.reddit.com/r/explainlikeimfive/top/.rss?t=day' },
  { source: 'Reddit', feed: 'r/YouShouldKnow', url: 'https://www.reddit.com/r/YouShouldKnow/top/.rss?t=day' },
  { source: 'Reddit', feed: 'r/Futurology', url: 'https://www.reddit.com/r/Futurology/top/.rss?t=day' },
  { source: 'Reddit', feed: 'r/technology', url: 'https://www.reddit.com/r/technology/top/.rss?t=day' },
  { source: 'Reddit', feed: 'r/science', url: 'https://www.reddit.com/r/science/top/.rss?t=day' },
  { source: 'BBC', feed: 'World', url: 'https://feeds.bbci.co.uk/news/world/rss.xml' },
  { source: 'BBC', feed: 'Technology', url: 'https://feeds.bbci.co.uk/news/technology/rss.xml' },
  { source: 'BBC', feed: 'Science', url: 'https://feeds.bbci.co.uk/news/science_and_environment/rss.xml' },
  { source: 'BBC', feed: 'Business', url: 'https://feeds.bbci.co.uk/news/business/rss.xml' },
  { source: 'The Guardian', feed: 'World', url: 'https://www.theguardian.com/world/rss' },
  { source: 'The Guardian', feed: 'Technology', url: 'https://www.theguardian.com/technology/rss' },
  { source: 'The Guardian', feed: 'Science', url: 'https://www.theguardian.com/science/rss' },
  { source: 'The Guardian', feed: 'Environment', url: 'https://www.theguardian.com/environment/rss' },
];

function log(message) {
  const line = `[${new Date().toISOString()}] ${message}`;
  fs.mkdirSync(MEMORY_DIR, { recursive: true });
  fs.appendFileSync(LOG_FILE, `${line}\n`, 'utf8');
  if (process.stdout.isTTY) console.log(line);
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
    const response = await fetch(url, {
      signal: controller.signal,
      headers: {
        'User-Agent': 'weixin-agent-reading-digest/1.0 (personal English learning digest)',
        'Accept': 'application/rss+xml, application/atom+xml, text/xml, text/html;q=0.8, */*;q=0.5',
      },
    });
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
    const content = firstMatch(entry, /<content[^>]*>([\s\S]*?)<\/content>/);
    const summary = stripTags(content).slice(0, 420);
    const updated = firstMatch(entry, /<updated>([\s\S]*?)<\/updated>/) || firstMatch(entry, /<published>([\s\S]*?)<\/published>/);
    return normalizeCandidate({
      ...feedConfig,
      title,
      url: linkMatch ? decodeXml(linkMatch[1]) : '',
      summary,
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
    title,
    url,
    summary: stripTags(candidate.summary || ''),
    rank: candidate.rank || 99,
    publishedMs,
  };
}

async function enrichWithArticleMeta(candidate) {
  if (candidate.source === 'Reddit') return candidate;
  try {
    const html = await fetchText(candidate.url, 9000);
    const metaDescription = firstMatch(html, /<meta\s+[^>]*(?:name|property)=["'](?:description|og:description)["'][^>]*content=["']([^"']+)["'][^>]*>/i)
      || firstMatch(html, /<meta\s+[^>]*content=["']([^"']+)["'][^>]*(?:name|property)=["'](?:description|og:description)["'][^>]*>/i);
    const clean = stripTags(metaDescription).slice(0, 520);
    if (clean && clean.length > (candidate.summary || '').length) {
      return { ...candidate, summary: clean };
    }
  } catch (err) {
    log(`article meta failed: ${candidate.source} ${err.message || err}`);
  }
  return candidate;
}

function scoreCandidate(candidate, sentSources) {
  const sourcePenalty = sentSources.slice(-4).includes(candidate.source) ? 8 : 0;
  const recencyBoost = candidate.publishedMs ? Math.max(0, 4 - ((Date.now() - candidate.publishedMs) / 3600000)) : 0;
  return 20 - candidate.rank - sourcePenalty + recencyBoost;
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

function pickCandidate(candidates, state) {
  const sentUrls = new Set((state.sentItems || []).map((item) => item.url));
  const sentSources = (state.sentItems || []).map((item) => item.source);
  const fresh = candidates.filter((candidate) => !sentUrls.has(candidate.url));
  const pool = fresh.length ? fresh : candidates;
  pool.sort((a, b) => scoreCandidate(b, sentSources) - scoreCandidate(a, sentSources));
  return pool[0];
}

async function buildMessageWithAI(candidate) {
  if (!AIHUBMIX_API_KEY) {
    throw new Error('AIHUBMIX_API_KEY not set in .env');
  }

  const prompt = `You are an English learning content writer. Write a B2-level reading digest based on this news article.

Article title: ${candidate.title}
Article summary: ${candidate.summary || '(no summary available)'}
Source: ${candidate.source} - ${candidate.feed}
URL: ${candidate.url}

Follow this exact structure in your output:

Line 1: The article title (copy exactly)
Line 2: (blank)
Lines 3+: Write a complete B2-level news story. 4-6 paragraphs. Natural, engaging English with context and background. No markdown. Do not start with "In" or "This article".
After story: (blank line)
Then write: Useful words:
Then write exactly 5 lines, each formatted as: - word = short definition in simple English
Choose real words from your story that B2 learners would find useful.
Then: (blank line)
Then write: Original link: ${candidate.url}

Output ONLY the above. No extra commentary.`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch(`${AIHUBMIX_BASE_URL}/chat/completions`, {
      method: 'POST',
      signal: controller.signal,
      headers: {
        'Authorization': `Bearer ${AIHUBMIX_API_KEY}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        model: 'gpt-4o-mini',
        messages: [{ role: 'user', content: prompt }],
        max_tokens: 1000,
        temperature: 0.7,
      }),
    });
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
    log('skip: another reading-digest run is still active');
    return;
  }

  const state = readState();
  const candidates = await fetchCandidates();
  if (!candidates.length) throw new Error('No reading candidates fetched.');
  let candidate = pickCandidate(candidates, state);
  candidate = await enrichWithArticleMeta(candidate);

  log(`generating AI story for: ${candidate.title}`);
  const message = await buildMessageWithAI(candidate);

  if (DRY_RUN) {
    console.log(message);
    return;
  }

  if (!FORCE && (state.sentItems || []).some((item) => item.url === candidate.url)) {
    log(`skip: already sent ${candidate.url}`);
    return;
  }

  const result = await sendText(message);
  state.sentItems = [
    ...(state.sentItems || []),
    {
      sentAt: new Date().toISOString(),
      source: candidate.source,
      feed: candidate.feed,
      title: candidate.title,
      url: candidate.url,
      rank: candidate.rank,
      toUserId: result.toUserId,
    },
  ];
  writeState(state);
  // Write to recent-context.md so Claude session knows what was sent.
  try {
    const now = formatLocalMinute();
    const contextLine = `[${now}] [cron:reading-digest] 推送了英语阅读卡片：${candidate.source}/${candidate.feed} - ${candidate.title}`;
    appendRecentContext(MEMORY_DIR, contextLine, { maxEntries: 10 });
  } catch (err) {
    log(`failed to write recent-context: ${err.message}`);
  }

  log(`sent: ${candidate.source} ${candidate.feed} rank=${candidate.rank} to=${result.toUserId}`);
}

main()
  .catch((err) => {
    log(`error: ${err.stack || err.message || err}`);
    process.exitCode = 1;
  })
  .finally(() => {
    releaseLock();
  });
