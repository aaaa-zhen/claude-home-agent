#!/usr/bin/env node
// market-brief.mjs — 盘后简报:三大指数 + 存储链自选股 + 半导体基金估值,一条微信。
// cron 工作日 15:10 触发;节假日靠"行情日期≠今天"自动跳过。
// 自选股清单与 scripts/semi-dip-alert.mjs 保持一致(用户关注内存/存储链方向)。
import { execFileSync } from "node:child_process";
import { sendText } from "/Users/zhen/home-agent/weixin-agent/weixin-send.mjs";

const ROOT = "/Users/zhen/home-agent/weixin-agent";
const FUND = { code: "008887", name: "华夏半导体芯片ETF联接A" };
const STOCKS = [
  { name: "兆易创新", secid: "1.603986" },
  { name: "澜起科技", secid: "1.688008" },
  { name: "江波龙", secid: "0.301308" },
  { name: "佰维存储", secid: "1.688525" },
  { name: "德明利", secid: "0.001309" },
  { name: "普冉股份", secid: "1.688766" },
  { name: "东芯股份", secid: "1.688110" },
  { name: "北京君正", secid: "0.300223" },
];

const dryRun = process.argv.includes("--dry-run");
const sign = (n) => (n > 0 ? `+${n.toFixed(2)}` : n.toFixed(2));

// 1. 三大指数(腾讯行情,via tools/info/stock.py)
const indices = JSON.parse(execFileSync("python3", [`${ROOT}/tools/info/stock.py`, "--json"], { encoding: "utf8" }));

// 节假日/停牌保护:行情日期不是今天就不发
const today = new Date();
const z = (n) => String(n).padStart(2, "0");
const todayStr = `${today.getFullYear()}${z(today.getMonth() + 1)}${z(today.getDate())}`;
if (!indices.some((i) => (i.time || "").startsWith(todayStr))) {
  console.log(`skip: no trading data for ${todayStr} (holiday?)`);
  process.exit(0);
}

// 2. 自选股(东方财富批量)
let stockLines = [];
try {
  const secids = STOCKS.map((s) => s.secid).join(",");
  const url = `https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=${secids}&fields=f12,f14,f2,f3`;
  const json = await (await fetch(url, { signal: AbortSignal.timeout(15000) })).json();
  const rows = (json?.data?.diff || [])
    .map((r) => ({ name: r.f14, price: Number(r.f2), pct: Number(r.f3) }))
    .filter((r) => Number.isFinite(r.pct))
    .sort((a, b) => b.pct - a.pct);
  stockLines = rows.map((r) => `${r.pct >= 0 ? "🔴" : "🟢"} ${r.name} ${sign(r.pct)}%`);
} catch (e) {
  stockLines = [`(自选股行情获取失败: ${e.message})`];
}

// 3. 基金估值(天天基金)
let fundLine = "";
try {
  const txt = await (await fetch(`http://fundgz.1234567.com.cn/js/${FUND.code}.js?rt=${Date.now()}`, {
    headers: { Referer: "http://fund.eastmoney.com/" }, signal: AbortSignal.timeout(15000),
  })).text();
  const m = txt.match(/jsonpgz\((.*)\)/);
  if (m) {
    const d = JSON.parse(m[1]);
    fundLine = `${FUND.name} 估值 ${sign(parseFloat(d.gszzl))}%（${d.gsz}）`;
  }
} catch { /* 基金估值失败不阻塞简报 */ }

const idxText = indices
  .map((i) => `${i.pct >= 0 ? "🔴" : "🟢"} ${i.name.replace("指数", "").replace("成指", "").replace("指", "")} ${i.price} ${sign(i.pct)}%`)
  .join("\n");

const msg = `📊 盘后简报 ${today.getFullYear()}-${z(today.getMonth() + 1)}-${z(today.getDate())}
${idxText}

存储链自选：
${stockLines.join("\n")}
${fundLine ? `\n${fundLine}` : ""}`;

if (dryRun) {
  console.log(msg);
} else {
  await sendText(msg);
  console.log("sent");
}
