#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { ConversationContext } from './scripts/conversation-context.mjs';
import {validateSendResponse} from './scripts/weixin-response-validation.mjs';
export {validateSendResponse} from './scripts/weixin-response-validation.mjs';

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
  const clientId = makeClientId();
  const body = JSON.stringify({
    msg: {
      from_user_id: '',
      to_user_id: options.toUserId || account.toUserId,
      client_id: clientId,
      message_type: 2,
      message_state: 2,
      item_list: [{ type: 1, text_item: { text: trimmed } }],
      ...(options.contextToken ? { context_token: options.contextToken } : {}),
    },
    base_info: { channel_version: CHANNEL_VERSION },
  });

  let response;
  try { response = await fetch(endpoint(account.baseUrl, 'ilink/bot/sendmessage'), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      AuthorizationType: 'ilink_bot_token',
      Authorization: `Bearer ${account.token}`,
      'Content-Length': String(Buffer.byteLength(body, 'utf8')),
      'X-WECHAT-UIN': randomWechatUin(),
    },
    body,
    signal: AbortSignal.timeout(30000),
  }); } catch (cause) {
    const error = new Error('sendmessage transport failed; delivery outcome unknown');
    error.deliveryUnknown = true;
    throw error;
  }
  let raw;
  try { raw = await response.text(); }
  catch { const error = new Error('sendmessage response interrupted; delivery outcome unknown'); error.deliveryUnknown = true; throw error; }
  validateSendResponse(response.status, raw);
  // The shared egress owns this receipt, including old scripts that only call
  // the CLI. Never let a failed local receipt make callers resend an accepted message.
  let context;
  try {
    context = new ConversationContext(process.env.HOME_AGENT_NOTIFICATION_DB || undefined);
    context.recordNotification({
      id: options.notification?.id || clientId,
      text: trimmed,
      conversation: (!options.toUserId || options.toUserId === account.toUserId) ? 'home-agent:shared' : options.toUserId,
      source: options.notification?.source || 'background',
      taskId: options.notification?.taskId || null,
      originTurnId: options.notification?.originTurnId || null,
    });
  } catch (error) { console.error('[notification-context] accepted send receipt failed: ' + error.message); }
  finally { context?.close(); }

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
  try {
    const result = await sendText(text);
    console.log(JSON.stringify(result));
  } catch (error) {
    console.error(JSON.stringify({ok: false, error: error.message, outcome: error.deliveryUnknown ? 'unknown' : 'rejected'}));
    process.exitCode = error.deliveryUnknown ? 3 : 1;
  }
}
