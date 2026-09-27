#!/usr/bin/env node
// Browser Bridge 旧命令兼容入口。所有登录态操作都转发到 browser-bridge.mjs，
// 避免多个 Chrome 进程同时打开同一个 profile。
import { execFileSync, spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const bridge = join(root, 'scripts', 'browser-bridge.mjs');
const args = process.argv.slice(2);
const command = args.shift();

function flag(name) {
  const index = args.indexOf(name);
  if (index < 0) return false;
  args.splice(index, 1);
  return true;
}

function option(name, fallback = '') {
  const index = args.indexOf(name);
  if (index < 0) return fallback;
  const value = args[index + 1] || fallback;
  args.splice(index, 2);
  return value;
}

function emit(value, plain, asJson) {
  process.stdout.write(asJson ? `${JSON.stringify(value, null, 2)}\n` : `${plain}\n`);
}

if (!command || !['text', 'shot', 'pdf', 'search', 'help'].includes(command)) {
  process.stderr.write('usage: browser.mjs <text|shot|pdf|search> <url|query> [--out f] [--full] [--wait ms] [--json]\n');
  process.exit(2);
}

const asJson = flag('--json');
const fullPage = flag('--full');
flag('--proxy'); // Historical flag; Agent Chrome follows the Mac network path.
const wait = option('--wait', '1500');
const out = option('--out', '');
const target = args.join(' ').trim();

if (command === 'help') {
  process.stdout.write('Use scripts/browser-bridge.mjs for new integrations.\n');
  process.exit(0);
}
if (!target) {
  process.stderr.write(`${command} requires a URL or query\n`);
  process.exit(2);
}

if (command === 'search') {
  const html = execFileSync('curl', [
    '-s', '--max-time', '20', '-x', 'http://127.0.0.1:7897',
    '-A', 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
    `https://html.duckduckgo.com/html/?q=${encodeURIComponent(target)}`,
  ], { encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 });
  const results = [];
  const pattern = /class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>[\s\S]*?(?:class="result__snippet"[^>]*>([\s\S]*?)<\/a>)?/g;
  const strip = (value) => (value || '').replace(/<[^>]+>/g, '').replace(/&amp;/g, '&')
    .replace(/&#x27;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, ' ').trim();
  let match;
  while ((match = pattern.exec(html)) && results.length < 8) {
    let url = match[1];
    try {
      const parsed = new URL(url, 'https://duckduckgo.com');
      if (parsed.pathname.startsWith('/l/')) url = decodeURIComponent(parsed.searchParams.get('uddg') || url);
    } catch {}
    const title = strip(match[2]);
    if (title && url.startsWith('http')) results.push({ title, url, snippet: strip(match[3]).slice(0, 200) });
  }
  emit(
    { query: target, results },
    results.map((item, index) => `${index + 1}. ${item.title}\n   ${item.url}\n   ${item.snippet}`).join('\n'),
    asJson,
  );
  process.exit(0);
}

const mapped = command === 'text' ? 'read' : command === 'shot' ? 'screenshot' : 'pdf';
const bridgeArgs = [bridge, mapped, target, '--wait', wait];
if (out) bridgeArgs.push('--out', out);
if (fullPage) bridgeArgs.push('--full');
const result = spawnSync(process.execPath, bridgeArgs, { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 });
let payload;
try {
  payload = JSON.parse(result.stdout || '{}');
} catch {
  process.stderr.write(result.stderr || result.stdout || 'Browser Bridge returned invalid JSON\n');
  process.exit(1);
}
if (result.status !== 0 || !payload.ok) {
  process.stderr.write(`${payload.error || result.stderr || 'Browser Bridge failed'}\n`);
  process.exit(result.status || 1);
}

if (mapped === 'read') {
  emit(payload.data, `# ${payload.data.title}\n${payload.data.url}\n\n${payload.data.text}`, asJson);
} else {
  emit({ ok: true, ...payload.data }, payload.data.file, asJson);
}
