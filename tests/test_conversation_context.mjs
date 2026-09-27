import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { ConversationContext, clip, isConversationLine } from '../scripts/conversation-context.mjs';

function context(t) { const c = new ConversationContext(':memory:'); t.after(() => c.close()); return c; }
function turn(c, id, user, answer = '回复') { c.receive(user, 'shared', id); c.activate(id); c.complete(id, answer); }
function card(id, source, fields = {}) {
  return {id, expected_revision: 0, title: '电视投屏', goal: '播放第四集', status: 'active', source_ids: [source], ...fields};
}

test('receipt survives restart; unfinished side effects are not replayed', t => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'context-test-'));
  t.after(() => fs.rmSync(tmp, {recursive: true, force: true}));
  const file = path.join(tmp, 'context.db');
  let c = new ConversationContext(file);
  c.receive('打开书房空调', 'shared', 'air'); c.activate('air'); c.close();
  c = new ConversationContext(file); c.recover();
  assert.equal(c.get('air').status, 'interrupted');
  assert.equal(c.packet('继续').interrupted_requests[0].id, 'air'); c.close();
});

test('queued future messages cannot leak into the active prompt or state', t => {
  const c = context(t);
  turn(c, 'old', '播放电视剧');
  c.receive('电视有声音吗', 'shared', 'now');
  c.receive('买股票', 'shared', 'future');
  c.activate('now');
  const p = c.packet('电视有声音吗');
  assert.equal(p.current_turn_id, 'now');
  assert.ok(!JSON.stringify(p).includes('买股票'));
  assert.throws(() => c.updateTopic(card('stocks', 'future')), /later queued/);
});

test('repeat text is a new turn; repeated message id is idempotent', t => {
  const c = context(t);
  turn(c, '1', '你再试试'); turn(c, '2', '你再试试');
  c.receive('你再试试', 'shared', '2');
  assert.equal(c.recent().length, 2);
  assert.throws(() => c.receive('换个任务', 'shared', '2'), /conflict/);
});

test('cron/heartbeat cannot become user topic during legacy import', t => {
  const c = context(t);
  const raw = '[2026-09-13 17:00:00] [wechat-direct] 播放第四集 → 已发请求\n' +
    '[2026-09-13 17:01:00] [heartbeat] 新闻谈油价\n' +
    '[2026-09-13 17:02:00] [cron:morning-english-news] 播英文新闻';
  assert.equal(c.importLegacy(raw), 1);
  assert.equal(c.importLegacy(raw), 0);
  assert.ok(!JSON.stringify(c.packet('这个')).includes('油价'));
  assert.equal(isConversationLine(raw.split('\n')[2]), false);
});

test('task survives an unrelated device turn and keeps latest user correction', t => {
  const c = context(t);
  turn(c, 'tv1', '播放第四集', '已成功播放');
  c.updateTopic(card('tv', 'tv1'));
  turn(c, 'tv2', '还是不行', '需要再检查');
  c.updateTopic(card('tv', 'tv2', {expected_revision: 1, status: 'waiting',
    facts: [{text: '用户确认未成功', kind: 'user', source_id: 'tv2', quote: '还是不行'}]}));
  turn(c, 'air', '打开书房空调', '已开到26度');
  const p = c.packet('24度吧');
  assert.equal(p.recent_dialogue.at(-1).id, 'air');
  assert.equal(p.working_topics[0].status, 'waiting');
  assert.equal(p.working_topics[0].facts[0].kind, 'user');
});

test('cancellation is durable and cannot be overwritten by stale evidence', t => {
  const c = context(t);
  turn(c, '1', '播放第四集'); c.updateTopic(card('tv', '1'));
  turn(c, '2', '不用投了'); c.updateTopic(card('tv', '2', {expected_revision: 1, status: 'cancelled'}));
  assert.equal(c.packet('继续').working_topics.length, 0);
  assert.equal(c.packet('继续').recent_closed_topics[0].status, 'cancelled');
  assert.throws(() => c.updateTopic(card('tv', '1', {expected_revision: 1})), /revision/);
  assert.throws(() => c.updateTopic(card('tv', '1', {expected_revision: 2})), /older context/);
});

test('a purported user fact cannot quote an assistant guess', t => {
  const c = context(t);
  turn(c, '1', '没画面', '手机锁住了接收器');
  assert.throws(() => c.updateTopic(card('tv', '1', {facts: [
    {text: '手机锁死接收器', kind: 'user', source_id: '1', quote: '手机锁住了接收器'}]})), /quote/);
  const data = c.updateTopic(card('tv', '1', {facts: [
    {text: '手机锁死接收器', kind: 'assistant_report', source_id: '1', quote: '手机锁住了接收器'}]}));
  assert.equal(data.facts[0].kind, 'assistant_report');
});

test('long reply retains the final result, and injection is bounded', t => {
  const c = context(t);
  const answer = '先检查网络。' + '尝试中。'.repeat(1000) + '最终：未验证，不能宣称成功。';
  assert.match(clip(answer, 300), /最终：未验证/);
  for (let i = 0; i < 30; i++) turn(c, String(i), '电视投屏检查'.repeat(200), answer);
  const p = c.packet('电视投屏');
  assert.ok(JSON.stringify(p).length <= 6800);
  assert.match(p.recent_dialogue.at(-1).assistant_report, /最终：未验证/);
});

test('related older topic can be retrieved without treating it as current', t => {
  const c = context(t);
  turn(c, 'tv', 'Redmi电视 Friday Night Dinner 第四集', '找到片源');
  for (let i = 0; i < 5; i++) turn(c, String(i), '今天天气', '晴天');
  const p = c.packet('Friday Night Dinner 第四集');
  assert.equal(p.related_older_dialogue[0].id, 'tv');
  assert.notEqual(p.recent_dialogue.at(-1).id, 'tv');
});

test('credentials are redacted, references remain searchable', t => {
  const c = context(t);
  turn(c, '1', '查看 https://example.com/report?token=secret-token 文件 /Users/zhen/report.md', 'Bearer abc123');
  const text = JSON.stringify(c.get('1'));
  assert.ok(!text.includes('secret-token')); assert.ok(!text.includes('abc123'));
  assert.ok(text.includes('/Users/zhen/report.md'));
});
