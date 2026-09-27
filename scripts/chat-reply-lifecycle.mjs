import fs from 'node:fs';
import path from 'node:path';

export function assertReplyContent(response) {
  const text = typeof response?.text === 'string' && response.text.trim();
  const media = response?.media;
  if (text || (['image', 'video', 'audio', 'file'].includes(media?.type) && typeof media.url === 'string' && media.url.trim())) return response;
  const error = new Error('刚才这条没有生成有效回复，未确认完成；请再发一句，我会先核对执行结果再继续。');
  error.code = 'EMPTY_RESPONSE';
  throw error;
}

export function replySummary(response) {
  return response?.text?.trim() || (response?.media ? '[媒体回复]' : '');
}

export async function generateReply({chat, request, context, turnId, report = () => {}}) {
  try {
    const response = await chat(request);
    // Local commands can legitimately have no conversational output.
    if (!/^\/(compact|context|heapdump|extra-usage)(?:\s|$)/.test(request.text?.trim() || '')) assertReplyContent(response);
    if (turnId) context?.complete(turnId, replySummary(response), 'generated');
    report({kind: 'generated', turnId, textChars: response?.text?.trim().length || 0, media: Boolean(response?.media)});
    return turnId ? {...response, homeAgentTurnId: turnId} : response;
  } catch (error) {
    const status = error?.code === 'EMPTY_RESPONSE' ? 'empty_response' : 'interrupted';
    if (turnId) context?.complete(turnId, '', status);
    report({kind: status, turnId, errorCode: error?.code || 'MODEL_ERROR'});
    throw error;
  }
}

export function recordReplyDelivery(context, response, outcome, report = () => {}) {
  const turnId = response?.homeAgentTurnId;
  if (!turnId) return;
  if (!['accepted', 'rejected', 'delivery_unknown'].includes(outcome)) throw new Error('Invalid delivery outcome');
  context?.complete(turnId, replySummary(response), outcome);
  report({kind: outcome, turnId, textChars: response.text?.trim().length || 0, media: Boolean(response.media)});
}

export function createChatReporter(directory) {
  const filename = path.join(directory, 'chat-reply-events.jsonl');
  return event => {
    try {
      fs.mkdirSync(directory, {recursive: true});
      // Keep diagnostics useful without creating another unbounded log.
      if (fs.existsSync(filename) && fs.statSync(filename).size > 2 * 1024 * 1024) fs.renameSync(filename, filename + '.previous');
      fs.appendFileSync(filename, JSON.stringify({at: new Date().toISOString(), ...event}) + '\n', {mode: 0o600});
    } catch (error) { console.error('[chat-reply] diagnostic write failed: ' + error.message); }
  };
}
