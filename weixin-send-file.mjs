#!/usr/bin/env node
// 给后台 worker 用的发文件脚本:复用 SDK 的 sendWeixinMediaFile(与交互路径同一套上传)。
// 用法:node weixin-send-file.mjs --file /abs/path [--text 说明文字]
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { sendWeixinMediaFile } from './node_modules/weixin-agent-sdk/dist/index.mjs';

const DEFAULT_BASE_URL = 'https://ilinkai.weixin.qq.com';
const CDN_BASE_URL = 'https://novac2c.cdn.weixin.qq.com/c2c';

function readJson(p) { return JSON.parse(fs.readFileSync(p, 'utf8')); }

function resolveStateDir() {
  return process.env.OPENCLAW_STATE_DIR?.trim()
    || process.env.CLAWDBOT_STATE_DIR?.trim()
    || path.join(os.homedir(), '.openclaw');
}

function resolveAccount() {
  const stateDir = resolveStateDir();
  const idx = path.join(stateDir, 'openclaw-weixin', 'accounts.json');
  const ids = readJson(idx).filter((id) => typeof id === 'string' && id.trim());
  const accountId = process.env.WEIXIN_ACCOUNT_ID?.trim() || ids[0];
  if (!accountId) throw new Error('No Weixin account. Run weixin-acp login first.');
  const account = readJson(path.join(stateDir, 'openclaw-weixin', 'accounts', `${accountId}.json`));
  const token = account.token?.trim();
  if (!token) throw new Error(`Account ${accountId} missing token.`);
  const toUserId = process.env.WEIXIN_PUSH_TO?.trim() || account.userId?.trim();
  if (!toUserId) throw new Error('No target user id (set WEIXIN_PUSH_TO or relogin).');
  return { token, baseUrl: account.baseUrl?.trim() || DEFAULT_BASE_URL, toUserId };
}

export async function sendFile(filePath, text = '', contextToken = '') {
  const abs = path.resolve(filePath);
  if (!fs.existsSync(abs)) throw new Error(`file not found: ${abs}`);
  const acc = resolveAccount();
  const opts = { baseUrl: acc.baseUrl, token: acc.token };
  // context_token 是「每条入站消息一发、不持久化」的值,后台/主动推送场景手上没有。
  // SDK 的 send*MessageWeixin 会在缺它时拒发,但底层 sendmessage 里 context_token 是可选字段
  // (与 weixin-send.mjs 纯文本推送一致,无 token 也能投递)。这里补一个占位值绕过 SDK 的客户端守卫。
  opts.contextToken = contextToken || 'bg-push';
  return sendWeixinMediaFile({
    filePath: abs,
    to: acc.toUserId,
    text: String(text || '').trim(),
    opts,
    cdnBaseUrl: CDN_BASE_URL,
  });
}

const isCli = process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (isCli) {
  const args = process.argv.slice(2);
  const fi = args.indexOf('--file');
  const ti = args.indexOf('--text');
  const ci = args.indexOf('--context-token');
  const file = fi >= 0 ? args[fi + 1] : '';
  const ctxTok = ci >= 0 ? args[ci + 1] : '';
  // --text 取到下一个 flag 为止
  let text = '';
  if (ti >= 0) {
    const rest = args.slice(ti + 1);
    const stop = rest.findIndex((a) => a.startsWith('--'));
    text = (stop === -1 ? rest : rest.slice(0, stop)).join(' ');
  }
  if (!file) { console.error('usage: weixin-send-file.mjs --file /abs/path [--text 说明] [--context-token T]'); process.exit(2); }
  try {
    const r = await sendFile(file, text, ctxTok);
    console.log(JSON.stringify({ ok: true, result: r ?? null }));
  } catch (e) {
    console.error(String(e?.message || e));
    process.exit(1);
  }
}
