// Scrub credentials from the two places where the Weixin SDK writes the raw
// inbound message on its own, outside the bridge (2026-09-22):
//   1. appendWeixinTurnMemory -> memory/recent-context.md "[wechat-direct]" lines
//   2. the "[weixin-msg] start … text=…" line in /tmp/openclaw/openclaw-<date>.log
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const marker = 'home-agent secret redaction v1';
function replaceOnce(source, before, after) {
  if (!source.includes(before) || source.indexOf(before) !== source.lastIndexOf(before)) throw new Error('Unsupported Weixin SDK layout: ' + before.slice(0, 90));
  return source.replace(before, after);
}

export function patchSecretRedaction(source, root) {
  if (source.includes(marker)) return source;
  source = `// ${marker}\nimport {redact as redactSecrets} from ${JSON.stringify(path.join(root, 'scripts/conversation-context.mjs'))};\n` + source;
  source = replaceOnce(source, 'const user = safeTurnMemoryText(userText, 220) || "(媒体消息)";', 'const user = safeTurnMemoryText(redactSecrets(userText), 220) || "(媒体消息)";');
  source = replaceOnce(source, 'text=${JSON.stringify(textBody.slice(0, 120))}`', 'text=${JSON.stringify(redactSecrets(textBody).slice(0, 120))}`');
  return source;
}

export function apply(root) {
  const target = path.join(root, 'node_modules/weixin-agent-sdk/dist/index.mjs');
  const before = fs.readFileSync(target, 'utf8');
  const after = patchSecretRedaction(before, root);
  if (after !== before) fs.writeFileSync(target, after);
  console.log('[patch] secret redaction of inbound text in turn memory and message log ' + (after === before ? 'already applied' : 'applied'));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  apply(process.env.HOME_AGENT_RUNTIME_ROOT || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'));
}
