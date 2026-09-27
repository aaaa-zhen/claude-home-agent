import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const marker = 'home-agent reply lifecycle v1';
function replaceOnce(source, before, after) {
  if (!source.includes(before) || source.indexOf(before) !== source.lastIndexOf(before)) throw new Error('Unsupported Weixin SDK layout: ' + before.slice(0, 90));
  return source.replace(before, after);
}

export function patchChatReliability(source, root) {
  if (source.includes(marker)) return source;
  source = `// ${marker}\nimport {validateSendResponse} from ${JSON.stringify(path.join(root, 'scripts/weixin-response-validation.mjs'))};\nimport {assertReplyContent} from ${JSON.stringify(path.join(root, 'scripts/chat-reply-lifecycle.mjs'))};\n` + source;
  source = replaceOnce(source, 'async function sendMessage(params) {\n\tawait apiFetch({', 'async function sendMessage(params) {\n\tlet rawText;\n\ttry { rawText = await apiFetch({');
  source = replaceOnce(source, '\t\tlabel: "sendMessage"\n\t});\n}', `\t\tlabel: "sendMessage"
\t}); } catch (error) {
        // HTTP 4xx is an explicit refusal; a disconnected/5xx send may have arrived.
        if (error.deliveryUnknown === undefined) error.deliveryUnknown = !/sendMessage 4\\d\\d:/.test(error.message || '');
        throw error;
    }
    return validateSendResponse(200, rawText);
}`);
  // Keep the deadline active until the complete body has been read.
  const apiStart = source.indexOf('async function apiFetch(params) {');
  const apiEnd = source.indexOf('async function getUpdates(params)', apiStart);
  if (apiStart < 0 || apiEnd < 0) throw new Error('Unsupported apiFetch layout');
  const api = source.slice(apiStart, apiEnd);
  const newApi = replaceOnce(api, '\t\tclearTimeout(t);\n\t\tconst rawText = await res.text();', '\t\tconst rawText = await res.text();\n\t\tclearTimeout(t);');
  source = source.slice(0, apiStart) + newApi + source.slice(apiEnd);
  source = replaceOnce(source, '\tconst clientId = generateClientId();\n\tconst req = buildSendMessageReq({', '\tif (!String(text || "").trim()) throw new Error("Refusing empty Weixin text");\n\tconst clientId = generateClientId();\n\tconst req = buildSendMessageReq({');
  source = replaceOnce(source, '\ttry {\n\t\tconst response = await (deps.runAgentChat ? deps.runAgentChat(request) : deps.agent.chat(request));', `    let homeAgentResponse;
    let homeAgentDeliveryAccepted = false;
    const recordDelivery = async (outcome, error) => {
        if (!homeAgentResponse || homeAgentDeliveryAccepted) return;
        if (outcome === 'accepted') homeAgentDeliveryAccepted = true;
        try { await deps.agent.onDelivery?.(homeAgentResponse, outcome, error); }
        catch (receiptError) { logger.error('[chat-reply] receipt failed: ' + receiptError.message); }
    };
\ttry {
\t\tconst response = await (deps.runAgentChat ? deps.runAgentChat(request) : deps.agent.chat(request));
        homeAgentResponse = response;
        assertReplyContent(response);`);
  source = replaceOnce(source, '\t\t\t\tlogger.warn(`[weixin-response-media-guard] missing media path=', '\t\t\t\thomeAgentResponse = {...response, text: fallbackText, media: undefined};\n\t\t\t\tlogger.warn(`[weixin-response-media-guard] missing media path=');
  source = replaceOnce(source, '\t\tawait appendWeixinTurnMemory(\n\t\t\ttextBody', '\t\tawait recordDelivery("accepted");\n\t\tawait appendWeixinTurnMemory(\n\t\t\ttextBody');
  source = replaceOnce(source, '\t\t\tresponse.text ?? (response.media ? "[媒体回复]" : ""),\n\t\t\trequestId', '\t\t\thomeAgentResponse.text ?? (homeAgentResponse.media ? "[媒体回复]" : ""),\n\t\t\trequestId');
  source = replaceOnce(source, '\t\tlogger.error(`[weixin-msg] error requestId=${requestId}', '\t\tif (homeAgentDeliveryAccepted) { logger.error("[chat-reply] post-send bookkeeping failed: " + String(err)); return; }\n\t\tawait recordDelivery(err?.deliveryUnknown ? "delivery_unknown" : "rejected", err);\n\t\tlogger.error(`[weixin-msg] error requestId=${requestId}');
  return source;
}

export function apply(root) {
  const target = path.join(root, 'node_modules/weixin-agent-sdk/dist/index.mjs');
  const before = fs.readFileSync(target, 'utf8');
  const after = patchChatReliability(before, root);
  if (after !== before) fs.writeFileSync(target, after);
  console.log('[patch] chat reply lifecycle and send receipts ' + (after === before ? 'already applied' : 'applied'));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  apply(process.env.HOME_AGENT_RUNTIME_ROOT || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'));
}
