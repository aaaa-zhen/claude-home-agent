#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { sendText } from './weixin-send.mjs';

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const MEMORY_DIR = path.join(SCRIPT_DIR, 'memory');
const STATE_FILE = path.join(MEMORY_DIR, 'reddit-daily-state.json');
const LOG_FILE = path.join(MEMORY_DIR, 'reddit-daily-cron.log');
const LOCK_FILE = path.join(MEMORY_DIR, 'reddit-daily.lock');
const DEFAULT_SUBREDDITS = ['todayilearned', 'explainlikeimfive', 'YouShouldKnow', 'LifeProTips', 'Futurology', 'technology', 'science'];
const SUBREDDITS = (process.env.REDDIT_DAILY_SUBREDDITS || DEFAULT_SUBREDDITS.join(','))
  .split(',')
  .map((s) => s.trim().replace(/^r\//i, ''))
  .filter(Boolean);
const args = new Set(process.argv.slice(2));
const DRY_RUN = args.has('--dry-run');
const FORCE = args.has('--force');
let LOCK_ACQUIRED = false;

function localDateKey(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

function log(message) {
  const line = `[${new Date().toISOString()}] ${message}`;
  fs.mkdirSync(MEMORY_DIR, { recursive: true });
  fs.appendFileSync(LOG_FILE, `${line}\n`, 'utf8');
  // Cron redirects stdout to LOG_FILE. Only echo interactively to avoid duplicate lines.
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
        const ageMs = Date.now() - stat.mtimeMs;
        if (ageMs > 30 * 60 * 1000) {
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
    return { sentPosts: [] };
  }
}

function writeState(state) {
  fs.mkdirSync(MEMORY_DIR, { recursive: true });
  const cutoff = Date.now() - 45 * 24 * 3600 * 1000;
  const sentPosts = (state.sentPosts || []).filter((item) => Date.parse(item.sentAt || 0) >= cutoff).slice(-100);
  fs.writeFileSync(STATE_FILE, JSON.stringify({ ...state, sentPosts }, null, 2), 'utf8');
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

async function fetchSubreddit(sub) {
  const jsonUrl = `https://www.reddit.com/r/${encodeURIComponent(sub)}/top.json?t=day&limit=8`;
  try {
    const jsonResponse = await fetch(jsonUrl, {
      headers: { 'User-Agent': 'weixin-agent-reddit-daily/1.1 (English learning reminder)' },
    });
    if (jsonResponse.ok) {
      const payload = await jsonResponse.json();
      const children = payload?.data?.children || [];
      return children.map((child, index) => {
        const data = child.data || {};
        return {
          sub,
          title: data.title || '',
          url: data.permalink ? `https://www.reddit.com${data.permalink}` : data.url || '',
          score: Number(data.score || 0),
          comments: Number(data.num_comments || 0),
          summary: String(data.selftext || '').replace(/\s+/g, ' ').trim().slice(0, 220),
          rank: index + 1,
        };
      }).filter((post) => post.title && post.url && !/\bmegathread\b/i.test(post.title));
    }
  } catch (err) {
    log(`json fetch failed: r/${sub} ${err.message || err}`);
  }

  const url = `https://www.reddit.com/r/${encodeURIComponent(sub)}/top/.rss?t=day`;
  const response = await fetch(url, {
    headers: { 'User-Agent': 'weixin-agent-reddit-daily/1.0 (English learning reminder)' },
  });
  if (!response.ok) throw new Error(`r/${sub} HTTP ${response.status}`);
  const rss = await response.text();
  const entries = [...rss.matchAll(/<entry>([\s\S]*?)<\/entry>/g)].map((m) => m[1]);
  return entries.slice(0, 5).map((entry, index) => {
    const title = firstMatch(entry, /<title>([\s\S]*?)<\/title>/);
    const urlMatch = entry.match(/<link\s+href="([^"]+)"/);
    const content = firstMatch(entry, /<content[^>]*>([\s\S]*?)<\/content>/);
    const summary = stripTags(content).slice(0, 220);
    const scoreMatch = content.match(/(\d[\d,]*)\s+points?/i);
    const commentsMatch = content.match(/(\d[\d,]*)\s+comments?/i);
    const score = scoreMatch ? Number(scoreMatch[1].replace(/,/g, '')) : null;
    const comments = commentsMatch ? Number(commentsMatch[1].replace(/,/g, '')) : null;
    return {
      sub,
      title,
      url: urlMatch ? decodeXml(urlMatch[1]) : '',
      score,
      comments,
      summary,
      rank: index + 1,
    };
  }).filter((post) => post.title && post.url && !/\bmegathread\b/i.test(post.title));
}

async function fetchCandidates() {
  const batches = await Promise.allSettled(SUBREDDITS.map(fetchSubreddit));
  const candidates = [];
  for (const result of batches) {
    if (result.status === 'fulfilled') candidates.push(...result.value);
    else log(`fetch failed: ${result.reason?.message || result.reason}`);
  }
  candidates.sort((a, b) => ((b.score ?? -1) - (a.score ?? -1)) || ((b.comments ?? -1) - (a.comments ?? -1)) || (a.rank - b.rank));
  return candidates;
}

function pickPost(candidates, state) {
  const sentUrls = new Set((state.sentPosts || []).map((item) => item.url));
  return candidates.find((post) => !sentUrls.has(post.url)) || candidates[0];
}

function formatCount(num, suffix) {
  if (!Number.isFinite(num) || num <= 0) return '';
  return `${num.toLocaleString('en-US')} ${suffix}`;
}

function buildMessage(post) {
  const metaParts = [
    `r/${post.sub}`,
    formatCount(post.score, 'points'),
    formatCount(post.comments, 'comments'),
  ].filter(Boolean);
  if (metaParts.length === 1) metaParts.push(`top #${post.rank} today`);
  return [
    'Reddit English Daily',
    metaParts.join(' · '),
    '',
    post.title,
    '',
    'Reading: open the link, skim the post and top comments, then write down 3 useful words.',
    'Listening/Speaking: use text-to-speech on one comment, then shadow it out loud once.',
    '',
    post.url,
  ].join('\n');
}

async function main() {
  if (!acquireLock()) {
    log('skip: another reddit-daily run is still active');
    return;
  }

  const today = localDateKey();
  const state = readState();
  if (!FORCE && state.lastSentDate === today) {
    log(`skip: already sent for ${today}`);
    return;
  }

  const candidates = await fetchCandidates();
  if (candidates.length === 0) throw new Error('No Reddit candidates fetched.');
  const post = pickPost(candidates, state);
  const message = buildMessage(post);

  if (DRY_RUN) {
    console.log(message);
    return;
  }

  const result = await sendText(message);
  state.lastSentDate = today;
  state.sentPosts = [
    ...(state.sentPosts || []),
    { sentAt: new Date().toISOString(), date: today, sub: post.sub, title: post.title, url: post.url, score: post.score, comments: post.comments, rank: post.rank },
  ];
  writeState(state);
  log(`sent: r/${post.sub} score=${post.score ?? 'n/a'} rank=${post.rank} to=${result.toUserId}`);
}

main()
  .catch((err) => {
    log(`error: ${err.stack || err.message || err}`);
    process.exitCode = 1;
  })
  .finally(() => {
    releaseLock();
  });
