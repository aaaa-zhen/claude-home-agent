import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createTurnRouter} from '../scripts/acp-turn-router.mjs';
import {BackgroundReplies} from '../scripts/background-replies.mjs';
import {ClaudeAcpAgent} from '/Users/zhen/home-agent/weixin-agent/node_modules/@zed-industries/claude-agent-acp/dist/acp-agent.js';
class Feed {
 constructor(){this.items=[];this.wait=[];this.closed=false;this.reads=0;}
 push(x){const done=this.wait.shift();if(done)done({value:x,done:false});else this.items.push(x);}
 next(){this.reads++;if(this.items.length)return Promise.resolve({value:this.items.shift(),done:false});if(this.closed)return Promise.resolve({done:true});return new Promise(r=>this.wait.push(r));}
 close(){this.closed=true;for(const r of this.wait.splice(0))r({done:true});}
 interrupt(){return Promise.resolve();}
}
const idle=()=>({type:'system',subtype:'session_state_changed',state:'idle',uuid:crypto.randomUUID()});
const result=(text)=>({type:'result',subtype:'success',uuid:crypto.randomUUID(),result:text,is_error:false,usage:{input_tokens:1,output_tokens:1,cache_read_input_tokens:0,cache_creation_input_tokens:0},modelUsage:{},total_cost_usd:0});
function block(text,id){return [
 {type:'stream_event',event:{type:'content_block_delta',index:0,delta:{type:'text_delta',text}},parent_tool_use_id:null},
 ...(id?[{type:'user',uuid:id,isReplay:true,message:{role:'user',content:[{type:'text',text:'request'}]},parent_tool_use_id:null}]:[]),result(text)];}
function fixture(){const raw=new Feed(),events=[],input={push(message){this.last=message;}};const routed=createTurnRouter(raw,input,{sessionId:'s',onBackground:e=>events.push(e)});return {raw,events,input,...routed};}
async function turn(f,text='latest',id='own'){f.input.push({uuid:id,message:{content:text}});const out=[];for(;;){const e=await f.query.next();assert.equal(e.done,false);out.push(e.value);if(e.value.state==='idle')return out;}}
const tick=()=>new Promise(r=>setImmediate(r));
test('idle background result is delivered without another user message',async()=>{const f=fixture();for(const e of [...block('installed'),idle()])f.raw.push(e);await tick();assert.equal(f.events[0].text,'installed');f.raw.close();await f.pumping;});
test('background followed by current input does not shift the reply',async()=>{const f=fixture();const p=turn(f);for(const e of [...block('installed'),idle(),...block('AC off','own'),idle()])f.raw.push(e);const own=await p;assert.deepEqual(own.filter(e=>e.type==='result').map(e=>e.result),['AC off']);assert.equal(f.events[0].text,'installed');f.raw.close();});
test('text streamed before input UUID replay is preserved',async()=>{const f=fixture();const p=turn(f);for(const e of [...block('answer','own'),idle()])f.raw.push(e);assert.equal((await p)[0].event.delta.text,'answer');assert.equal(f.events.length,0);f.raw.close();});
test('background and user results within the same idle interval stay separate',async()=>{const f=fixture();const p=turn(f);for(const e of [...block('old task'),...block('current answer','own'),...block('other task'),idle()])f.raw.push(e);const own=await p;assert.deepEqual(own.filter(e=>e.type==='result').map(e=>e.result),['current answer']);assert.deepEqual(f.events.map(e=>e.text),['old task','other task']);f.raw.close();});
test('three consecutive messages remain matched after multiple background events',async()=>{const f=fixture();for(const id of ['aircon','1','2']){const p=turn(f,id,id);for(const e of [...block('bg:'+id),idle(),...block('answer:'+id,id),idle()])f.raw.push(e);assert.equal((await p).find(e=>e.type==='result').result,'answer:'+id);}assert.equal(f.events.length,3);f.raw.close();});
test('compact without replay requires compact boundary and ignores earlier background idle',async()=>{const f=fixture();const p=turn(f,'/compact preserve topic');for(const e of [...block('background'),idle(),{type:'system',subtype:'compact_boundary'},result('compact'),idle()])f.raw.push(e);assert.equal((await p).find(e=>e.type==='result').result,'compact');assert.equal(f.events.length,1);f.raw.close();});
test('local command requires its own output evidence',async()=>{const f=fixture();const p=turn(f,'/context');for(const e of [...block('background'),idle(),{type:'system',subtype:'local_command_output',content:'context'},result('context'),idle()])f.raw.push(e);assert.equal((await p).find(e=>e.type==='result').result,'context');f.raw.close();});
test('unexpected stream termination rejects pending request instead of serving stale output',async()=>{const f=fixture();const p=turn(f);f.raw.close();await assert.rejects(p,/before a correlated/);});
test('cancellation suppresses buffered text',async()=>{const f=fixture();const p=turn(f);await f.query.interrupt();for(const e of [...block('stale output'),idle()])f.raw.push(e);assert.equal((await p).some(e=>e.type==='stream_event'),false);f.raw.close();});
test('buffer limit fails closed',async()=>{const raw=new Feed();const f=createTurnRouter(raw,{push(){}},{maxFrameBytes:20});const p=turn(f);raw.push({type:'assistant',message:'x'.repeat(100)});await assert.rejects(p,/buffer limit/);});
async function adapterRun(routed){const raw=new Feed(),input={push(m){this.last=m}},seen=[],background=[];let stream={query:raw,input};if(routed)stream=createTurnRouter(raw,input,{sessionId:'s',onBackground:e=>background.push(e)});const agent=new ClaudeAcpAgent({sessionUpdate:async p=>seen.push(p)},{log(){},error(){}});agent.sessions.s={...stream,cancelled:false,promptRunning:false,pendingMessages:new Map(),nextPendingOrder:0,cwd:'/tmp'};const p=agent.prompt({sessionId:'s',prompt:[{type:'text',text:'current request'}]});await tick();const uuid=input.last.uuid;for(const e of [...block('PREVIOUS TASK'),idle(),...block('CURRENT ANSWER',uuid),idle()])raw.push(e);await p;raw.close();return {seen,background};}
test('real installed ACP adapter reproduces old failure without routing',async()=>{const r=await adapterRun(false);assert.equal(r.seen.find(e=>e.update.sessionUpdate==='agent_message_chunk').update.content.text,'PREVIOUS TASK');});
test('real installed ACP adapter returns correct answer with routing',async()=>{const r=await adapterRun(true);assert.deepEqual(r.seen.filter(e=>e.update.sessionUpdate==='agent_message_chunk').map(e=>e.update.content.text),['CURRENT ANSWER']);assert.equal(r.background[0].text,'PREVIOUS TASK');});
test('background delivery is deduplicated and has its own receipt',async()=>{const dir=fs.mkdtempSync(path.join(os.tmpdir(),'bg-routing-'));try{const sent=[],records=[];const sink=new BackgroundReplies({directory:dir,send:async t=>sent.push(t),record:e=>records.push(e)});const e={id:'x',sessionId:'s',text:'done'};await Promise.all([sink.enqueue(e),sink.enqueue(e)]);assert.deepEqual(sent,['后台任务更新：\ndone']);assert.equal(records.length,1);assert.equal(JSON.parse(fs.readFileSync(path.join(dir,fs.readdirSync(dir)[0]))).status,'accepted');}finally{fs.rmSync(dir,{recursive:true});}});
test('uncertain background delivery is not retried or recorded as delivered',async()=>{const dir=fs.mkdtempSync(path.join(os.tmpdir(),'bg-routing-'));try{let attempts=0,records=0;const sink=new BackgroundReplies({directory:dir,send:async()=>{attempts++;throw Object.assign(new Error('timeout'),{deliveryUnknown:true});},record:()=>records++});const e={id:'x',sessionId:'s',text:'done'};await sink.enqueue(e);await sink.enqueue(e);assert.equal(attempts,1);assert.equal(records,0);assert.equal(JSON.parse(fs.readFileSync(path.join(dir,fs.readdirSync(dir)[0]))).status,'unknown');}finally{fs.rmSync(dir,{recursive:true});}});

import {patchClaude,patchWeixin} from '../patches/patch-acp-turn-router.mjs';
test('patch migration is idempotent and preserves unrelated package code',()=>{const c=fs.readFileSync('/Users/zhen/home-agent/weixin-agent/node_modules/@zed-industries/claude-agent-acp/dist/acp-agent.js','utf8');const changed=patchClaude(c,'/example/acp-turn-router.mjs');assert.equal(patchClaude(changed,'/example/acp-turn-router.mjs'),changed);assert.ok(!changed.includes('weixin sequential prompt replay patch'));const w=fs.readFileSync('/Users/zhen/home-agent/weixin-agent/node_modules/weixin-acp/dist/acp-agent-BUZjysVy.mjs','utf8');const patched=patchWeixin(w);assert.equal(patchWeixin(patched),patched);assert.ok(patched.includes('send_file patch'));assert.throws(()=>patchClaude('unrecognized','/x'),/Unsupported/);});

test('local zero-turn command result remains separate from a model background reply',async()=>{
 const f=fixture();const p=turn(f,'/context');
 for(const e of [...block('background'),idle(),{...result('## Context Usage'),num_turns:0,stop_reason:null,usage:{output_tokens:0}},idle()])f.raw.push(e);
 assert.equal((await p).find(e=>e.type==='result').result,'## Context Usage');
 assert.equal(f.events[0].text,'background');f.raw.close();
});
test('SDK background receipt keeps a stable conversation event identity at the shared sender',async()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'bg-identity-'));
 try{let options;const sink=new BackgroundReplies({directory:dir,send:async(text,o)=>{options=o;}});
 await sink.enqueue({id:'result-1',sessionId:'session-1',text:'done'});
 assert.deepEqual(options.notification,{id:'sdk-background:session-1:result-1',source:'sdk-background'});
 }finally{fs.rmSync(dir,{recursive:true});}
});

test('SDK synthetic assistant accompanying /context is not a background completion',async()=>{
 const f=fixture();const p=turn(f,'/context');
 for(const e of [{type:'assistant',message:{model:'<synthetic>',usage:{output_tokens:0},content:[{type:'text',text:'## Context Usage'}]}},{...result('## Context Usage'),num_turns:0,stop_reason:null,usage:{output_tokens:0}},idle()])f.raw.push(e);
 assert.equal((await p).find(e=>e.type==='result').result,'## Context Usage');assert.equal(f.events.length,0);f.raw.close();
});

// 2026-09-20: a /compact that fails (API unreachable) has no compact_boundary and used to
// leave its prompt UUID pending forever, rejecting every later prompt as unserialized.
async function untilError(f){const out=[];for(;;){const e=await f.query.next();assert.equal(e.done,false);out.push(e.value);if(e.value.type==='result'&&e.value.is_error)return out;}}
test('failed compact with a bare idle frame surfaces an error result and frees the router',async()=>{
 const f=fixture();f.input.push({uuid:'c1',message:{content:'/compact keep topics'}});
 const p=untilError(f);f.raw.push(idle());const out=await p;
 assert.deepEqual(out.map(e=>e.type),['user','result']);assert.equal(out[0].uuid,'c1');assert.match(out[1].errors[0],/compact failed/);
 assert.equal(f.events.length,0);
 const q=turn(f,'next question','own');for(const e of [...block('answer','own'),idle()])f.raw.push(e);
 assert.equal((await q).find(e=>e.type==='result').result,'answer');f.raw.close();
});
test('failed compact reported through local command output is owned by the compact prompt',async()=>{
 const f=fixture();const p=turn(f,'/compact keep topics');
 for(const e of [{type:'system',subtype:'local_command_output',content:'Error during compaction: API Error'},idle()])f.raw.push(e);
 const out=await p;assert.equal(out[0].subtype,'local_command_output');assert.equal(f.events.length,0);f.raw.close();
});
test('compact is not attributed to a frame that contains a background model reply',async()=>{
 const f=fixture();const p=turn(f,'/compact keep topics');
 for(const e of [...block('background'),idle(),{type:'system',subtype:'compact_boundary'},result('compact'),idle()])f.raw.push(e);
 assert.equal((await p).find(e=>e.type==='result').result,'compact');assert.deepEqual(f.events.map(e=>e.text),['background']);f.raw.close();
});
test('pending compact with no SDK events at all times out into an error result',async()=>{
 const raw=new Feed(),events=[],input={push(m){this.last=m;}};const f={raw,events,input,...createTurnRouter(raw,input,{sessionId:'s',onBackground:e=>events.push(e),localCommandTimeoutMs:30})};
 f.input.push({uuid:'c2',message:{content:'/compact keep topics'}});const out=await untilError(f);
 assert.match(out[1].errors[0],/no completion event within/);
 // the late boundary is ignored and the next prompt still works
 for(const e of [{type:'system',subtype:'compact_boundary'},result(''),idle()])raw.push(e);await tick();
 const q=turn(f,'next','own');for(const e of [...block('answer','own'),idle()])raw.push(e);
 assert.equal((await q).find(e=>e.type==='result').result,'answer');assert.equal(events.length,0);raw.close();
});
test('real installed ACP adapter rejects a failed compact and answers the next prompt',async()=>{
 const raw=new Feed(),input={push(m){this.last=m}},seen=[];const stream=createTurnRouter(raw,input,{sessionId:'s',onBackground(){}});
 const agent=new ClaudeAcpAgent({sessionUpdate:async p=>seen.push(p)},{log(){},error(){}});
 agent.sessions.s={...stream,cancelled:false,promptRunning:false,pendingMessages:new Map(),nextPendingOrder:0,cwd:'/tmp',accumulatedUsage:{inputTokens:0,outputTokens:0,cachedReadTokens:0,cachedWriteTokens:0}};
 const p=agent.prompt({sessionId:'s',prompt:[{type:'text',text:'/compact keep topics'}]});await tick();raw.push(idle());
 await assert.rejects(p,e=>/compact failed/.test(e.message||'')||/compact failed/.test(JSON.stringify(e)));
 const p2=agent.prompt({sessionId:'s',prompt:[{type:'text',text:'next question'}]});await tick();
 const uuid=input.last.uuid;for(const e of [...block('REAL ANSWER',uuid),idle()])raw.push(e);
 await Promise.race([p2,new Promise((_,rej)=>setTimeout(()=>rej(new Error('second prompt hung after failed compact')),2000))]);
 assert.deepEqual(seen.filter(e=>e.update.sessionUpdate==='agent_message_chunk').map(e=>e.update.content.text),['REAL ANSWER']);raw.close();
});

import {localCommandFailure, defaultTranscriptPath} from '../scripts/acp-turn-router.mjs';
test('transcript local_command failure lines are recognised, success output is not',()=>{
 assert.match(localCommandFailure(JSON.stringify({type:'system',subtype:'local_command',content:'<local-command-stderr>Error: No messages to compact</local-command-stderr>'})),/No messages to compact/);
 assert.match(localCommandFailure(JSON.stringify({type:'system',subtype:'local_command',content:'<local-command-stdout>Error during compaction: API Error: Unable to connect to API (ConnectionRefused)</local-command-stdout>'})),/ConnectionRefused/);
 assert.equal(localCommandFailure(JSON.stringify({type:'system',subtype:'local_command',content:'<local-command-stdout>## Context Usage</local-command-stdout>'})),null);
 assert.equal(localCommandFailure('not json local_command'),null);
 assert.equal(defaultTranscriptPath('abc',{configDir:'/cfg',cwd:'/Users/zhen/home-agent/weixin-agent'}),'/cfg/projects/-Users-zhen-home-agent-weixin-agent/abc.jsonl');
});
test('compact failure written to the transcript settles the prompt within a poll interval',async()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'router-transcript-'));const tp=path.join(dir,'s.jsonl');fs.writeFileSync(tp,'{"type":"user","message":{"content":"earlier"}}\n');
 try{
  const raw=new Feed(),input={push(m){this.last=m;}},events=[];const f={raw,input,events,...createTurnRouter(raw,input,{sessionId:'s',onBackground:e=>events.push(e),transcriptPath:tp,transcriptPollMs:20,localCommandTimeoutMs:60000})};
  const started=Date.now();f.input.push({uuid:'c3',message:{content:'/compact keep topics'}});
  fs.appendFileSync(tp,JSON.stringify({type:'user',message:{content:'<command-name>/compact</command-name>'}})+'\n'+JSON.stringify({type:'system',subtype:'local_command',content:'<local-command-stderr>Error: No messages to compact</local-command-stderr>'})+'\n');
  const out=await untilError(f);assert.match(out[1].errors[0],/No messages to compact/);assert.ok(Date.now()-started<2000);
  const q=turn(f,'next','own');for(const e of [...block('answer','own'),idle()])raw.push(e);assert.equal((await q).find(e=>e.type==='result').result,'answer');assert.equal(events.length,0);raw.close();
 }finally{fs.rmSync(dir,{recursive:true});}
});
