#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const DEFAULT_BASE_URL = 'https://ilinkai.weixin.qq.com';
const CHANNEL_VERSION = '0.1.0';

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function resolveStateDir() {
  return process.env.OPENCLAW_STATE_DIR?.trim()
    || process.env.CLAWDBOT_STATE_DIR?.trim()
    || path.join(os.homedir(), '.openclaw');
}

function resolveAccount() {
  const stateDir = resolveStateDir();
  const accountIndexPath = path.join(stateDir, 'openclaw-weixin', 'accounts.json');
  const accountIds = readJson(accountIndexPath).filter((id) => typeof id === 'string' && id.trim());
  const accountId = process.env.WEIXIN_ACCOUNT_ID?.trim() || accountIds[0];
  if (!accountId) throw new Error('No Weixin account found. Run weixin-acp login first.');

  const accountPath = path.join(stateDir, 'openclaw-weixin', 'accounts', `${accountId}.json`);
  const account = readJson(accountPath);
  const token = account.token?.trim();
  if (!token) throw new Error(`Account ${accountId} is missing token.`);

  const toUserId = process.env.WEIXIN_PUSH_TO?.trim() || account.userId?.trim();
  if (!toUserId) throw new Error('No target user id. Set WEIXIN_PUSH_TO or relogin so account.userId is saved.');

  return {
    accountId,
    token,
    baseUrl: account.baseUrl?.trim() || DEFAULT_BASE_URL,
    toUserId,
  };
}

function randomWechatUin() {
  return Buffer.from(String(crypto.randomBytes(4).readUInt32BE(0)), 'utf8').toString('base64');
}

function makeClientId() {
  return `openclaw-weixin:${Date.now()}-${crypto.randomBytes(4).toString('hex')}`;
}

function endpoint(baseUrl, pathname) {
  const base = baseUrl.endsWith('/') ? baseUrl : `${baseUrl}/`;
  return new URL(pathname, base).toString();
}

export async function sendText(text, options = {}) {
  const trimmed = String(text ?? '').trim();
  if (!trimmed) throw new Error('Refusing to send an empty Weixin message.');

  const account = resolveAccount();
  const body = JSON.stringify({
    msg: {
      from_user_id: '',
      to_user_id: options.toUserId || account.toUserId,
      client_id: makeClientId(),
      message_type: 2,
      message_state: 2,
      item_list: [{ type: 1, text_item: { text: trimmed } }],
      ...(options.contextToken ? { context_token: options.contextToken } : {}),
    },
    base_info: { channel_version: CHANNEL_VERSION },
  });

  const response = await fetch(endpoint(account.baseUrl, 'ilink/bot/sendmessage'), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      AuthorizationType: 'ilink_bot_token',
      Authorization: `Bearer ${account.token}`,
      'Content-Length': String(Buffer.byteLength(body, 'utf8')),
      'X-WECHAT-UIN': randomWechatUin(),
    },
    body,
  });

  const raw = await response.text();
  if (!response.ok) {
    throw new Error(`sendmessage failed: HTTP ${response.status} ${raw.slice(0, 500)}`);
  }

  return { ok: true, status: response.status, accountId: account.accountId, toUserId: options.toUserId || account.toUserId };
}

async function readStdin() {
  let data = '';
  for await (const chunk of process.stdin) data += chunk;
  return data;
}

const isCli = process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (isCli) {
  const args = process.argv.slice(2);
  const textFlag = args.indexOf('--text');
  let text = '';
  if (textFlag >= 0) text = args.slice(textFlag + 1).join(' ');
  else text = args.join(' ');
  if (!text.trim() && !process.stdin.isTTY) text = await readStdin();
  const result = await sendText(text);
  console.log(JSON.stringify(result));
}
