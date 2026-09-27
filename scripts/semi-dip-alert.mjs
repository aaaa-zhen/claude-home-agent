import { sendText } from '/Users/zhen/home-agent/weixin-agent/weixin-send.mjs';
import fs from 'node:fs';

const FUND = '008887';
const FUND_NAME = '华夏国证半导体芯片ETF联接A';
const DAILY_DROP_THRESHOLD = -3.0;
const DRAWDOWN_THRESHOLD = -8.0;
const MEMORY_DAILY_DROP_THRESHOLD = -4.0;
const MEMORY_DRAWDOWN_THRESHOLD = -10.0;
const STATE_PATH = '/Users/zhen/home-agent/weixin-agent/tmp/semi-dip-alert-state.json';
const MEMORY_STOCKS = [
  { name: '兆易创新', secid: '1.603986' },
  { name: '澜起科技', secid: '1.688008' },
  { name: '江波龙', secid: '0.301308' },
  { name: '佰维存储', secid: '1.688525' },
  { name: '德明利', secid: '0.001309' },
  { name: '普冉股份', secid: '1.688766' },
  { name: '东芯股份', secid: '1.688110' },
  { name: '北京君正', secid: '0.300223' },
];

function today() {
  const d = new Date();
  const z = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`;
}

function readState() {
  try {
    return JSON.parse(fs.readFileSync(STATE_PATH, 'utf8'));
  } catch {
    return {};
  }
}

function writeState(state) {
  fs.writeFileSync(STATE_PATH, `${JSON.stringify(state, null, 2)}\n`);
}

async function fetchMemoryBasket() {
  const fields = 'f12,f14,f2,f3,f4,f124';
  const secids = MEMORY_STOCKS.map((s) => s.secid).join(',');
  const url = `https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=${secids}&fields=${fields}`;
  const res = await fetch(url);
  const json = await res.json();
  const rows = json?.data?.diff || [];
  const stocks = rows.map((row) => ({
    code: row.f12,
    name: row.f14,
    price: Number(row.f2),
    pct: Number(row.f3),
    time: row.f124 ? new Date(row.f124 * 1000) : new Date(),
  })).filter((row) => Number.isFinite(row.price) && Number.isFinite(row.pct));
  if (!stocks.length) return null;
  const avgPct = stocks.reduce((sum, row) => sum + row.pct, 0) / stocks.length;
  const worst = [...stocks].sort((a, b) => a.pct - b.pct).slice(0, 3);
  return { stocks, avgPct, worst };
}

const rt = Date.now();
const res = await fetch(`http://fundgz.1234567.com.cn/js/${FUND}.js?rt=${rt}`, {
  headers: { Referer: 'http://fund.eastmoney.com/' },
});
const text = await res.text();
const m = text.match(/jsonpgz\((.*)\)/);
if (!m) process.exit(0);

const data = JSON.parse(m[1]);
const pct = parseFloat(data.gszzl); // 当日估算涨跌幅 %
const estimatedNav = parseFloat(data.gsz);
const gzDate = (data.gztime || '').slice(0, 10);
const state = readState();
const memory = await fetchMemoryBasket().catch(() => null);

if (Number.isFinite(estimatedNav)) {
  if (!Number.isFinite(state.highNav) || estimatedNav > state.highNav) {
    state.highNav = estimatedNav;
    state.highAt = data.gztime;
  }
  if (!Number.isFinite(state.baselineNav)) {
    state.baselineNav = estimatedNav;
    state.baselineAt = data.gztime;
  }
}

const drawdown = Number.isFinite(state.highNav) && Number.isFinite(estimatedNav)
  ? ((estimatedNav / state.highNav) - 1) * 100
  : 0;
const alertKey = `${gzDate}:${pct.toFixed(2)}:${drawdown.toFixed(2)}`;
const shouldAlert = gzDate === today()
  && Number.isFinite(pct)
  && (
    pct <= DAILY_DROP_THRESHOLD
    || drawdown <= DRAWDOWN_THRESHOLD
  )
  && state.lastAlertKey !== alertKey;

let memoryDrawdown = 0;
let shouldMemoryAlert = false;
let memoryAlertKey = '';
if (memory) {
  const currentBasket = Number.isFinite(state.memoryBasket)
    ? state.memoryBasket * (1 + memory.avgPct / 100)
    : 100;
  if (!Number.isFinite(state.highMemoryBasket) || currentBasket > state.highMemoryBasket) {
    state.highMemoryBasket = currentBasket;
    state.highMemoryAt = data.gztime;
  }
  if (!Number.isFinite(state.memoryBasket)) {
    state.memoryBasket = currentBasket;
    state.memoryBasketAt = data.gztime;
  } else {
    state.memoryBasket = currentBasket;
    state.memoryBasketAt = data.gztime;
  }
  memoryDrawdown = Number.isFinite(state.highMemoryBasket)
    ? ((currentBasket / state.highMemoryBasket) - 1) * 100
    : 0;
  memoryAlertKey = `${gzDate}:memory:${memory.avgPct.toFixed(2)}:${memoryDrawdown.toFixed(2)}`;
  shouldMemoryAlert = gzDate === today()
    && (
      memory.avgPct <= MEMORY_DAILY_DROP_THRESHOLD
      || memoryDrawdown <= MEMORY_DRAWDOWN_THRESHOLD
      || memory.worst.some((row) => row.pct <= -7)
    )
    && state.lastMemoryAlertKey !== memoryAlertKey;
}

if (shouldAlert) {
  const msg =
    `半导体基金回调提醒\n\n` +
    `${FUND_NAME}（${FUND}）\n` +
    `今日估算涨跌：${pct.toFixed(2)}%\n` +
    `估算净值：${data.gsz}，更新于 ${data.gztime}\n` +
    `相对观察高点回撤：${drawdown.toFixed(2)}%\n\n` +
    `你之前说等下跌再考虑买回，可以看看是否分批进场。15:00 前下单通常按今天净值。`;
  await sendText(msg);
  state.lastAlertKey = alertKey;
  state.lastAlertAt = new Date().toISOString();
}

if (shouldMemoryAlert) {
  const worstText = memory.worst
    .map((row) => `${row.name} ${row.pct.toFixed(2)}%`)
    .join('，');
  const msg =
    `内存/存储链回调提醒\n\n` +
    `观察组合平均涨跌：${memory.avgPct.toFixed(2)}%\n` +
    `相对观察高点回撤：${memoryDrawdown.toFixed(2)}%\n` +
    `跌幅靠前：${worstText}\n\n` +
    `你说不只看 008887，也要看内存方向。现在存储链有明显回调，可以看看 008887、018411、019632 是否适合分批。`;
  await sendText(msg);
  state.lastMemoryAlertKey = memoryAlertKey;
  state.lastMemoryAlertAt = new Date().toISOString();
}

writeState(state);
