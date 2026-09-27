import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {DatabaseSync} from 'node:sqlite';

export function checkChatReply(turn, now = Date.now()) {
  if (!turn) return {ok: true, message: 'chat reply not yet exercised'};
  const age = Math.max(0, Math.floor((now - Date.parse(turn.received_at)) / 1000));
  if (['empty_response', 'interrupted', 'rejected', 'delivery_unknown', 'timed_out'].includes(turn.status)) {
    return {ok: false, message: `chat reply ${turn.status}; last request ${age}s ago`};
  }
  if (['received', 'processing', 'generated'].includes(turn.status) && age > 300) {
    return {ok: false, message: `chat reply pending ${age}s (${turn.status})`};
  }
  return {ok: true, message: `chat reply ${turn.status}; last request ${age}s ago`};
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const filename = process.env.HOME_AGENT_CHAT_HEALTH_DB || path.join(root, 'runtime/conversation-context.db');
  try {
    if (!fs.existsSync(filename)) throw new Error('conversation database missing');
    const db = new DatabaseSync(filename, {readOnly: true});
    try {
      const turn = db.prepare("SELECT received_at,status FROM turns WHERE conversation='home-agent:shared' AND source='wechat' ORDER BY seq DESC LIMIT 1").get();
      const result = checkChatReply(turn);
      console.log(result.message);
      process.exitCode = result.ok ? 0 : 1;
    } finally { db.close(); }
  } catch (error) { console.log('chat reply check failed: ' + error.message); process.exitCode = 1; }
}
