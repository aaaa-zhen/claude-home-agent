#!/usr/bin/env node

import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { AcpAgent } from "weixin-acp";
import { isLoggedIn, start } from "weixin-agent-sdk";
import { ConversationContext, redact } from "./conversation-context.mjs";
import { BackgroundReplies } from "./background-replies.mjs";
import { sendText } from "../weixin-send.mjs";
import {generateReply, recordReplyDelivery, createChatReporter} from "./chat-reply-lifecycle.mjs";
import { defaultTranscriptPath } from "./acp-turn-router.mjs";
import { createRetryingChat, describeError } from "./chat-turn-retry.mjs";
import { detectSecret } from "./secret-detect.mjs";
import { SecretVault, markForeground, clearForeground } from "./secret-vault.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const CHAT_SOCKET = path.join(ROOT, "runtime", "acp-chat.sock");
const MAX_REQUEST_BYTES = 200_000;

const separator = process.argv.indexOf("--");
const agentArgs = separator >= 0 ? process.argv.slice(separator + 1) : process.argv.slice(2);
const [agentCommand, ...agentCommandArgs] = agentArgs;

if (!agentCommand) {
  throw new Error("ACP agent command is required");
}
if (!isLoggedIn()) {
  throw new Error("WeChat login is required");
}

const backgroundReplies = new BackgroundReplies({
  directory: path.join(ROOT, "runtime", "background-replies"),
  send: sendText,
  record(item) {
    const ts = new Date().toLocaleString('sv-SE', {timeZone: 'Asia/Shanghai'});
    fs.appendFileSync(path.join(ROOT, 'memory/recent-context.md'), `[${ts}] [bgtask] 后台回复发送接口已接受：${redact(item.text).replace(/\s+/g, ' ')}\n`);
  },
});

const acpAgent = new AcpAgent({
  onBackgroundResponse: event => backgroundReplies.enqueue({...event, text: redact(event.text)}),
  command: agentCommand,
  args: agentCommandArgs,
  cwd: ROOT,
});

let sharedConversationId;
let chatTail = Promise.resolve();
let context;
try { context = new ConversationContext(); context.recover(); }
catch (error) { console.error(`[context] unavailable: ${error.message}`); }
function remember(fn) {
  try { return context && fn(context); }
  catch (error) { console.error(`[context] write failed: ${error.message}`); }
}

const reportReply = createChatReporter(path.join(ROOT, 'runtime'));
const replyContext = {complete: (...args) => remember(c => c.complete(...args))};

// Transient upstream failures (API 529, proxy blip, reset socket) get one delayed
// retry unless the attempt already ran a tool. A final failure surfaces to the
// user in plain language through the SDK error notice (2026-09-22).
const chatWithRetry = createRetryingChat({
  chat: request => acpAgent.chat(request),
  transcriptPath: request => defaultTranscriptPath(acpAgent.sessions?.get(request.conversationId),
    {configDir: process.env.CLAUDE_CONFIG_DIR, cwd: ROOT}),
  retries: Number(process.env.HOME_AGENT_TURN_RETRIES ?? 1),
  delayMs: Number(process.env.HOME_AGENT_TURN_RETRY_DELAY_MS ?? 30_000),
  log: message => console.log(`[acp] retry: ${message}`),
});

// Credentials typed by the user go to the Keychain before anything is journaled
// or prompted; the model only ever sees「[已存入密码箱：<name>]」(2026-09-22).
const vault = new SecretVault();

function enqueueChat(request, {journal = false} = {}) {
  // Journal the receipt before queueing. A later queued turn stays out of the
  // current prompt; activate() selects exactly the turn being processed.
  const secret = journal ? detectSecret(request.text) : null;
  if (secret) request = {...request, text: secret.redactedText};
  const turnId = journal ? remember(c => c.receive(request.text || '[媒体消息]', 'home-agent:shared')) : null;
  const run = async () => {
    if (turnId) remember(c => c.activate(turnId));
    // The vault's `use` only works while this marker is fresh: a foreground user turn.
    if (turnId) markForeground(turnId);
    try {
      if (secret) {
        try {
          const stored = await vault.add({name: secret.name, user: secret.user, password: secret.password}, {source: `wechat:${turnId || 'turn'}`});
          console.log(`[vault] ${stored.updated ? 'updated' : 'stored'} ${stored.name} from inbound message`);
        } catch (error) {
          console.error(`[vault] store failed: ${error.message}`);
          request = {...request, text: `${request.text}\n\n[系统：密码箱写入失败（${error.message}），这条密码没有保存；如实告诉用户没存上、请稍后重发，不要把密码写进任何文件。]`};
        }
      }
      const notify = turnId
        ? text => sendText(text, {notification: {id: `turn-retry:${turnId}`, source: 'chat-bridge', originTurnId: turnId}})
        : undefined;
      return await generateReply({chat: request => chatWithRetry(request, {notify}), request,
        context: replyContext, turnId, report: reportReply});
    } catch (error) {
      // Terminal marker for session-manager's is_acp_turn_busy(): a failed turn
      // (e.g. a refused /compact) must not read as "still running" until the
      // next prompt lands (2026-09-20).
      const detail = error?.cause ? `${describeError(error)} <- ${describeError(error.cause)}` : describeError(error);
      console.log(`[acp] error: ${detail.slice(0, 300)}`);
      throw error;
    } finally {
      if (turnId) clearForeground();
    }
  };
  const next = chatTail.then(run, run);
  chatTail = next.catch(() => {});
  return next;
}

const sharedAgent = {
  chat(request) {
    sharedConversationId ||= request.conversationId || "home-agent:shared";
    return enqueueChat({ ...request, conversationId: sharedConversationId }, {journal: true});
  },
  onDelivery(response, outcome) {
    recordReplyDelivery(replyContext, response, outcome, reportReply);
  },
  clearSession(conversationId) {
    acpAgent.clearSession(sharedConversationId || conversationId);
  },
  dispose() {
    acpAgent.dispose();
  },
};

function sendJson(client, payload) {
  client.end(`${JSON.stringify(payload)}\n`);
}

async function handleChatRequest(client, raw) {
  try {
    const request = JSON.parse(raw);
    if (!request || typeof request.text !== "string" || !request.text.trim()) {
      sendJson(client, { error: "invalid chat request" });
      return;
    }
    sharedConversationId ||= "home-agent:shared";
    const response = await enqueueChat({
      conversationId: sharedConversationId,
      text: request.text,
    });
    sendJson(client, { text: response.text || "" });
  } catch (error) {
    // ACP RequestError is a plain object ({code, message, data.details}), not an Error.
    const message = error?.message || String(error);
    const detail = String(error?.data?.details || message).slice(0, 300);
    console.error(`[chat-bridge] request failed: ${message}${detail !== message ? ` (${detail})` : ""}`);
    sendJson(client, { error: "assistant unavailable", detail });
  }
}

function acceptClient(client) {
  let body = "";
  let handled = false;
  client.setEncoding("utf8");
  client.on("data", (chunk) => {
    if (handled) return;
    body += chunk;
    if (Buffer.byteLength(body, "utf8") > MAX_REQUEST_BYTES) {
      handled = true;
      sendJson(client, { error: "chat request is too large" });
      return;
    }
    const newline = body.indexOf("\n");
    if (newline >= 0) {
      handled = true;
      void handleChatRequest(client, body.slice(0, newline));
    }
  });
  client.on("end", () => {
    if (!handled) {
      handled = true;
      void handleChatRequest(client, body);
    }
  });
  client.on("error", () => {});
}

if (fs.existsSync(CHAT_SOCKET)) {
  const existing = fs.lstatSync(CHAT_SOCKET);
  if (!existing.isSocket()) {
    throw new Error(`Refusing to replace non-socket path: ${CHAT_SOCKET}`);
  }
  fs.unlinkSync(CHAT_SOCKET);
}

const bridge = net.createServer(acceptClient);
await new Promise((resolve, reject) => {
  bridge.once("error", reject);
  bridge.listen(CHAT_SOCKET, () => {
    bridge.off("error", reject);
    fs.chmodSync(CHAT_SOCKET, 0o600);
    resolve();
  });
});

const abortController = new AbortController();
let stopping = false;

function stop() {
  if (stopping) return;
  stopping = true;
  abortController.abort();
  bridge.close();
  sharedAgent.dispose();
}

process.on("SIGINT", stop);
process.on("SIGTERM", stop);
process.on("exit", () => {
  if (fs.existsSync(CHAT_SOCKET) && fs.lstatSync(CHAT_SOCKET).isSocket()) {
    fs.unlinkSync(CHAT_SOCKET);
  }
});

console.log(`[chat-bridge] listening on ${CHAT_SOCKET}`);
await start(sharedAgent, { abortSignal: abortController.signal });
