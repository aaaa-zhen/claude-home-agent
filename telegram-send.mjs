#!/usr/bin/env node
/**
 * Send messages to Telegram channels/chats via Bot API.
 *
 * Usage:
 *   node telegram-send.mjs --channel <channel> --text "message"
 *   node telegram-send.mjs --chat-id <id> --text "message"
 *   node telegram-send.mjs --channel english --text "message"  # shorthand
 *
 * Channels (shorthands) — 频道名在环境变量里配置，见下方 CHANNEL_MAP：
 *   english   (英语阅读卡片)
 *   podcast   (播客推荐)
 *   home      (家居 & 安全)
 *   reminder  (出行 & 提醒)
 *   env       (环境监控)
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Load .env from telegram-agent dir
const dirs = [
  path.dirname(fileURLToPath(import.meta.url)),
  '/home/ubuntu/telegram-agent',
];
for (const dir of dirs) {
  const envFile = path.join(dir, '.env');
  if (fs.existsSync(envFile)) {
    for (const line of fs.readFileSync(envFile, 'utf8').split('\n')) {
      const m = line.match(/^([A-Z_]+)=(.*)$/);
      if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
    }
    break;
  }
}

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
if (!BOT_TOKEN) {
  console.error('TELEGRAM_BOT_TOKEN not set');
  process.exit(1);
}

// 频道从环境变量读，填你自己的 Telegram 频道用户名（@yourchannel）或 chat_id
const CHANNEL_MAP = {
  english:  process.env.TG_CHANNEL_ENGLISH  || '@your_english_channel',
  podcast:  process.env.TG_CHANNEL_PODCAST  || '@your_podcast_channel',
  home:     process.env.TG_CHANNEL_HOME     || '@your_home_channel',
  reminder: process.env.TG_CHANNEL_REMINDER || '@your_reminder_channel',
  env:      process.env.TG_CHANNEL_ENV      || '@your_env_channel',
  daily:    process.env.TG_DAILY_CHAT_ID    || '',
};

// Parse args
const args = process.argv.slice(2);
function getArg(name) {
  const idx = args.indexOf(name);
  if (idx === -1 || idx + 1 >= args.length) return null;
  return args[idx + 1];
}

let chatId = getArg('--chat-id');
const channelArg = getArg('--channel');
const text = getArg('--text');
const parseMode = getArg('--parse-mode') || null;
const threadId = getArg('--thread-id') || null;

if (channelArg) {
  chatId = CHANNEL_MAP[channelArg] || (channelArg.startsWith('@') ? channelArg : `@${channelArg}`);
}

if (!chatId || !text) {
  console.error('Usage: telegram-send.mjs --channel <name> --text "message"');
  console.error('Channels:', Object.keys(CHANNEL_MAP).join(', '));
  process.exit(1);
}

// Send
const body = { chat_id: chatId, text };
if (parseMode) body.parse_mode = parseMode;
if (threadId) body.message_thread_id = parseInt(threadId);

try {
  const resp = await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (data.ok) {
    console.log(`Sent to ${chatId}`);
  } else {
    // Retry without parse_mode if Markdown failed
    if (parseMode && data.description?.includes('parse')) {
      delete body.parse_mode;
      const retry = await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const r2 = await retry.json();
      if (r2.ok) {
        console.log(`Sent to ${chatId} (plain text fallback)`);
      } else {
        console.error(`Failed: ${r2.description}`);
        process.exit(1);
      }
    } else {
      console.error(`Failed: ${data.description}`);
      process.exit(1);
    }
  }
} catch (err) {
  console.error(`Error: ${err.message}`);
  process.exit(1);
}
