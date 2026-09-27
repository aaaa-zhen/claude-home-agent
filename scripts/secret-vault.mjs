#!/usr/bin/env node
// Secret vault on the macOS login Keychain (service "home-agent"), 2026-09-22.
//
// Commands: add / list / use / rm. There is deliberately no "get": nothing here
// prints a stored value. `use` injects the value into a child process
// environment or straight into a browser field, and scrubs the child's output.
// Every use is written to memory/secret-audit.log (name and purpose, never the
// value). `use` only works while a foreground WeChat turn is running, so a
// heartbeat, cron job or background task cannot pull credentials on its own.
//
// Must run inside the agent's launchd session: the Keychain refuses writes from
// a plain SSH session ("User interaction is not allowed").
import {spawn} from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SERVICE = process.env.HOME_AGENT_VAULT_SERVICE || 'home-agent';
const VAULT_DIR = process.env.HOME_AGENT_VAULT_DIR || path.join(ROOT, 'runtime', 'secret-vault');
const AUDIT_PATH = process.env.HOME_AGENT_VAULT_AUDIT || path.join(ROOT, 'memory', 'secret-audit.log');
const EGO_BROWSER = process.env.HOME_AGENT_EGO_BROWSER || path.join(process.env.HOME || '', '.local/bin/ego-browser');
const SECURITY = '/usr/bin/security';
export const FOREGROUND_MAX_AGE_MS = 20 * 60 * 1000;
const NAME_RE = /^[\p{L}\p{N}][\p{L}\p{N}._@+-]{0,63}$/u;

function runProcess(command, args, {input, env, cwd} = {}) {
  return new Promise(resolve => {
    let child;
    try {
      child = spawn(command, args, {env: env || process.env, cwd, stdio: ['pipe', 'pipe', 'pipe']});
    } catch (error) {
      resolve({code: 127, stdout: '', stderr: String(error.message)});
      return;
    }
    let stdout = '', stderr = '';
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.on('error', error => resolve({code: 127, stdout, stderr: stderr + String(error.message)}));
    child.on('close', code => resolve({code: code ?? 1, stdout, stderr}));
    if (input !== undefined) child.stdin.end(input); else child.stdin.end();
  });
}

const defaultSecurity = ({args, input}) => runProcess(SECURITY, args, {input});
const defaultBrowser = ({script}) => runProcess(EGO_BROWSER, ['nodejs'], {input: script});

export function assertName(name) {
  if (!NAME_RE.test(String(name ?? ''))) throw new Error(`密码箱条目名不合法：${JSON.stringify(name)}（只允许字母、数字、中文和 . _ @ + -，最多 64 字符）`);
  return String(name);
}

// Replace every appearance of the secret (raw, URL-encoded, JSON-escaped,
// base64) in text produced by a child process.
export function scrubOutput(text, secrets) {
  let out = String(text ?? '');
  for (const secret of secrets.filter(s => typeof s === 'string' && s.length >= 3)) {
    const forms = new Set([secret, encodeURIComponent(secret), JSON.stringify(secret).slice(1, -1), Buffer.from(secret).toString('base64')]);
    for (const form of forms) if (form) out = out.split(form).join('[secret]');
  }
  return out;
}

export function foregroundMarkerPath(dir = VAULT_DIR) { return path.join(dir, 'foreground.json'); }

export function markForeground(turnId, {dir = VAULT_DIR, now = new Date()} = {}) {
  fs.mkdirSync(dir, {recursive: true, mode: 0o700});
  fs.writeFileSync(foregroundMarkerPath(dir), JSON.stringify({turnId, startedAt: now.toISOString()}), {mode: 0o600});
}

export function clearForeground({dir = VAULT_DIR} = {}) {
  try { fs.unlinkSync(foregroundMarkerPath(dir)); } catch {}
}

export function readForeground({dir = VAULT_DIR, now = new Date(), maxAgeMs = FOREGROUND_MAX_AGE_MS} = {}) {
  let marker;
  try { marker = JSON.parse(fs.readFileSync(foregroundMarkerPath(dir), 'utf8')); } catch { return null; }
  const started = Date.parse(marker?.startedAt);
  if (!Number.isFinite(started) || now.getTime() - started > maxAgeMs) return null;
  return marker;
}

export class SecretVault {
  constructor({service = SERVICE, dir = VAULT_DIR, auditPath = AUDIT_PATH, security = defaultSecurity, browser = defaultBrowser,
    now = () => new Date(), requireForeground = true} = {}) {
    this.service = service;
    this.dir = dir;
    this.auditPath = auditPath;
    this.security = security;
    this.browser = browser;
    this.now = now;
    this.requireForeground = requireForeground;
    this.indexPath = path.join(dir, 'index.json');
  }

  readIndex() {
    try { return JSON.parse(fs.readFileSync(this.indexPath, 'utf8')); } catch { return []; }
  }

  writeIndex(entries) {
    fs.mkdirSync(this.dir, {recursive: true, mode: 0o700});
    fs.writeFileSync(this.indexPath, JSON.stringify(entries, null, 2), {mode: 0o600});
  }

  audit(action, name, fields = {}) {
    const clean = value => String(value ?? '').replace(/[\r\n\t]+/g, ' ').slice(0, 160);
    const line = [this.now().toISOString(), action, name, ...Object.entries(fields).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => `${k}=${clean(v)}`)].join('\t');
    try {
      fs.mkdirSync(path.dirname(this.auditPath), {recursive: true});
      fs.appendFileSync(this.auditPath, line + '\n', {mode: 0o600});
    } catch (error) { console.error(`[vault] audit write failed: ${error.message}`); }
  }

  async add({name, user = '', password, note = ''}, {source = 'cli'} = {}) {
    assertName(name);
    if (typeof password !== 'string' || !password.trim()) throw new Error('密码为空，没有存');
    const blob = Buffer.from(JSON.stringify({user: String(user ?? ''), password}), 'utf8').toString('base64');
    // `security -i` reads the command from stdin, so the value never appears in argv.
    const result = await this.security({args: ['-i'], input: `add-generic-password -a "${name}" -s "${this.service}" -w ${blob} -U\n`});
    if (result.code !== 0) throw new Error(`钥匙串写入失败：${(result.stderr || result.stdout).trim().slice(0, 200)}`);
    const entries = this.readIndex().filter(e => e.name !== name);
    const previous = this.readIndex().find(e => e.name === name);
    entries.push({name, user: String(user ?? ''), note: String(note ?? '').slice(0, 120), addedAt: previous?.addedAt || this.now().toISOString(),
      updatedAt: this.now().toISOString(), lastUsedAt: previous?.lastUsedAt || null, uses: previous?.uses || 0});
    entries.sort((a, b) => a.name.localeCompare(b.name));
    this.writeIndex(entries);
    this.audit(previous ? 'update' : 'add', name, {source, user: user ? 'yes' : 'no'});
    return {name, updated: Boolean(previous)};
  }

  list() {
    return this.readIndex().map(({name, user, note, addedAt, updatedAt, lastUsedAt, uses}) => ({name, user, note, addedAt, updatedAt, lastUsedAt, uses}));
  }

  async remove(name, {source = 'cli'} = {}) {
    assertName(name);
    const result = await this.security({args: ['delete-generic-password', '-a', name, '-s', this.service]});
    const entries = this.readIndex();
    const known = entries.some(e => e.name === name);
    if (result.code !== 0 && !known) throw new Error(`密码箱里没有「${name}」`);
    this.writeIndex(entries.filter(e => e.name !== name));
    this.audit('rm', name, {source});
    return {name};
  }

  async #read(name) {
    assertName(name);
    const result = await this.security({args: ['find-generic-password', '-a', name, '-s', this.service, '-w']});
    if (result.code !== 0) throw new Error(`密码箱里没有「${name}」`);
    let parsed;
    try { parsed = JSON.parse(Buffer.from(result.stdout.trim(), 'base64').toString('utf8')); } catch { throw new Error(`「${name}」的存储格式不对，请删掉重存`); }
    if (typeof parsed?.password !== 'string') throw new Error(`「${name}」的存储格式不对，请删掉重存`);
    return {user: String(parsed.user ?? ''), password: parsed.password};
  }

  #gate() {
    if (!this.requireForeground) return null;
    const marker = readForeground({dir: this.dir, now: this.now()});
    if (!marker) throw new Error('密码箱只在回复用户的当前对话回合里可用；心跳、定时任务和后台任务不能取用。');
    return marker;
  }

  #touch(name) {
    const entries = this.readIndex();
    const entry = entries.find(e => e.name === name);
    if (entry) { entry.lastUsedAt = this.now().toISOString(); entry.uses = (entry.uses || 0) + 1; this.writeIndex(entries); }
  }

  // into=env: run `command args…` with HOME_AGENT_SECRET_USER / _PASSWORD in its
  // environment. into=browser: fill the fields in an ego-browser task space.
  async use(name, {into, purpose = '', command, args = [], task, passwordField, userField, submit, enter = false, cwd} = {}) {
    assertName(name);
    const marker = this.#gate();
    if (!purpose.trim()) throw new Error('use 需要 --purpose 说明用途（写入审计，不写值）');
    const secret = await this.#read(name);
    const scrub = text => scrubOutput(text, [secret.password]);
    let result;
    if (into === 'env') {
      if (!command) throw new Error('--into env 需要在 -- 后面给出要运行的命令');
      const env = {...process.env, HOME_AGENT_SECRET_NAME: name, HOME_AGENT_SECRET_USER: secret.user, HOME_AGENT_SECRET_PASSWORD: secret.password};
      result = await runProcess(command, args, {env, cwd});
    } else if (into === 'browser') {
      if (!task || !passwordField) throw new Error('--into browser 需要 --task <task space> 和 --password-field <selector>');
      result = await this.browser({script: buildBrowserScript({task, passwordField, userField, submit, enter, user: secret.user, password: secret.password})});
    } else {
      throw new Error('--into 只支持 env 或 browser');
    }
    this.#touch(name);
    this.audit('use', name, {into, turn: marker?.turnId, purpose, exit: result.code});
    return {code: result.code, stdout: scrub(result.stdout), stderr: scrub(result.stderr)};
  }
}

// The script goes to ego-browser over stdin; values are JSON literals inside
// it and never reach argv, the log or the caller.
export function buildBrowserScript({task, passwordField, userField, submit, enter, user, password}) {
  const lines = [`const task = await useOrCreateTaskSpace(${JSON.stringify(task)});`];
  if (userField) {
    if (!user) throw new Error('这条没有存账号，不能填 --user-field');
    lines.push(`await fillInput(${JSON.stringify(userField)}, ${JSON.stringify(user)});`);
  }
  lines.push(`await fillInput(${JSON.stringify(passwordField)}, ${JSON.stringify(password)});`);
  if (submit) lines.push(`await click(${JSON.stringify(submit)}, {label: 'submit login form'});`);
  else if (enter) lines.push(`await pressKey('Enter');`);
  lines.push(`cliLog('filled ' + (${JSON.stringify(Boolean(userField))} ? 'user and password' : 'password') + ' in task space ' + task.id);`);
  return lines.join('\n') + '\n';
}

function parseArgs(argv) {
  const positional = [], options = {};
  let rest = null;
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (rest) { rest.push(arg); continue; }
    if (arg === '--') { rest = []; continue; }
    if (arg.startsWith('--')) {
      const key = arg.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith('--') || key === 'enter') options[key] = true;
      else { options[key] = next; i++; }
    } else positional.push(arg);
  }
  return {positional, options, rest: rest || []};
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return Buffer.concat(chunks).toString('utf8');
}

const USAGE = `用法（没有 get，任何命令都不会打印密码）：
  secret-vault.mjs add <名字> [--user 账号] [--note 备注]        # 密码从 stdin 读入
  secret-vault.mjs list
  secret-vault.mjs use <名字> --purpose "干嘛用" --into env -- <命令> [参数…]
      子进程环境变量：HOME_AGENT_SECRET_USER / HOME_AGENT_SECRET_PASSWORD（输出中的密码会被抹成 [secret]）
  secret-vault.mjs use <名字> --purpose "干嘛用" --into browser --task <task space> --password-field <选择器> [--user-field <选择器>] [--submit <选择器> | --enter]
  secret-vault.mjs rm <名字>`;

async function main(argv) {
  const {positional, options, rest} = parseArgs(argv);
  const [command, name] = positional;
  const vault = new SecretVault();
  switch (command) {
    case 'add': {
      if (!name) throw new Error(USAGE);
      const password = (await readStdin()).replace(/\r?\n$/, '');
      const result = await vault.add({name, user: options.user || '', password, note: options.note || ''});
      console.log(`${result.updated ? '已更新' : '已存入'}：${result.name}${options.user ? `（账号 ${options.user}）` : ''}`);
      return;
    }
    case 'list': {
      const entries = vault.list();
      if (!entries.length) { console.log('密码箱是空的'); return; }
      for (const e of entries) console.log(`${e.name}${e.user ? `\t账号 ${e.user}` : '\t'}\t存于 ${e.addedAt.slice(0, 10)}\t用过 ${e.uses || 0} 次${e.note ? `\t${e.note}` : ''}`);
      return;
    }
    case 'use': {
      if (!name) throw new Error(USAGE);
      const [cmd, ...args] = rest;
      const result = await vault.use(name, {into: options.into, purpose: options.purpose || '', command: cmd, args, task: options.task,
        passwordField: options['password-field'], userField: options['user-field'], submit: options.submit, enter: Boolean(options.enter)});
      if (result.stdout) process.stdout.write(result.stdout);
      if (result.stderr) process.stderr.write(result.stderr);
      process.exitCode = result.code;
      return;
    }
    case 'rm': {
      if (!name) throw new Error(USAGE);
      await vault.remove(name);
      console.log(`已删除：${name}`);
      return;
    }
    case 'get':
      throw new Error('密码箱没有 get：不提供任何打印密码的接口。要用密码请走 use --into env 或 --into browser。');
    default:
      throw new Error(USAGE);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch(error => { console.error(error.message); process.exitCode = 1; });
}
