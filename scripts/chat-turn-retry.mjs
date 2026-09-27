// Generic turn-failure policy for the WeChat bridge (2026-09-22).
//
// Before this, one transient upstream error (API 529, proxy blip, reset socket)
// ended the user's turn with nothing done and nothing said. Now a transient
// error gets one delayed retry, and the final failure is reported in plain
// language. Retries never re-run a turn that already executed tools, because
// device actions must not happen twice.
import fs from 'node:fs';

export const TRANSIENT_ERROR = /\b(529|500|502|503|504)\b|overloaded|rate.?limit|too many requests|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EAI_AGAIN|EPIPE|fetch failed|unable to connect|socket hang up|network error|connection (?:reset|refused|closed)|timed? ?out/i;

export function describeError(error) {
  return String(error?.data?.details || error?.message || error).replace(/\s+/g, ' ').trim();
}

export function isTransientError(error) {
  // An empty model answer is a content problem, not a transport one: never replay it.
  if (!error || error.code === 'EMPTY_RESPONSE') return false;
  return TRANSIENT_ERROR.test(describeError(error));
}

export function shortReason(error) {
  const text = describeError(error);
  if (/529|overloaded/i.test(text)) return '模型服务端拥堵（529）';
  if (/rate.?limit|too many requests/i.test(text)) return '模型接口限流';
  if (/ECONNREFUSED|unable to connect|fetch failed|EAI_AGAIN|network/i.test(text)) return '网络或代理连不上';
  if (/timed? ?out|ETIMEDOUT/i.test(text)) return '请求超时';
  return text.slice(0, 80);
}

export function transcriptSize(file) {
  if (!file) return 0;
  try { return fs.statSync(file).size; } catch { return 0; }
}

// True when the session transcript appended since `offset` shows any tool
// call or tool result. Only whole lines are parsed; a partial trailing line
// (Claude still writing) is ignored.
export function transcriptToolActivity(file, offset = 0) {
  if (!file) return false;
  let text;
  try {
    const fd = fs.openSync(file, 'r');
    try {
      const size = fs.fstatSync(fd).size;
      if (size <= offset) return false;
      const buffer = Buffer.alloc(size - offset);
      fs.readSync(fd, buffer, 0, buffer.length, offset);
      text = buffer.toString('utf8');
    } finally { fs.closeSync(fd); }
  } catch { return false; }
  for (const line of text.split('\n')) {
    if (!line.trim()) continue;
    let entry;
    try { entry = JSON.parse(line); } catch { continue; }
    const content = entry?.message?.content;
    if (Array.isArray(content) && content.some(block => block?.type === 'tool_use' || block?.type === 'tool_result')) return true;
  }
  return false;
}

function preview(text) {
  return String(text ?? '').replace(/\s+/g, ' ').trim().slice(0, 20);
}

function finalError(cause, message) {
  const error = new Error(message);
  error.code = cause?.code ?? 'MODEL_ERROR';
  error.cause = cause;
  error.transientTurnFailure = true;
  return error;
}

export function createRetryingChat({
  chat,
  transcriptPath = () => null,
  retries = 1,
  delayMs = 30_000,
  sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
  log = () => {},
}) {
  return async function chatWithRetry(request, {notify} = {}) {
    const text = String(request?.text ?? '');
    // Local commands (/compact, /context…) have their own supervisors; leave them alone.
    const local = /^\s*\//.test(text);
    for (let attempt = 0; ; attempt++) {
      const fileBefore = transcriptPath(request);
      const offset = transcriptSize(fileBefore);
      try {
        return await chat(request);
      } catch (error) {
        if (local || !isTransientError(error)) throw error;
        const reason = describeError(error);
        // The session may have been created during this attempt; re-resolve the file.
        const fileAfter = transcriptPath(request);
        if (transcriptToolActivity(fileAfter, fileAfter === fileBefore ? offset : 0)) {
          log(`not retrying after tool activity: ${reason}`);
          throw finalError(error, `「${preview(text)}」执行到一半被打断（${shortReason(error)}）。为了不把操作做两遍没有自动重试，请先确认状态再决定要不要重发。`);
        }
        if (attempt >= retries) {
          log(`giving up after ${attempt} retr${attempt === 1 ? 'y' : 'ies'}: ${reason}`);
          throw finalError(error, `「${preview(text)}」自动重试 ${attempt} 次后还是失败（${shortReason(error)}），这条没有处理，请稍后重发。`);
        }
        log(`retry ${attempt + 1}/${retries} in ${delayMs}ms after transient error: ${reason}`);
        if (notify) {
          try { await notify(`刚才那条「${preview(text)}」碰上${shortReason(error)}，${Math.round(delayMs / 1000)} 秒后自动再试一次。`); }
          catch (noticeError) { log(`retry notice failed: ${describeError(noticeError)}`); }
        }
        await sleep(delayMs);
      }
    }
  };
}
