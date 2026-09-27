import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
test('existing teacher and browser knowledge is reachable from natural requests', t => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'browser-routing-'));
  t.after(() => fs.rmSync(tmp, {recursive:true, force:true}));
  fs.mkdirSync(path.join(tmp, 'scripts'));
  for (const file of ['prompt-inject.mjs', 'conversation-context.mjs'])
    fs.copyFileSync(path.join(root, 'scripts', file), path.join(tmp, 'scripts', file));
  fs.cpSync(path.join(root, 'memory', 'skills'), path.join(tmp, 'memory', 'skills'), {recursive:true});
  const run = (...args) => execFileSync(process.execPath, [path.join(tmp, 'scripts/prompt-inject.mjs'), ...args], {encoding:'utf8'});
  run('--rebuild');
  for (const input of ['给我数学老师说一下，我先吃饭，让他等5分钟', '数学那个老师', 'PREPLY 的你去看看', 'Omar 这个', 'omar 这个']) {
    const output = run('--probe', input);
    assert.match(output, /skill: memory\/skills\/preply\.md/, input);
  }
  for (const input of ['ego lite 那个浏览器', 'EGO-BROWSER', 'GUI 的那个'])
    assert.match(run('--probe', input), /skill: memory\/skills\/browser-bridge-cli\.md/, input);
  assert.doesNotMatch(run('--probe', '24度吧'), /skill: memory\/skills\/(preply|browser)/);
});
