#!/usr/bin/env node
// preply-watch.mjs — 盯某个 Preply 老师的对话,她一回消息就微信提醒。
// 复用助理专属浏览器 profile(.browser-profile,已登录 Preply),走 Clash 代理。
//
// 用法:
//   node scripts/preply-watch.mjs baseline --conv 10000001 --name "Tutor A."
//   node scripts/preply-watch.mjs check      # cron 每 15 分钟跑;她回了→推微信+卸 cron
//   node scripts/preply-watch.mjs status
//   node scripts/preply-watch.mjs stop
//
// 状态存 memory/preply-watch.json。触发/过期后自动从 crontab 删除自己。
import fs from "node:fs";
import path from "node:path";
import { execSync } from "node:child_process";
import { chromium } from "playwright";

const ROOT = "/Users/zhen/home-agent/weixin-agent";
const PROFILE = path.join(ROOT, ".browser-profile");
const STATE = path.join(ROOT, "memory", "preply-watch.json");
const NODE = "/opt/homebrew/bin/node";
const CRON_TAG = "preply-watch.mjs check";

const args = process.argv.slice(2);
const cmd = args.shift();
function opt(name, dflt) { const i = args.indexOf(name); if (i >= 0) { const v = args[i + 1]; args.splice(i, 2); return v; } return dflt; }

function loadState() { try { return JSON.parse(fs.readFileSync(STATE, "utf8")); } catch { return null; } }
function saveState(s) { fs.writeFileSync(STATE, JSON.stringify(s, null, 2)); }

async function sendWeChat(text) {
  const mod = await import(path.join(ROOT, "weixin-send.mjs"));
  await mod.sendText(text);
}

function removeCron() {
  try {
    const cur = execSync("crontab -l 2>/dev/null").toString();
    const kept = cur.split("\n").filter((l) => !l.includes(CRON_TAG)).join("\n");
    execSync(`echo ${JSON.stringify(kept.replace(/\n+$/, "") + "\n")} | crontab -`);
  } catch {}
}

async function withPage(fn) {
  const ctx = await chromium.launchPersistentContext(PROFILE, {
    headless: true, viewport: { width: 1280, height: 900 },
    locale: "zh-CN", timezoneId: "Asia/Shanghai",
    proxy: { server: "http://127.0.0.1:7897" },
    userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
  });
  try {
    const page = ctx.pages()[0] ?? await ctx.newPage();
    return await fn(page);
  } finally { await ctx.close(); }
}

// 从消息列表抓某个对话的预览行(最新一条消息的文字)
async function fetchPreview(page, convId) {
  await page.goto("https://preply.com/en/messages", { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(8000);
  return await page.evaluate((cid) => {
    const a = document.querySelector(`a[href*="/messages/${cid}"]`);
    if (!a) return null;
    const t = a.innerText.replace(/\s+/g, " ").trim();
    // 去掉开头的 "名字 时间" 部分不好切;直接返回整行,比较时按整行比
    return t;
  }, convId);
}

// 打开具体对话,抓最后一条消息文字(用于提醒里展示她说了啥)
async function fetchLastMessage(page, convId) {
  await page.goto(`https://preply.com/en/messages/${convId}`, { waitUntil: "domcontentloaded", timeout: 45000 });
  await page.waitForTimeout(7000);
  return await page.evaluate(() => {
    // 消息气泡通常带较长文本,取可见文本节点里最靠后的一段像样的
    const bodyText = document.body.innerText;
    return bodyText.replace(/\s+/g, " ").trim().slice(-400);
  });
}

if (cmd === "baseline") {
  const conv = opt("--conv");
  const name = opt("--name", "老师");
  if (!conv) { console.error("need --conv <id>"); process.exit(2); }
  const preview = await withPage((p) => fetchPreview(p, conv));
  const state = { conv, name, baseline: preview, since: Date.now() };
  saveState(state);
  console.log("baseline saved:\n" + JSON.stringify(state, null, 2));
} else if (cmd === "check") {
  const s = loadState();
  if (!s) { console.log("no active watch"); process.exit(0); }
  // 兜底过期:开始后 18 小时还没回,停掉并告知(避免 cron 长挂)
  if (Date.now() - s.since > 18 * 3600 * 1000) {
    removeCron();
    try { fs.unlinkSync(STATE); } catch {}
    await sendWeChat(`盯了大半天,${s.name} 一直没回你 Preply 的消息,我先撤了。要接着盯再叫我。`);
    console.log("expired");
    process.exit(0);
  }
  const cur = await withPage((p) => fetchPreview(p, s.conv));
  if (cur == null) { console.log("row not found, skip"); process.exit(0); }
  if (cur !== s.baseline) {
    // 预览变了 = 有新消息。抓最后一条文字给用户看
    let last = "";
    try { last = await withPage((p) => fetchLastMessage(p, s.conv)); } catch {}
    removeCron();
    try { fs.unlinkSync(STATE); } catch {}
    const snippet = last ? `\n\n最后一句:\n${last}` : "";
    await sendWeChat(`📩 ${s.name} 回你 Preply 消息了!${snippet}\n\n看这里:https://preply.com/en/messages/${s.conv}`);
    console.log("triggered + notified + cron removed");
  } else {
    console.log("no change");
  }
} else if (cmd === "status") {
  console.log(JSON.stringify(loadState(), null, 2));
} else if (cmd === "stop") {
  removeCron();
  try { fs.unlinkSync(STATE); } catch {}
  console.log("stopped, cron removed");
} else {
  console.error("usage: preply-watch.mjs <baseline|check|status|stop> [--conv id --name name]");
  process.exit(2);
}
