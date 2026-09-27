#!/usr/bin/env node
import { mkdir } from 'node:fs/promises';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import {
  chromeApp,
  connectBrowser,
  endpoint,
  isEndpointReady,
  newPage,
  openVisible,
  pathExists,
  profileDir,
  safeUrl,
  validateWebUrl,
} from './browser-bridge-lib.mjs';

const root = resolve(dirname(new URL(import.meta.url).pathname), '..');
const legacyProfile = join(root, '.browser-profile');

function parseArgs(argv) {
  const parsed = { _: [] };
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (!item.startsWith('--')) {
      parsed._.push(item);
      continue;
    }
    const key = item.slice(2);
    if (['full', 'json'].includes(key)) {
      parsed[key] = true;
      continue;
    }
    if (index + 1 >= argv.length) throw new Error(`Missing value for --${key}`);
    parsed[key] = argv[index + 1];
    index += 1;
  }
  return parsed;
}

function emit(action, data) {
  process.stdout.write(`${JSON.stringify({ ok: true, action, data }, null, 2)}\n`);
}

function usage() {
  return [
    'browser-bridge doctor',
    'browser-bridge login URL',
    'browser-bridge open URL',
    'browser-bridge tabs',
    'browser-bridge read URL [--wait MS] [--max-chars N]',
    'browser-bridge screenshot URL [--out PATH] [--full]',
    'browser-bridge pdf URL [--out PATH]',
    '',
    'Login is always completed by the user in the visible Agent Browser.',
    'The bridge never exports cookies and exposes no arbitrary click/eval command.',
  ].join('\n');
}

async function doctor() {
  const active = await isEndpointReady();
  emit('doctor', {
    chrome_available: await pathExists(chromeApp),
    profile_ready: await pathExists(profileDir),
    profile: profileDir,
    legacy_profile_detected: await pathExists(legacyProfile),
    endpoint: active ? endpoint : 'inactive',
    endpoint_loopback_only: true,
    browser_active: active,
    cookies_exported: false,
    arbitrary_script_exposed: false,
    high_risk_requires_confirmation: true,
  });
}

async function tabs() {
  if (!(await isEndpointReady())) {
    emit('tabs', { browser_active: false, tabs: [] });
    return;
  }
  const { browser, context } = await connectBrowser();
  try {
    const output = [];
    for (const page of context.pages()) {
      output.push({ title: await page.title().catch(() => ''), url: safeUrl(page.url()) });
    }
    emit('tabs', { browser_active: true, tabs: output });
  } finally {
    await browser.close();
  }
}

async function readPage(args) {
  const target = args._[1] || args.url;
  if (!target) throw new Error('read requires a URL');
  validateWebUrl(target);
  const waitMs = Math.max(0, Math.min(Number(args.wait || 1500), 30_000));
  const maxChars = Math.max(500, Math.min(Number(args['max-chars'] || 12_000), 50_000));
  const { browser, context } = await connectBrowser();
  let page;
  try {
    page = await newPage(context, target, waitMs);
    const data = await page.evaluate((limit) => {
      const clone = document.body.cloneNode(true);
      for (const node of clone.querySelectorAll('script, style, noscript, svg, nav, footer, iframe')) node.remove();
      const main = clone.querySelector('article, main, [role="main"], #content, .content') || clone;
      return (main.innerText || main.textContent || '').replace(/\n{3,}/g, '\n\n').trim().slice(0, limit);
    }, maxChars);
    emit('read', {
      title: await page.title(),
      url: safeUrl(page.url()),
      text: data,
      truncated: data.length >= maxChars,
      untrusted_content: true,
    });
  } finally {
    await page?.close().catch(() => {});
    await browser.close();
  }
}

function outputPath(value, fallback) {
  return value ? (isAbsolute(value) ? value : resolve(root, value)) : join(root, 'tmp', fallback);
}

async function capture(args, kind) {
  const target = args._[1] || args.url;
  if (!target) throw new Error(`${kind} requires a URL`);
  validateWebUrl(target);
  const suffix = kind === 'pdf' ? 'pdf' : 'png';
  const file = outputPath(args.out, `browser-${Date.now()}.${suffix}`);
  await mkdir(dirname(file), { recursive: true });
  const { browser, context } = await connectBrowser();
  let page;
  try {
    page = await newPage(context, target, Number(args.wait || 1500));
    if (kind === 'pdf') await page.pdf({ path: file, format: 'A4' });
    else await page.screenshot({ path: file, fullPage: Boolean(args.full) });
    emit(kind, { file, title: await page.title(), url: safeUrl(page.url()) });
  } finally {
    await page?.close().catch(() => {});
    await browser.close();
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const command = args._[0] || 'doctor';
  switch (command) {
    case 'doctor':
    case 'status':
      await doctor();
      break;
    case 'login':
    case 'open': {
      const target = args._[1] || args.url;
      if (!target) throw new Error(`${command} requires a URL`);
      const result = await openVisible(target);
      emit(command, { ...result, manual_login_required: command === 'login' });
      break;
    }
    case 'tabs':
      await tabs();
      break;
    case 'read':
    case 'text':
      await readPage(args);
      break;
    case 'screenshot':
    case 'shot':
      await capture(args, 'screenshot');
      break;
    case 'pdf':
      await capture(args, 'pdf');
      break;
    case 'help':
    case '--help':
    case '-h':
      emit('help', { usage: usage() });
      break;
    default:
      throw new Error(`Unknown command: ${command}\n\n${usage()}`);
  }
}

main().catch((error) => {
  process.stdout.write(`${JSON.stringify({ ok: false, error: error.message }, null, 2)}\n`);
  process.exitCode = 1;
});
