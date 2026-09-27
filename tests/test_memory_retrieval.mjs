import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'memory-retrieval-'));
  t.after(() => fs.rmSync(dir, {recursive:true, force:true}));
  fs.mkdirSync(path.join(dir, 'scripts'));
  for (const name of ['prompt-inject.mjs', 'conversation-context.mjs'])
    fs.copyFileSync(path.join(root, 'scripts', name), path.join(dir, 'scripts', name));
  const write = (name, text) => {
    const p = path.join(dir, 'memory', name); fs.mkdirSync(path.dirname(p), {recursive:true}); fs.writeFileSync(p, text);
  };
  const run = (...args) => execFileSync(process.execPath, [path.join(dir, 'scripts/prompt-inject.mjs'), ...args], {encoding:'utf8'});
  const probe = input => run('--probe', input);
  const hits = input => probe(input).split('\n').filter(s => s.startsWith('{"source":')).map(s => JSON.parse(s));
  return {dir, write, run, probe, hits};
}

test('short personal questions retrieve facts buried in long files without keyword routes', t => {
  const f = fixture(t);
  f.write('user-profile.md', '# 用户画像\n\n## 日常\n' + '平时散步。'.repeat(6000) +
    '\n\n## 课程与老师\n- 数学老师是 Nadia，英语老师是 Iris。\n');
  assert.ok(f.hits('我有几个老师？').some(x => x.excerpt.includes('Nadia')));
  assert.ok(f.hits('我的数学老师是谁').some(x => x.excerpt.includes('Nadia')));
});

test('pending, superseded, historical and placeholder text is not current evidence', t => {
  const f = fixture(t);
  f.write('learned-facts.md', '# 知识\n- [2026-09-13] {f0001 user high} 数学老师是 Nadia。\n' +
    '- [2026-09-01] {f0002 infer low} 数学老师是 UNVERIFIED。\n' +
    '- [2026-08-01] {f0003 user high superseded→f0001} 数学老师是 OLD。\n');
  for (const name of ['learned-facts-pending.md','learned-facts-archive.md','session-handoff.md','conversation-summary.md','daily/2026-09-12.md'])
    f.write(name, '# 数学老师\n数学老师是 UNVERIFIED。');
  f.write('user-profile.md', '# 画像\n## 待补充信息\n数学老师是 WHO_KNOWS。\n');
  const out = f.probe('数学老师是谁');
  assert.match(out, /Nadia/); assert.doesNotMatch(out, /UNVERIFIED|WHO_KNOWS|数学老师是 OLD/);
});

test('new, changed and removed memories are reflected without manual rebuild', t => {
  const f = fixture(t);
  f.write('people.md', '# 同事\n- 同事 Rena 在 Sunnyvale。\n');
  assert.match(f.probe('Rena 在哪'), /Sunnyvale/);
  f.write('people.md', '# 同事\n- 同事 Rena 目前搬到 Tokyo。\n');
  const changed = f.probe('Rena 在哪'); assert.match(changed, /Tokyo/); assert.doesNotMatch(changed, /Sunnyvale/);
  f.write('reading-list.md', '# 读书清单\n## 想读\nAntifragile（反脆弱）。\n');
  assert.match(f.probe('我之前想读什么书'), /Antifragile/);
  fs.unlinkSync(path.join(f.dir,'memory/people.md'));
  assert.doesNotMatch(f.probe('Rena 在哪'), /Tokyo/);
});

test('scalar followups and social filler do not pull unrelated memory', t => {
  const f = fixture(t);
  f.write('skills/weather.md', '# 天气\n今天几度、明天几度、今晚下雨吗。\n');
  f.write('skills/preply.md', '# 老师\n数学老师 Nadia。\n');
  for (const q of ['24度吧','可以啊','今晚吃什么']) assert.equal(f.hits(q).length,0,q);
});

test('snippets carry source positions, are bounded, and redact credentials', t => {
  const f = fixture(t);
  f.write('people.md', '# 联系人\n\n## Rena\nRena 负责合作。\npassword: private-secret\n');
  const hits=f.hits('Rena'); assert.ok(hits.length); assert.equal(hits[0].source,'memory/people.md');
  assert.equal(hits[0].line,4); assert.ok(hits[0].excerpt.length <= 900);
  assert.doesNotMatch(JSON.stringify(hits),/private-secret/);
  assert.equal(fs.statSync(path.join(f.dir,'runtime/prompt-inject/index.json')).mode & 0o777,0o600);
});

test('current real teacher and book queries have factual excerpts after a fresh start', t => {
  const f=fixture(t);
  for (const name of ['user-profile.md','reading-list.md','skills/preply.md'])
    f.write(name,fs.readFileSync(path.join(root,'memory',name),'utf8'));
  for (const q of ['我有几个老师？','上课的都有谁','我的课程都是谁在教？','数学那个老师','Omar 这个'])
    assert.match(f.probe(q),/Omar/,q);
  assert.match(f.probe('我之前想读什么书'),/Antifragile/);
  assert.match(f.probe('我现在在读哪本书'),/First 50 Years/);
  const combined=f.probe('数学老师是谁；我有几个老师；我之前想读什么书；Preply 浏览器是否能点击和发消息');
  assert.match(combined,/Omar/);
  assert.match(combined,/Antifragile/);
});
