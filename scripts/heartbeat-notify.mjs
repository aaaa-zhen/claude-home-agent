import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { sendText } from '../weixin-send.mjs';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dir = path.join(root, 'runtime/heartbeat-notifications');
fs.mkdirSync(dir, {recursive: true, mode: 0o700});
function save(file, item) {
  const temp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temp, JSON.stringify(item), {mode: 0o600}); fs.renameSync(temp, file);
}
async function deliver(file, item) {
  if (item.status === 'accepted' || item.status === 'unknown' || item.attempts >= 3) return;
  if (Date.now() - item.created > 4 * 3600_000) { item.status = 'expired'; save(file, item); return; }
  item.attempts++; item.status = 'unknown'; save(file, item); // crash while sending is ambiguous
  try {
    await sendText(item.text);
  } catch (error) {
    item.status = error.deliveryUnknown ? 'unknown' : 'rejected';
    item.error = error.message; save(file, item);
    console.error(`[heartbeat-notify] ${item.status}; preserved for ${item.status === 'rejected' ? 'bounded retry' : 'inspection'}`);
    return;
  }
  item.status = 'accepted'; item.accepted_at = new Date().toISOString(); save(file, item);
  // Only accepted sends can enter conversational memory. This still does not
  // assert the user has read the message.
  const ts = new Date().toLocaleString('sv-SE', {timeZone: 'Asia/Shanghai'});
  fs.appendFileSync(path.join(root, 'tmp/heartbeat.log'), `[${ts}] SPOKE: ${item.text.slice(0, 120)}\n`);
  fs.appendFileSync(path.join(root, 'memory/recent-context.md'), `[${ts}] [heartbeat] 主动提醒了用户: ${item.text.replace(/\s+/g, ' ')}\n`);
  const last = path.join(root, 'runtime/prompt-inject/last-proactive.json');
  fs.mkdirSync(path.dirname(last), {recursive: true});
  save(last, {ts: item.accepted_at, source: 'heartbeat', text: item.text});
}
if (process.argv.includes('--retry')) {
  // Each heartbeat re-derives the reminder from current state, so when several
  // rejected items pile up (agent down for hours) only the newest still holds;
  // the older ones are superseded, not resent (2026-09-20: two near-identical
  // class reminders were flushed together after a two-day outage).
  const rejected = fs.readdirSync(dir).filter(n => n.endsWith('.json'))
    .map(name => { const file = path.join(dir, name); return {file, item: JSON.parse(fs.readFileSync(file, 'utf8'))}; })
    .filter(({item}) => item.status === 'rejected')
    .sort((a, b) => (b.item.created || 0) - (a.item.created || 0));
  for (const [index, {file, item}] of rejected.entries()) {
    if (index === 0) await deliver(file, item);
    else { item.status = 'superseded'; item.superseded_at = new Date().toISOString(); save(file, item); }
  }
} else {
  let text = ''; for await (const chunk of process.stdin) text += chunk;
  text = text.trim();
  if (text) {
    const day = new Date().toLocaleDateString('sv-SE', {timeZone: 'Asia/Shanghai'});
    const id = crypto.createHash('sha256').update(day + text).digest('hex').slice(0, 24);
    const file = path.join(dir, id + '.json');
    const item = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : {text, created: Date.now(), attempts: 0, status: 'pending'};
    await deliver(file, item);
  }
}
