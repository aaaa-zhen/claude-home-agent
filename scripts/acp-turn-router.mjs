import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
export function defaultTranscriptPath(sessionId, {configDir = process.env.CLAUDE_CONFIG_DIR, cwd = process.cwd()} = {}) {
  if (!sessionId) return null;
  const project = cwd.replace(/[^A-Za-z0-9]/g, '-');
  return path.join(configDir || path.join(os.homedir(), '.claude'), 'projects', project, `${sessionId}.jsonl`);
}
export function localCommandFailure(line) {
  if (!line || !line.includes('local_command')) return null;
  let entry;
  try { entry = JSON.parse(line); } catch { return null; }
  if (entry?.type !== 'system' || entry.subtype !== 'local_command' || typeof entry.content !== 'string') return null;
  const text = entry.content.replace(/<\/?local-command-[a-z]+>/g, '').trim();
  const failed = entry.content.includes('<local-command-stderr>') ||
    /^error\b|error during compaction|no messages to compact|not enough messages/i.test(text);
  return failed ? text.slice(0, 300) : null;
}
// Claude emits text before replaying its input UUID. Route complete SDK turns
// at the authoritative idle boundary, and keep reading while no chat is active.
export function createTurnRouter(raw, sourceInput, {sessionId, onBackground = async () => {}, maxFrameBytes = 32 * 1024 * 1024,
  localCommandTimeoutMs = Number(process.env.HOME_AGENT_LOCAL_COMMAND_TIMEOUT_MS) || 5 * 60 * 1000,
  transcriptPath = defaultTranscriptPath(sessionId), transcriptPollMs = 2000} = {}) {
  const waiting = new Map();
  // Local commands (/compact, /context, ...) are control round-trips with no
  // model turn events in between. A /compact that fails (e.g. API unreachable)
  // leaves no compact_boundary, and a prompt UUID that never settles blocks
  // every later prompt with "requires serialized prompts". Bound that wait.
  const timers = new Map();
  const watchers = new Map();
  const isLocalCommand = command => command === '/compact' || ['/context', '/heapdump', '/extra-usage'].includes(command);
  function settle(id) {
    waiting.delete(id);
    const timer = timers.get(id);
    if (timer) { clearTimeout(timer); timers.delete(id); }
    const watcher = watchers.get(id);
    if (watcher) { clearInterval(watcher); watchers.delete(id); }
  }
  // Claude records a failed local command in the session transcript at once
  // (system/local_command with the error text) but emits nothing on the SDK
  // stream. Tail the transcript from the push offset so the failure settles in
  // seconds; the timeout above stays as the backstop.
  function watchTranscript(id, command) {
    if (!transcriptPath || !(transcriptPollMs > 0)) return;
    let offset = 0;
    try { offset = fs.statSync(transcriptPath).size; } catch { offset = 0; }
    let carry = '';
    const watcher = setInterval(() => {
      if (!waiting.has(id)) return settle(id);
      let size;
      try { size = fs.statSync(transcriptPath).size; } catch { return; }
      if (size < offset) { offset = 0; carry = ''; }
      if (size === offset) return;
      let chunk;
      try {
        const fd = fs.openSync(transcriptPath, 'r');
        try {
          const buffer = Buffer.alloc(size - offset);
          fs.readSync(fd, buffer, 0, buffer.length, offset);
          chunk = buffer.toString('utf8');
        } finally { fs.closeSync(fd); }
      } catch { return; }
      offset = size;
      const lines = (carry + chunk).split('\n');
      carry = lines.pop();
      for (const line of lines) {
        const failure = localCommandFailure(line);
        if (failure) return failLocalCommand(id, command, failure);
      }
    }, transcriptPollMs);
    watcher.unref?.();
    watchers.set(id, watcher);
  }
  function failLocalCommand(id, command, reason) {
    settle(id);
    console.error(`[acp-turn-router] ${command} did not complete: ${reason}`);
    // Replay the prompt UUID so claude-agent-acp treats the error result as ours,
    // then surface an error result. No trailing idle: the adapter throws on the
    // result and a leftover idle would answer the next prompt with nothing.
    emit({type: 'user', uuid: id, isReplay: true, session_id: sessionId, parent_tool_use_id: null,
      message: {role: 'user', content: [{type: 'text', text: command}]}});
    emit({type: 'result', subtype: 'error_during_execution', uuid: crypto.randomUUID(), session_id: sessionId,
      is_error: true, errors: [`Home Agent: ${command} failed: ${reason}`], result: `${command} failed: ${reason}`,
      duration_ms: 0, duration_api_ms: 0, num_turns: 0, stop_reason: null, total_cost_usd: 0, modelUsage: {},
      usage: {input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0}});
  }
  const ready = [];
  const readers = [];
  let ended = false, failure = null, frame = [], frameBytes = 0, interrupted = false;
  function fail(error) {
    for (const timer of timers.values()) clearTimeout(timer);
    timers.clear();
    for (const watcher of watchers.values()) clearInterval(watcher);
    watchers.clear();
    failure = error instanceof Error ? error : new Error(String(error));
    ended = true;
    ready.length = 0;
    for (const reader of readers.splice(0)) reader.reject(failure);
  }
  function emit(value) {
    const reader = readers.shift();
    if (reader) reader.resolve({value, done: false});
    else ready.push(value);
  }
  function next() {
    if (failure) return Promise.reject(failure);
    if (ready.length) return Promise.resolve({value: ready.shift(), done: false});
    if (ended) return Promise.resolve({done: true});
    return new Promise((resolve, reject) => readers.push({resolve, reject}));
  }
  async function routeBlock(messages) {
    const owners = [...new Set(messages.filter(e => e.type === 'user' && waiting.has(e.uuid)).map(e => e.uuid))];
    // Local commands don't invoke the model and may omit replay. Require their
    // command-specific output event; a background result or idle is insufficient.
    if (!owners.length && waiting.size === 1) {
      const [id, command] = [...waiting][0];
      const localCommand = ['/context', '/heapdump', '/extra-usage'].includes(command);
      const localOutput = messages.some(e => e.type === 'system' &&
        ((command === '/compact' && e.subtype === 'compact_boundary') ||
         (localCommand && e.subtype === 'local_command_output')));
      // Current SDK /context returns a synthetic assistant and zero-turn result, without
      // replay or local_command_output. Model/background replies have turns and
      // output tokens. Never use a bare result/idle as command ownership evidence.
      const localResult = localCommand && !messages.some(e => ['user', 'stream_event'].includes(e.type) ||
        (e.type === 'assistant' && (e.message?.model !== '<synthetic>' || e.message?.usage?.output_tokens !== 0))) &&
        messages.some(e => e.type === 'result' && e.subtype === 'success' && !e.is_error &&
          e.num_turns === 0 && e.stop_reason == null && e.usage?.output_tokens === 0);
      if (localOutput || localResult || interrupted) owners.push(id);
      else if (command === '/compact') {
        // A failed compaction produces no compact_boundary. It leaves either a
        // local command output event or a frame with no model activity at all.
        const commandOutput = messages.some(e => e.type === 'system' && ['local_command_output', 'local_command'].includes(e.subtype));
        const modelActivity = messages.some(e => ['user', 'assistant', 'stream_event', 'tool_use', 'tool_result'].includes(e.type) ||
          (e.type === 'result' && (e.num_turns > 0 || e.usage?.output_tokens > 0)));
        if (commandOutput) owners.push(id);
        else if (!modelActivity) {
          const error = messages.find(e => e.type === 'result' && e.is_error);
          failLocalCommand(id, command, error ? String(error.result || error.errors?.join(', ') || error.subtype) : 'no compact boundary in the SDK turn');
          return false; // handled, but the frame's idle must not follow the error result
        }
      }
    }
    if (owners.length > 1) throw new Error('Multiple prompt UUIDs in one SDK turn; refusing ambiguous reply');
    if (owners.length === 1) {
      settle(owners[0]);
      for (const message of messages) {
        // Cancellation must not accidentally return buffered background text.
        if (!interrupted || message.type === 'result' || message.subtype === 'session_state_changed') emit(message);
      }
      interrupted = false;
      return true;
    }
    const result = messages.findLast(e => e.type === 'result');
    const assistant = messages.findLast(e => e.type === 'assistant' && e.parent_tool_use_id == null && Array.isArray(e.message?.content) && e.message.content.some(c => c.type === 'text'));
    const text = typeof result?.result === 'string' ? result.result :
      (assistant?.message.content.filter(c => c.type === 'text').map(c => c.text).join('\n') || '');
    if (text.trim()) {
      await onBackground({id: result?.uuid || messages.at(-1).uuid, sessionId, text: text.trim(), isError: Boolean(result?.is_error)});
    }
  }
  async function finishTurn(messages) {
    // One idle interval may contain both a foreground result and autonomous
    // follow-ups. Result boundaries keep their text separate within that interval.
    let block = [], owned = false, routed = 0;
    const idle = messages.at(-1);
    for (const message of messages.slice(0, -1)) {
      block.push(message);
      if (message.type === 'result') {
        owned = Boolean(await routeBlock(block)) || owned;
        routed++;
        block = [];
      }
    }
    if (block.length) owned = Boolean(await routeBlock(block)) || owned;
    // An idle with nothing at all before it is still a completed SDK turn: a
    // failed /compact ends exactly like that, and the pending prompt must settle.
    else if (!routed && waiting.size) owned = Boolean(await routeBlock([])) || owned;
    if (owned) emit(idle);
  }
  const input = new Proxy(sourceInput, {get(target, key) {
    if (key === 'push') return message => {
      if (failure) throw failure;
      if (ended) throw new Error('SDK turn stream is closed');
      if (!message.uuid) throw new Error('Prompt UUID is required');
      if (waiting.size) throw new Error('Home Agent requires serialized prompts');
      const content = message.message?.content;
      const text = typeof content === 'string' ? content : content?.find(c => c.type === 'text')?.text || '';
      const command = text.trim().split(/\s/, 1)[0];
      waiting.set(message.uuid, command);
      if (isLocalCommand(command) && localCommandTimeoutMs > 0) {
        const timer = setTimeout(() => {
          if (waiting.has(message.uuid)) failLocalCommand(message.uuid, command, `no completion event within ${Math.round(localCommandTimeoutMs / 1000)}s`);
        }, localCommandTimeoutMs);
        timer.unref?.();
        timers.set(message.uuid, timer);
        watchTranscript(message.uuid, command);
      }
      try { return target.push(message); }
      catch (error) { settle(message.uuid); throw error; }
    };
    const value = Reflect.get(target, key);
    return typeof value === 'function' ? value.bind(target) : value;
  }});
  let query;
  query = new Proxy(raw, {get(target, key) {
    if (key === 'next') return next;
    if (key === Symbol.asyncIterator) return () => query;
    if (key === 'interrupt') return (...args) => { interrupted = true; return target.interrupt(...args); };
    if (key === 'close') return (...args) => {
      fail(new Error('SDK turn stream closed'));
      return target.close(...args);
    };
    const value = Reflect.get(target, key);
    return typeof value === 'function' ? value.bind(target) : value;
  }});
  const pumping = (async () => {
    try {
      while (!ended) {
        const {value: event, done} = await raw.next();
        if (done) {
          if (waiting.size || frame.length) throw new Error('SDK stream ended before a correlated idle boundary');
          ended = true;
          for (const reader of readers.splice(0)) reader.resolve({done: true});
          break;
        }
        frameBytes += Buffer.byteLength(JSON.stringify(event));
        if (frameBytes > maxFrameBytes) throw new Error('SDK turn exceeds transport buffer limit');
        frame.push(event);
        if (event.type === 'system' && event.subtype === 'session_state_changed' && event.state === 'idle') {
          const complete = frame; frame = []; frameBytes = 0;
          await finishTurn(complete);
        }
      }
    } catch (error) { fail(error); raw.close?.(); }
  })();
  return {query, input, pumping};
}
