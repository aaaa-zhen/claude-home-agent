import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {ConversationContext} from '../scripts/conversation-context.mjs';
import {assertReplyContent, generateReply, recordReplyDelivery} from '../scripts/chat-reply-lifecycle.mjs';
import {validateSendResponse} from '../scripts/weixin-response-validation.mjs';
import {checkChatReply} from '../scripts/chat-health.mjs';
import {patchChatReliability} from '../patches/patch-chat-reliability.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const original = fs.readFileSync(process.env.HOME_AGENT_SDK_FIXTURE || path.join(root, 'node_modules/weixin-agent-sdk/dist/index.mjs'), 'utf8');
const patched = patchChatReliability(original, root);
const cut = (begin, end) => patched.slice(patched.indexOf(begin), patched.indexOf(end, patched.indexOf(begin)));
function compile(source, name, values) {
  return new Function(...Object.keys(values), source + ';return ' + name)(...Object.values(values));
}
function sender(fetch) {
  return compile(cut('async function sendMessage(params)', '/** Fetch bot config'), 'sendMessage', {
    apiFetch: fetch, buildBaseInfo: () => ({}), DEFAULT_API_TIMEOUT_MS: 30, validateSendResponse,
  });
}
function context(t) {
  const c = new ConversationContext(':memory:');
  t.after(() => c.close());
  c.receive('打开主卧空调', 'home-agent:shared', 'turn-1'); c.activate('turn-1');
  return c;
}
async function processReply(c, response, options = {}) {
  const notices = [], sends = [], outcomes = [];
  const send = sender(async () => {
    sends.push('attempt');
    if (options.sendError) throw options.sendError;
    return options.body || '{"ret":0}';
  });
  const values = {
    Date, crypto, logger: {info(){},error(){},warn(){}},
    extractTextBody: () => '打开主卧空调', bodyFromItemList: () => '打开主卧空调',
    setContextToken(){}, maybeHandleSelfRestart: async () => false, maybeHandleAgentOSGateway: async () => false,
    findMediaItem: () => undefined, assertReplyContent, path,
    fs: {existsSync: () => false}, markdownToPlainText: text => text,
    redactSecrets: text => text,
    sendMessageWeixin: send, sendWeixinErrorNotice: x => notices.push(x),
    appendWeixinTurnMemory: async () => {if (options.memoryError) throw new Error('local disk error');},
    clearInterval, sendTyping: async () => {},
  };
  const process = compile(cut('async function processOneMessage(full, deps)', '//#endregion'), 'processOneMessage', values);
  const agent = {
    chat: request => generateReply({chat: async () => response, request, context: c, turnId: 'turn-1'}),
    onDelivery(reply, outcome) {
      outcomes.push(outcome);
      recordReplyDelivery(c, reply, outcome);
      if (options.receiptError) throw new Error('receipt failed after accepted send');
    },
  };
  await process({from_user_id: 'fake-owner', context_token: 'fake-context', msg_id: 'fake-id'}, {agent});
  return {notices, sends, outcomes, row: c.get('turn-1')};
}

test('thinking-only and blank responses are rejected; actual media stays valid', () => {
  for (const r of [undefined, {}, {text: ''}, {text: ' \n '}, {media: {type: 'image', url: ''}}]) assert.throws(() => assertReplyContent(r), e => e.code === 'EMPTY_RESPONSE');
  assertReplyContent({media: {type: 'image', url: '/tmp/example.png'}});
  assertReplyContent({text: '结果已核实'});
});
test('SDK empty turn becomes a recorded failure and one error notice, with no false media or send', async t => {
  const r = await processReply(context(t), {});
  assert.equal(r.row.status, 'empty_response'); assert.equal(r.row.assistant_text, '');
  assert.equal(r.notices.length, 1); assert.equal(r.sends.length, 0); assert.deepEqual(r.outcomes, []);
});
test('content generation alone is not delivery success', async t => {
  const c=context(t);
  const reply=await generateReply({chat:async()=>({text:'完成'}),request:{text:'请求'},context:c,turnId:'turn-1'});
  assert.equal(c.get('turn-1').status,'generated');
  recordReplyDelivery(c,reply,'accepted'); assert.equal(c.get('turn-1').status,'accepted');
});
test('real SDK foreground flow records accepted only after business success', async t => {
  const r=await processReply(context(t), {text:'主卧空调状态已核实'});
  assert.equal(r.row.status,'accepted'); assert.deepEqual(r.outcomes,['accepted']); assert.equal(r.notices.length,0);
});
test('HTTP 200 ret=-2 cannot become a successful foreground receipt', async t => {
  const r=await processReply(context(t),{text:'待发送回答'},{body:'{"ret":-2}'});
  assert.equal(r.row.status,'rejected'); assert.equal(r.row.assistant_text,'待发送回答'); assert.deepEqual(r.outcomes,['rejected']);
});
test('transport timeout is unknown, not an accepted or safely retryable send', async t => {
  const r=await processReply(context(t),{text:'待发送回答'},{sendError:new Error('network timeout')});
  assert.equal(r.row.status,'delivery_unknown'); assert.equal(r.sends.length,1);
});
test('malformed successful HTTP body is an unknown delivery', async t => {
  const r=await processReply(context(t),{text:'待发送回答'},{body:'<html>'});
  assert.equal(r.row.status,'delivery_unknown');
});
test('HTTP 4xx remains an explicit refusal', async () => {
  await assert.rejects(sender(async()=>{throw new Error('sendMessage 401: unauthorized');})({}),e=>e.deliveryUnknown===false);
});
test('receipt/bookkeeping failure cannot downgrade or resend an accepted answer', async t => {
  const r=await processReply(context(t),{text:'成功回答'},{receiptError:true,memoryError:true});
  assert.equal(r.row.status,'accepted'); assert.deepEqual(r.outcomes,['accepted']); assert.equal(r.sends.length,1); assert.equal(r.notices.length,0);
});
test('missing media records the actual error text that was sent', async t => {
  const r=await processReply(context(t),{media:{type:'file',url:'/tmp/missing.pdf'}});
  assert.equal(r.row.status,'accepted'); assert.match(r.row.assistant_text,/文件没找到/); assert.notEqual(r.row.assistant_text,'[媒体回复]');
});
test('crash after generation retains text and marks uncertain delivery for inspection', t => {
  const c=context(t);c.complete('turn-1','已生成的回答','generated');c.recover();
  assert.equal(c.get('turn-1').status,'delivery_unknown');assert.equal(c.get('turn-1').assistant_text,'已生成的回答');
  assert.equal(c.packet('刚才的').interrupted_requests[0].id,'turn-1');
});
test('empty turn does not replay actions and a later request can succeed', async t => {
  const c=context(t);let calls=0;
  await assert.rejects(generateReply({chat:async()=>{calls++;return {};},request:{text:'请求'},context:c,turnId:'turn-1'}));
  assert.equal(calls,1);c.receive('接下来','home-agent:shared','turn-2');c.activate('turn-2');
  const r=await generateReply({chat:async()=>({text:'现在正常'}),request:{text:'接下来'},context:c,turnId:'turn-2'});
  recordReplyDelivery(c,r,'accepted');assert.equal(c.get('turn-2').status,'accepted');
});
test('internal probes do not create a real user receipt', async t => {
  const c=context(t);const r=await generateReply({chat:async()=>({text:'探测正常'}),request:{text:'探测'},context:c,turnId:null});
  recordReplyDelivery(c,r,'accepted');assert.equal(c.get('turn-1').status,'processing');assert.equal(r.homeAgentTurnId,undefined);
});
test('legitimate empty compact command is not mistaken for an empty user answer', async () => {
  const r=await generateReply({chat:async()=>({text:''}),request:{text:'/compact keep tasks'},turnId:null});assert.equal(r.text,'');
});
test('reply monitor catches empty and stuck replies without relying on the heartbeat model', () => {
  const now=Date.now(), received_at=new Date(now-360000).toISOString();
  for(const status of ['empty_response','interrupted','rejected','delivery_unknown','processing','generated'])assert.equal(checkChatReply({status,received_at},now).ok,false);
  assert.equal(checkChatReply({status:'accepted',received_at},now).ok,true);
});
test('SDK patch is repeatable and rejects an unsupported bundle', () => {
  assert.equal(patchChatReliability(patched,root),patched);
  assert.throws(()=>patchChatReliability('unexpected source',root),/Unsupported/);
});
test('HTTP deadline includes body consumption, not just response headers', async () => {
  const fn=compile(cut('async function apiFetch(params)', '/**\n* Long-poll getUpdates'), 'apiFetch', {
    ensureTrailingSlash:s=>s+'/', buildHeaders:()=>({}), logger:{debug(){}}, redactUrl:s=>s, redactBody:s=>s,
    AbortController,setTimeout,clearTimeout,
    fetch:async(_url,{signal})=>({ok:true,status:200,text:()=>new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('body timed out')),{once:true}))}),
  });
  await assert.rejects(fn({baseUrl:'https://test.invalid',endpoint:'send',body:'{}',timeoutMs:20}),/body timed out/);
});
