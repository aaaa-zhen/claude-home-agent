#!/usr/bin/env node
import { connectBrowser } from './browser-bridge-lib.mjs';

const lessonsUrl = 'https://preply.com/en/lessons';
const messagesUrl = 'https://preply.com/en/messages';

function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i];
    if (!item.startsWith('--')) {
      args._.push(item);
      continue;
    }
    const key = item.slice(2);
    if (key === 'json' || key === 'dry-run' || key === 'apply') {
      args[key] = true;
      continue;
    }
    args[key] = argv[i + 1] || '';
    i += 1;
  }
  return args;
}

function normalizeName(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

function aliasTutorQuery(value) {
  const normalized = normalizeName(value);
  if (/\b(sosophia|sophia)\b/.test(normalized)) return 'sophie g';
  return normalized;
}

function tutorScore(query, name, rawText) {
  const q = aliasTutorQuery(query);
  const n = normalizeName(name);
  const raw = normalizeName(rawText);
  if (!q) return 0;
  if (n === q) return 100;
  if (n.startsWith(q) || q.startsWith(n)) return 90;
  if (raw.includes(q)) return 70;
  const qParts = q.split(/\s+/).filter(Boolean);
  const matches = qParts.filter((part) => n.includes(part) || raw.includes(part)).length;
  return matches ? matches / qParts.length * 60 : 0;
}

function extractTutorName(text) {
  const lines = String(text || '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
  return lines[0] || '';
}

async function ensureBrowser(url = lessonsUrl) {
  return connectBrowser(url);
}

async function messagesPage(context) {
  const page = await context.newPage();
  await page.setViewportSize({ width: 1440, height: 1000 }).catch(() => {});
  await page.goto(messagesUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(5000);
  return page;
}

async function listTutors(page) {
  return page.evaluate(() => {
    const seen = new Set();
    return Array.from(document.querySelectorAll('a[href*="/en/messages/"]'))
      .map((link) => {
        const href = link.href;
        const rawText = (link.innerText || link.textContent || '').trim();
        const name = rawText.split('\n').map((line) => line.trim()).filter(Boolean)[0] || '';
        return { name, rawText, href };
      })
      .filter((item) => {
        if (!item.href || !item.name || seen.has(item.href)) return false;
        seen.add(item.href);
        return true;
      });
  });
}

function chooseTutor(tutors, query) {
  const ranked = tutors
    .map((tutor) => ({ ...tutor, score: tutorScore(query, tutor.name, tutor.rawText) }))
    .sort((a, b) => b.score - a.score);
  const best = ranked[0];
  if (!best || best.score < 40) {
    return { tutor: null, ranked: ranked.slice(0, 5) };
  }
  return { tutor: best, ranked: ranked.slice(0, 5) };
}

async function detectLogin(page) {
  const url = page.url();
  const title = await page.title().catch(() => '');
  const text = await page.locator('body').innerText({ timeout: 10000 }).catch(() => '');
  const lower = `${title}\n${url}\n${text}`.toLowerCase();
  return lower.includes('/login') || lower.includes('log in to your preply account') || lower.includes('continue with google');
}

function todayPatterns(now = new Date()) {
  const weekday = new Intl.DateTimeFormat('en-US', { weekday: 'long', timeZone: 'Asia/Shanghai' }).format(now);
  const weekdayShort = new Intl.DateTimeFormat('en-US', { weekday: 'short', timeZone: 'Asia/Shanghai' }).format(now);
  const monthLong = new Intl.DateTimeFormat('en-US', { month: 'long', timeZone: 'Asia/Shanghai' }).format(now);
  const monthShort = new Intl.DateTimeFormat('en-US', { month: 'short', timeZone: 'Asia/Shanghai' }).format(now);
  const day = new Intl.DateTimeFormat('en-US', { day: 'numeric', timeZone: 'Asia/Shanghai' }).format(now);
  const zh = new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    timeZone: 'Asia/Shanghai',
  }).format(now);

  return [
    'today',
    weekday.toLowerCase(),
    weekdayShort.toLowerCase(),
    `${monthLong} ${day}`.toLowerCase(),
    `${monthShort} ${day}`.toLowerCase(),
    zh,
    zh.replace('/', '月') + '日',
  ];
}

function relevantLines(text) {
  const lines = text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
  const needles = [
    ...todayPatterns(),
    'lesson',
    'class',
    'tutor',
    'teacher',
    'minutes',
    'min',
    'preply classroom',
    'join',
    '课程',
    '课',
    '老师',
    '分钟',
  ];

  const picked = [];
  for (const line of lines) {
    const lower = line.toLowerCase();
    if (needles.some((needle) => lower.includes(needle))) picked.push(line);
    if (picked.length >= 80) break;
  }
  return picked;
}

async function commandStatus() {
  const { browser, context } = await ensureBrowser(lessonsUrl);
  const page = await context.newPage();
  await page.setViewportSize({ width: 1440, height: 1000 }).catch(() => {});
  await page.goto(lessonsUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(6000);

  const url = page.url();
  const title = await page.title();
  const text = await page.locator('body').innerText({ timeout: 10000 }).catch(() => '');

  if (await detectLogin(page)) {
    console.log(JSON.stringify({
      status: 'need_login',
      url,
      title,
      message: 'Open Agent Browser and log in to Preply once.',
    }, null, 2));
    await page.close().catch(() => {});
    await browser.close();
    return;
  }

  console.log(JSON.stringify({
    status: 'ok',
    url,
    title,
    lines: relevantLines(text),
  }, null, 2));
  await page.close().catch(() => {});
  await browser.close();
}

async function commandListTutors() {
  const { browser, context } = await ensureBrowser(messagesUrl);
  const page = await messagesPage(context);
  if (await detectLogin(page)) {
    console.log(JSON.stringify({ status: 'need_login', url: page.url(), title: await page.title() }, null, 2));
    await page.close().catch(() => {});
    await browser.close();
    return;
  }
  const tutors = await listTutors(page);
  console.log(JSON.stringify({ status: 'ok', tutors }, null, 2));
  await page.close().catch(() => {});
  await browser.close();
}

async function commandSendMessage(args) {
  const tutorQuery = args.tutor || args.to || '';
  const message = args.message || '';
  if (!tutorQuery || !message) {
    throw new Error('send-message requires --tutor and --message');
  }

  const { browser, context } = await ensureBrowser(messagesUrl);
  const page = await messagesPage(context);
  if (await detectLogin(page)) {
    console.log(JSON.stringify({ status: 'need_login', url: page.url(), title: await page.title() }, null, 2));
    await page.close().catch(() => {});
    await browser.close();
    return;
  }

  const tutors = await listTutors(page);
  const { tutor, ranked } = chooseTutor(tutors, tutorQuery);
  if (!tutor) {
    console.log(JSON.stringify({
      status: 'not_found',
      tutorQuery,
      candidates: ranked.map(({ name, href, score }) => ({ name, href, score })),
    }, null, 2));
    await page.close().catch(() => {});
    await browser.close();
    return;
  }

  await page.goto(tutor.href, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(5000);
  const textarea = page.locator('textarea[data-qa-id="message_field"], textarea[placeholder="Your message"]').first();
  await textarea.waitFor({ state: 'visible', timeout: 20000 });
  await textarea.fill(message);
  await page.waitForTimeout(500);

  if (!args.apply || args['dry-run']) {
    await textarea.fill('');
    console.log(JSON.stringify({
      status: 'preview',
      matchedTutor: extractTutorName(tutor.rawText) || tutor.name,
      tutorHref: tutor.href,
      message,
      ready: true,
      requires_apply: true,
    }, null, 2));
    await page.close().catch(() => {});
    await browser.close();
    return;
  }

  const sendButton = page.locator('button[data-qa-id="send_message"], button[aria-label="Send"]').first();
  await sendButton.waitFor({ state: 'visible', timeout: 10000 });
  await sendButton.click();
  await page.waitForTimeout(2500);
  const bodyText = await page.locator('body').innerText({ timeout: 10000 }).catch(() => '');
  const confirmed = bodyText.includes(message);
  console.log(JSON.stringify({
    status: confirmed ? 'sent' : 'sent_unverified',
    matchedTutor: extractTutorName(tutor.rawText) || tutor.name,
    tutorHref: tutor.href,
    message,
    confirmed,
  }, null, 2));
  await page.close().catch(() => {});
  await browser.close();
}

const args = parseArgs(process.argv.slice(2));
const command = args._[0] || 'status';

if (command === 'status') {
  await commandStatus();
} else if (command === 'list-tutors') {
  await commandListTutors();
} else if (command === 'send-message') {
  await commandSendMessage(args);
} else {
  throw new Error(`unknown command: ${command}`);
}
