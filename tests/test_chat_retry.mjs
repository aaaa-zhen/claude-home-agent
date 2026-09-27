import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createRetryingChat, isTransientError, transcriptToolActivity, shortReason} from '../scripts/chat-turn-retry.mjs';
import {generateReply} from '../scripts/chat-reply-lifecycle.mjs';
import {ConversationContext} from '../scripts/conversation-context.mjs';

const overloaded = () => ({code: -32603, message: 'Internal error: API Error: 529 Overloaded. This is a server-side issue, usually temporary — try again in a moment.'});
const refused = () => ({code: -32603, message: 'Internal error: API Error: Unable to connect to API (ConnectionRefused)'});
const request = {conversationId: 'home-agent:shared', text: '客厅的空调不是卧室的空调'};

function harness(t, {fail = [], transcript = null} = {}) {
  const calls = [], logs = [], notices = [], sleeps = [];
  let response = {text: '客厅空调已关'};
  const chat = async () => {
    calls.push(1);
    const error = fail.shift();
    if (error) throw error;
    return response;
  };
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chat-retry-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const file = path.join(dir, 'session.jsonl');
  if (transcript !== null) fs.writeFileSync(file, transcript);
  const retrying = createRetryingChat({
    chat, transcriptPath: () => file, retries: 1, delayMs: 30000,
    sleep: async ms => { sleeps.push(ms); }, log: m => logs.push(m),
  });
  return {calls, logs, notices, sleeps, file, run: () => retrying(request, {notify: async text => { notices.push(text); }})};
}

test('transient classification: 529, refused connection and resets yes; empty answers and logic errors no', () => {
  assert.equal(isTransientError(overloaded()), true);
  assert.equal(isTransientError(refused()), true);
  assert.equal(isTransientError(new Error('read ECONNRESET')), true);
  assert.equal(isTransientError(new Error('fetch failed')), true);
  assert.equal(isTransientError(Object.assign(new Error('空'), {code: 'EMPTY_RESPONSE'})), false);
  assert.equal(isTransientError(new Error('Home Agent requires serialized prompts')), false);
  assert.equal(isTransientError(new Error('Invalid params')), false);
  assert.equal(shortReason(overloaded()), '模型服务端拥堵（529）');
});

test('one 529 then success: retried once after a delay, user told once, answer returned', async t => {
  const h = harness(t, {fail: [overloaded()]});
  const out = await h.run();
  assert.equal(out.text, '客厅空调已关');
  assert.equal(h.calls.length, 2);
  assert.deepEqual(h.sleeps, [30000]);
  assert.equal(h.notices.length, 1);
  assert.match(h.notices[0], /客厅的空调不是卧室的空调/);
  assert.match(h.notices[0], /529/);
  assert.match(h.logs[0], /retry 1\/1 in 30000ms/);
});

test('two transient failures: final error is user readable, keeps the ACP code and original cause', async t => {
  const h = harness(t, {fail: [overloaded(), refused()]});
  await assert.rejects(h.run(), error => {
    assert.equal(error.code, -32603);
    assert.equal(error.cause.message, refused().message);
    assert.match(error.message, /自动重试 1 次后还是失败/);
    assert.match(error.message, /网络或代理连不上/);
    assert.match(error.message, /请稍后重发/);
    return true;
  });
  assert.equal(h.calls.length, 2);
  assert.equal(h.notices.length, 1);
});

test('non-transient errors are thrown through untouched with no retry or notice', async t => {
  const original = new Error('Home Agent requires serialized prompts');
  const h = harness(t, {fail: [original]});
  await assert.rejects(h.run(), error => error === original);
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.notices, []);
  assert.deepEqual(h.sleeps, []);
});

test('empty model answers are never replayed', async t => {
  const empty = Object.assign(new Error('刚才这条没有生成有效回复'), {code: 'EMPTY_RESPONSE'});
  const h = harness(t, {fail: [empty]});
  await assert.rejects(h.run(), error => error === empty);
  assert.equal(h.calls.length, 1);
});

test('local commands such as /compact are not retried by the bridge', async t => {
  const h = harness(t, {fail: [overloaded()]});
  const retrying = createRetryingChat({chat: async () => { h.calls.push(1); throw overloaded(); }, sleep: async () => {}});
  await assert.rejects(retrying({...request, text: '/compact 保留:未完成任务'}), error => error.code === -32603 && !error.transientTurnFailure);
  assert.equal(h.calls.length, 1);
});

const line = obj => JSON.stringify(obj) + '\n';
const textTurn = line({type: 'assistant', message: {role: 'assistant', content: [{type: 'text', text: '好的'}]}});
const toolTurn = line({type: 'assistant', message: {role: 'assistant', content: [{type: 'tool_use', id: 'toolu_1', name: 'Bash', input: {command: 'ha climate off'}}]}});
const toolResult = line({type: 'user', message: {role: 'user', content: [{tool_use_id: 'toolu_1', type: 'tool_result', content: 'ok'}]}});

test('transcript scan only looks at lines appended after the offset', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chat-retry-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const file = path.join(dir, 's.jsonl');
  fs.writeFileSync(file, toolTurn + toolResult);
  const offset = fs.statSync(file).size;
  assert.equal(transcriptToolActivity(file, 0), true);
  assert.equal(transcriptToolActivity(file, offset), false);
  fs.appendFileSync(file, textTurn + '{"partial":');
  assert.equal(transcriptToolActivity(file, offset), false);
  fs.appendFileSync(file, '\n' + toolResult);
  assert.equal(transcriptToolActivity(file, offset), true);
  assert.equal(transcriptToolActivity(path.join(dir, 'missing.jsonl'), 0), false);
  assert.equal(transcriptToolActivity(null, 0), false);
});

test('a turn that already ran a tool before the transient error is not retried', async t => {
  const h = harness(t, {transcript: textTurn});
  const retrying = createRetryingChat({
    chat: async () => { h.calls.push(1); fs.appendFileSync(h.file, toolTurn); throw overloaded(); },
    transcriptPath: () => h.file, sleep: async () => { h.sleeps.push(1); }, log: m => h.logs.push(m),
  });
  await assert.rejects(retrying(request, {notify: async n => { h.notices.push(n); }}), error => {
    assert.match(error.message, /执行到一半被打断/);
    assert.match(error.message, /没有自动重试/);
    return error.code === -32603;
  });
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.sleeps, []);
  assert.deepEqual(h.notices, []);
  assert.match(h.logs[0], /not retrying after tool activity/);
});

test('text-only activity before the failure still allows the retry', async t => {
  const h = harness(t, {transcript: toolTurn});
  let calls = 0;
  const retrying = createRetryingChat({
    chat: async () => { calls++; if (calls === 1) { fs.appendFileSync(h.file, textTurn); throw overloaded(); } return {text: '完成'}; },
    transcriptPath: () => h.file, sleep: async () => {},
  });
  assert.equal((await retrying(request)).text, '完成');
  assert.equal(calls, 2);
});

test('a session created during the failed attempt is scanned from the start', async t => {
  const h = harness(t);
  let file = null;
  const retrying = createRetryingChat({
    chat: async () => { h.calls.push(1); file = h.file; fs.writeFileSync(h.file, toolTurn); throw overloaded(); },
    transcriptPath: () => file, sleep: async () => {},
  });
  await assert.rejects(retrying(request), /执行到一半被打断/);
  assert.equal(h.calls.length, 1);
});

test('retry notice failure does not break the retry itself', async t => {
  const h = harness(t, {fail: [overloaded()]});
  const retrying = createRetryingChat({chat: async () => { h.calls.push(1); if (h.calls.length === 1) throw overloaded(); return {text: '好'}; }, sleep: async () => {}, log: m => h.logs.push(m)});
  assert.equal((await retrying(request, {notify: async () => { throw new Error('sendmessage transport failed'); }})).text, '好');
  assert.equal(h.calls.length, 2);
  assert.ok(h.logs.some(m => /retry notice failed/.test(m)));
});

test('lifecycle: retry success records generated; final failure records interrupted with the ACP code', async t => {
  const c = new ConversationContext(':memory:');
  t.after(() => c.close());
  c.receive('客厅的空调不是卧室的空调', 'home-agent:shared', 'turn-1'); c.activate('turn-1');
  const events = [];
  let calls = 0;
  const flaky = createRetryingChat({chat: async () => { calls++; if (calls === 1) throw overloaded(); return {text: '客厅空调已关'}; }, sleep: async () => {}});
  const reply = await generateReply({chat: flaky, request, context: c, turnId: 'turn-1', report: e => events.push(e)});
  assert.equal(reply.text, '客厅空调已关');
  assert.equal(c.get('turn-1').status, 'generated');
  assert.deepEqual(events.map(e => e.kind), ['generated']);

  c.receive('再试', 'home-agent:shared', 'turn-2'); c.activate('turn-2');
  const dead = createRetryingChat({chat: async () => { throw overloaded(); }, sleep: async () => {}});
  await assert.rejects(generateReply({chat: dead, request: {...request, text: '再试'}, context: c, turnId: 'turn-2', report: e => events.push(e)}), /自动重试 1 次后还是失败/);
  assert.equal(c.get('turn-2').status, 'interrupted');
  assert.equal(events.at(-1).kind, 'interrupted');
  assert.equal(events.at(-1).errorCode, -32603);
});
