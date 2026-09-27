import { execFile } from 'node:child_process';
import { access, mkdir } from 'node:fs/promises';
import { constants } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { chromium } from 'playwright';

export const profileDir = process.env.AGENT_BROWSER_PROFILE
  || join(homedir(), 'Library', 'Application Support', 'weixin-agent', 'agent-browser');
export const port = Number(process.env.AGENT_BROWSER_PORT || '9333');
export const host = '127.0.0.1';
export const endpoint = `http://${host}:${port}`;
export const chromeApp = '/Applications/Google Chrome.app';

export function validateWebUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`Invalid URL: ${value}`);
  }
  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new Error(`Blocked URL scheme: ${url.protocol}`);
  }
  if (url.username || url.password) {
    throw new Error('URLs containing embedded credentials are blocked');
  }
  return url;
}

export function safeUrl(value) {
  try {
    const url = new URL(value);
    return `${url.origin}${url.pathname}`;
  } catch {
    return '';
  }
}

export async function pathExists(path) {
  try {
    await access(path, constants.F_OK);
    return true;
  } catch {
    return false;
  }
}

export async function isEndpointReady(timeoutMs = 1000) {
  try {
    const response = await fetch(`${endpoint}/json/version`, {
      signal: AbortSignal.timeout(timeoutMs),
    });
    return response.ok;
  } catch {
    return false;
  }
}

export async function waitForEndpoint(timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await isEndpointReady()) return;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Agent Browser did not expose its local endpoint on ${endpoint}`);
}

function execFilePromise(file, args) {
  return new Promise((resolve, reject) => {
    execFile(file, args, (error) => (error ? reject(error) : resolve()));
  });
}

export async function launchBrowser(url = 'about:blank') {
  if (url !== 'about:blank') validateWebUrl(url);
  await mkdir(profileDir, { recursive: true });
  const args = [
    '-na',
    'Google Chrome',
    '--args',
    `--user-data-dir=${profileDir}`,
    `--remote-debugging-port=${port}`,
    `--remote-debugging-address=${host}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--new-window',
    url,
  ];
  await execFilePromise('/usr/bin/open', args);
  await waitForEndpoint();
}

export async function openVisible(url) {
  validateWebUrl(url);
  if (!(await isEndpointReady())) {
    await launchBrowser(url);
    return { opened: true, reused: false, url: safeUrl(url) };
  }

  const response = await fetch(`${endpoint}/json/new?${encodeURIComponent(url)}`, {
    method: 'PUT',
    signal: AbortSignal.timeout(5000),
  });
  if (!response.ok) throw new Error(`Unable to open Agent Browser tab (${response.status})`);
  return { opened: true, reused: true, url: safeUrl(url) };
}

export async function connectBrowser() {
  if (!(await isEndpointReady())) await launchBrowser('about:blank');
  const browser = await chromium.connectOverCDP(endpoint);
  const context = browser.contexts()[0] || await browser.newContext();
  return { browser, context };
}

export async function newPage(context, targetUrl, waitMs = 1500) {
  const url = validateWebUrl(targetUrl);
  const page = await context.newPage();
  await page.setViewportSize({ width: 1440, height: 1000 }).catch(() => {});
  await page.goto(url.href, { waitUntil: 'domcontentloaded', timeout: 60_000 });
  if (waitMs > 0) await page.waitForTimeout(waitMs);
  return page;
}
