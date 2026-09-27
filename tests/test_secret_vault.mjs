import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {detectSecret, proposeName} from '../scripts/secret-detect.mjs';
import {SecretVault, scrubOutput, buildBrowserScript, markForeground, clearForeground, readForeground, assertName} from '../scripts/secret-vault.mjs';
import {redact} from '../scripts/conversation-context.mjs';
import {patchSecretRedaction} from '../patches/patch-secret-redaction.mjs';

test('detect: the way the user actually types it, with and without separators', () => {
  for (const text of ['京东 账号 abc123 密码 Pa55word', '京东账号abc123密码Pa55word', '京东 账号：abc123 密码：Pa55word', '京东，账号是abc123，密码是Pa55word。']) {
    const hit = detectSecret(text);
    assert.ok(hit, text);
    assert.equal(hit.password, 'Pa55word');
    assert.equal(hit.user, 'abc123');
    assert.equal(hit.name, '京东');
    assert.ok(!hit.redactedText.includes('Pa55word'));
    assert.ok(hit.redactedText.includes('[已存入密码箱：京东]'));
    assert.ok(hit.redactedText.includes('abc123'), 'account stays visible for context');
  }
});

test('detect: password only, English keywords, request text around it', () => {
  const hit = detectSecret('帮我记一下 路由器 密码 admin2024');
  assert.equal(hit.name, '路由器'); assert.equal(hit.user, ''); assert.equal(hit.password, 'admin2024');
  const en = detectSecret('github username zhen password gh_Secret_9 then check my notifications');
  assert.equal(en.user, 'zhen'); assert.equal(en.password, 'gh_Secret_9'); assert.equal(en.name, 'github');
  assert.equal(en.redactedText, 'github username zhen password [已存入密码箱：github] then check my notifications');
});

test('detect: conversation about passwords is not a credential', () => {
  for (const text of ['密码错了', '密码是什么', '我忘了密码', '密码忘了怎么办', '把密码改一下', '密码箱里有什么', '把京东的密码删掉', 'passwordless login', '[已存入密码箱：京东] 存好了吗', '密码 [已移入密码箱：路由器]',
    '密码 主卧空调25度就行', 'token 存在 .env 里', 'token>abcdefghijklmnop', '- password -field 的选择器', 'curl -u "$HOME_AGENT_SECRET_USER:$HOME_AGENT_SECRET_PASSWORD" https://x', '密码 /compact 保留话题', '账号 abc 密码 <密码>', '重启代理让它读新 token launchctl kickstart', 'XX网站 账号 abc 密码 123']) {
    assert.equal(detectSecret(text), null, text);
  }
});

test('detect: values wrapped in backticks or quotes are still credentials', () => {
  assert.equal(detectSecret('notion token ntn_12345678abcdef').password, 'ntn_12345678abcdef');
  assert.equal(detectSecret('健康160 密码 `Hosp2024ok`').password, 'Hosp2024ok');
  assert.equal(detectSecret('wifi 密码 "home-2024"').password, 'home-2024');
  const h = detectSecret('- 健康160 账号：手机号 `15500000000`，临时密码 `Temp2024ok`（会改）');
  assert.equal(h.user, '15500000000'); assert.equal(h.password, 'Temp2024ok'); assert.equal(h.name, '健康160');
});

test('detect: name falls back to account or a timestamp', () => {
  const now = new Date('2026-09-22T12:34:00');
  assert.equal(detectSecret('账号 someone@x.com 密码 abcd1234', {now}).name, 'someone@x.com');
  assert.equal(detectSecret('密码 abcd1234', {now}).name, 'secret-20260922-1234');
  assert.equal(proposeName('帮我记住 珠海人民医院 的 账号', 99, '', now), '珠海人民医院');
});

test('redact: widened regex covers「密码 123」and leaves placeholders and chatter alone', () => {
  assert.equal(redact('京东 密码 Pa55word 记一下'), '京东 密码 [redacted] 记一下');
  assert.equal(redact('密码：Pa55word'), '密码：[redacted]');
  assert.equal(redact('password=Pa55word'), 'password=[redacted]');
  assert.equal(redact('密码错了'), '密码错了');
  assert.equal(redact('密码 [已存入密码箱：github]'), '密码 [已存入密码箱：github]');
  assert.equal(redact('密码箱里有什么'), '密码箱里有什么');
  assert.equal(redact('Bearer abc.def-ghi'), 'Bearer [redacted]');
});

function fakeSecurity(store) {
  return async ({args, input}) => {
    if (args[0] === '-i') {
      const m = /add-generic-password -a "([^"]+)" -s "([^"]+)" -w (\S+) -U/.exec(input);
      store.set(`${m[2]}/${m[1]}`, m[3]);
      return {code: 0, stdout: '', stderr: ''};
    }
    const name = args[args.indexOf('-a') + 1], service = args[args.indexOf('-s') + 1];
    if (args[0] === 'find-generic-password') return store.has(`${service}/${name}`) ? {code: 0, stdout: store.get(`${service}/${name}`) + '\n', stderr: ''} : {code: 44, stdout: '', stderr: 'not found'};
    if (args[0] === 'delete-generic-password') { const had = store.delete(`${service}/${name}`); return {code: had ? 0 : 44, stdout: '', stderr: had ? '' : 'not found'}; }
    return {code: 1, stdout: '', stderr: 'unexpected'};
  };
}

function vaultFixture(t, options = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vault-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  const store = new Map(), scripts = [];
  const vault = new SecretVault({service: 'test-vault', dir, auditPath: path.join(dir, 'audit.log'), security: fakeSecurity(store),
    browser: async ({script}) => { scripts.push(script); return {code: 0, stdout: 'filled\n', stderr: ''}; }, ...options});
  return {vault, dir, store, scripts, audit: () => fs.readFileSync(path.join(dir, 'audit.log'), 'utf8')};
}

test('vault: add stores base64 JSON via security -i, list never shows the value, rm removes', async t => {
  const f = vaultFixture(t);
  await f.vault.add({name: '京东', user: 'abc123', password: 'Pa55word'}, {source: 'wechat:turn-1'});
  assert.equal(f.store.size, 1);
  const stored = Buffer.from([...f.store.values()][0], 'base64').toString('utf8');
  assert.deepEqual(JSON.parse(stored), {user: 'abc123', password: 'Pa55word'});
  const listed = f.vault.list();
  assert.equal(listed.length, 1); assert.equal(listed[0].name, '京东'); assert.equal(listed[0].user, 'abc123');
  assert.ok(!JSON.stringify(listed).includes('Pa55word'));
  assert.match(f.audit(), /\tadd\t京东\tsource=wechat:turn-1/);
  assert.ok(!f.audit().includes('Pa55word'));
  await f.vault.remove('京东');
  assert.equal(f.store.size, 0); assert.equal(f.vault.list().length, 0);
  await assert.rejects(f.vault.remove('京东'), /没有/);
});

test('vault: names are validated and there is no way to read a value back', async t => {
  const f = vaultFixture(t);
  assert.throws(() => assertName('a; rm -rf /'), /不合法/);
  assert.throws(() => assertName(''), /不合法/);
  await assert.rejects(f.vault.add({name: 'x', password: '   '}), /密码为空/);
  assert.equal(typeof f.vault.get, 'undefined');
});

test('vault: use refuses outside a foreground turn and needs a purpose', async t => {
  const f = vaultFixture(t);
  await f.vault.add({name: 'github', user: 'zhen', password: 'gh_Secret_9'});
  await assert.rejects(f.vault.use('github', {into: 'env', purpose: 'x', command: 'true'}), /当前对话回合/);
  markForeground('turn-9', {dir: f.dir, now: new Date(Date.now() - 30 * 60 * 1000)});
  await assert.rejects(f.vault.use('github', {into: 'env', purpose: 'x', command: 'true'}), /当前对话回合/);
  markForeground('turn-9', {dir: f.dir});
  await assert.rejects(f.vault.use('github', {into: 'env', command: 'true'}), /purpose/);
  clearForeground({dir: f.dir});
  assert.equal(readForeground({dir: f.dir}), null);
});

test('vault: env injection hands the value to the child and scrubs it from the output', async t => {
  const f = vaultFixture(t);
  await f.vault.add({name: 'github', user: 'zhen', password: 'gh_Secret_9'});
  markForeground('turn-9', {dir: f.dir});
  const r = await f.vault.use('github', {into: 'env', purpose: '测试注入', command: 'sh', args: ['-c', 'echo "user=$HOME_AGENT_SECRET_USER pass=$HOME_AGENT_SECRET_PASSWORD"; printf "%s" "$HOME_AGENT_SECRET_PASSWORD" | base64; exit 3']});
  assert.equal(r.code, 3);
  assert.equal(r.stdout, 'user=zhen pass=[secret]\n[secret]\n');
  assert.match(f.audit(), /\tuse\tgithub\tinto=env\tturn=turn-9\tpurpose=测试注入\texit=3/);
  assert.equal(f.vault.list()[0].uses, 1);
});

test('vault: browser injection builds a stdin script with literal values and reports without them', async t => {
  const f = vaultFixture(t);
  await f.vault.add({name: 'preply', user: 'me@x.com', password: 'p"w\'d\\9'});
  markForeground('turn-2', {dir: f.dir});
  const r = await f.vault.use('preply', {into: 'browser', purpose: '登录 Preply', task: 'preply login', passwordField: 'input[type="password"]', userField: '#email', submit: 'button[type="submit"]'});
  assert.equal(r.code, 0);
  assert.equal(f.scripts.length, 1);
  assert.match(f.scripts[0], /useOrCreateTaskSpace\("preply login"\)/);
  assert.match(f.scripts[0], /fillInput\("#email", "me@x.com"\)/);
  assert.ok(f.scripts[0].includes('fillInput("input[type=\\"password\\"]", "p\\"w\'d\\\\9")'));
  assert.match(f.scripts[0], /click\("button\[type=\\"submit\\"\]"/);
  await assert.rejects(f.vault.use('preply', {into: 'browser', purpose: 'x', task: 't'}), /password-field/);
  assert.throws(() => buildBrowserScript({task: 't', passwordField: '#p', userField: '#u', user: '', password: 'x'}), /没有存账号/);
  assert.match(buildBrowserScript({task: 't', passwordField: '#p', enter: true, user: '', password: 'x'}), /pressKey\('Enter'\)/);
});

test('scrubOutput covers raw, url-encoded, json-escaped and base64 forms', () => {
  const secret = 'p@ss word"1';
  const text = [secret, encodeURIComponent(secret), JSON.stringify(secret), Buffer.from(secret).toString('base64')].join('|');
  assert.equal(scrubOutput(text, [secret]), '[secret]|[secret]|"[secret]"|[secret]');
  assert.equal(scrubOutput('ab', ['ab']), 'ab', 'too short to scrub, avoids shredding normal output');
});

test('SDK patch scrubs the turn-memory line and the inbound log line, and is idempotent', () => {
  const source = 'head\n\t\tconst user = safeTurnMemoryText(userText, 220) || "(媒体消息)";\n\tlogger.info(`[weixin-msg] start requestId=${requestId} text=${JSON.stringify(textBody.slice(0, 120))}`);\n';
  const patched = patchSecretRedaction(source, '/root');
  assert.match(patched, /import \{redact as redactSecrets\} from "\/root\/scripts\/conversation-context.mjs"/);
  assert.match(patched, /safeTurnMemoryText\(redactSecrets\(userText\), 220\)/);
  assert.match(patched, /JSON\.stringify\(redactSecrets\(textBody\)\.slice\(0, 120\)\)/);
  assert.equal(patchSecretRedaction(patched, '/root'), patched);
  assert.throws(() => patchSecretRedaction('nothing here', '/root'), /Unsupported/);
});
