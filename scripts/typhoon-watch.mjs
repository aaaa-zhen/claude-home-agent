#!/usr/bin/env node
// typhoon-watch.mjs — 台风预警监控(珠海视角)。cron 每小时跑,状态变化才推微信。
// 数据源:
//   1. 澳门气象局 SMG 台风信号 XML(珠澳一水之隔,风球=珠海同感,权威且更新快)
//   2. 中央气象台 NMC 台风路径 JSON(活跃台风位置/强度/预报路径,算到珠海距离)
// 推送时机:风球升/降/除下;台风进入 800km 圈或强度变化;预报路径将进 300km。
// 用法: node scripts/typhoon-watch.mjs [--dry-run]   # --dry-run 打印不推送、不写状态
import fs from "node:fs";
import { sendText } from "/Users/zhen/home-agent/weixin-agent/weixin-send.mjs";

const ROOT = "/Users/zhen/home-agent/weixin-agent";
const STATE_PATH = `${ROOT}/tmp/typhoon-watch-state.json`;
const ZH = { lat: 22.27, lng: 113.57 }; // 珠海
const NEAR_KM = 800;   // 现实距离关注圈
const CLOSE_KM = 300;  // 预报路径警戒圈
const dryRun = process.argv.includes("--dry-run");

const LEVELS = { TD: "热带低压", TS: "热带风暴", STS: "强热带风暴", TY: "台风", STY: "强台风", SuperTY: "超强台风" };

function dist(lat1, lng1, lat2, lng2) {
  const R = 6371, toR = (d) => (d * Math.PI) / 180;
  const dLat = toR(lat2 - lat1), dLng = toR(lng2 - lng1);
  const a = Math.sin(dLat / 2) ** 2 + Math.cos(toR(lat1)) * Math.cos(toR(lat2)) * Math.sin(dLng / 2) ** 2;
  return Math.round(R * 2 * Math.asin(Math.sqrt(a)));
}

async function fetchText(url, opts = {}) {
  const res = await fetch(url, { signal: AbortSignal.timeout(20000), headers: { "User-Agent": "Mozilla/5.0" }, ...opts });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`);
  return res.text();
}
const jsonp = (t) => JSON.parse(t.slice(t.indexOf("(") + 1, t.lastIndexOf(")")).replace(/^\(/, "").replace(/\)$/, ""));

// ---- 1. 澳门风球信号 ----
let signal = "";
try {
  const xml = await fetchText("https://xml.smg.gov.mo/c_typhoon.xml");
  signal = (xml.match(/<TcSignal>([^<]*)<\/TcSignal>/) || [])[1]?.trim() ?? "";
} catch (e) { console.error(`SMG failed: ${e.message}`); signal = null; } // null=获取失败,别当"除下"

// ---- 2. NMC 活跃台风 ----
const typhoons = [];
try {
  const list = jsonp(await fetchText(`http://typhoon.nmc.cn/weatherservice/typhoon/jsons/list_default?t=${Date.now()}`)).typhoonList;
  for (const t of list) {
    if (t[7] !== "start") continue;
    const [id, en, cn] = t;
    try {
      const detail = jsonp(await fetchText(`http://typhoon.nmc.cn/weatherservice/typhoon/jsons/view_${id}?t=${Date.now()}`)).typhoon;
      const track = detail[8];
      if (!track?.length) continue;
      const last = track[track.length - 1];
      const [lng, lat, pressure, wind] = [last[4], last[5], last[6], last[7]];
      const level = LEVELS[last[3]] ?? last[3];
      const d = dist(ZH.lat, ZH.lng, lat, lng);
      let minForecast = d;
      for (const f of last[10]?.BABJ ?? []) minForecast = Math.min(minForecast, dist(ZH.lat, ZH.lng, f[3], f[2]));
      typhoons.push({ id, name: cn || en, level, levelCode: last[3], wind, pressure, distKm: d, minForecastKm: minForecast });
    } catch { /* 单个台风详情失败跳过 */ }
  }
} catch (e) { console.error(`NMC failed: ${e.message}`); }

// ---- 3. 状态对比,决定推不推 ----
let state = {}; try { state = JSON.parse(fs.readFileSync(STATE_PATH, "utf8")); } catch {}
const prevSignal = state.signal ?? "";
const prevTy = state.typhoons ?? {};
const lines = [];
let push = false;

if (signal !== null && signal !== prevSignal) {
  push = true;
  if (signal) lines.push(`⚠️ 澳门已${prevSignal ? "改挂" : "悬挂"}${signal}(珠澳同步,珠海注意)`);
  else lines.push(`✅ 澳门风球已除下`);
}

const near = typhoons.filter((t) => t.distKm <= NEAR_KM || t.minForecastKm <= CLOSE_KM);
for (const t of near) {
  const prev = prevTy[t.id];
  const bucket = Math.ceil(t.distKm / 100); // 每 100km 一档,进档才再推
  if (!prev || prev.levelCode !== t.levelCode || bucket < prev.bucket) push = true;
  const move = t.minForecastKm < t.distKm ? `,预报将逼近至约 ${t.minForecastKm}km` : "";
  lines.push(`🌀 ${t.level}「${t.name}」距珠海约 ${t.distKm}km,中心风力 ${t.wind}m/s${move}`);
}
// 之前在圈内、现在走了/消失 → 解除通报
for (const id of Object.keys(prevTy)) {
  if (!near.some((t) => String(t.id) === String(id))) {
    push = true;
    lines.push(`✅ 台风「${prevTy[id].name}」已远离/停编`);
  }
}

const nextState = {
  signal: signal === null ? prevSignal : signal,
  typhoons: Object.fromEntries(near.map((t) => [t.id, { name: t.name, levelCode: t.levelCode, bucket: Math.ceil(t.distKm / 100) }])),
  checkedAt: new Date().toISOString(),
};

if (!lines.length) { console.log("all clear, nothing to report"); }
if (dryRun) {
  console.log(`[dry-run] push=${push}\n${lines.join("\n")}`);
} else {
  if (push && lines.length) {
    await sendText(`🌪 台风动态\n${lines.join("\n")}`);
    fs.appendFileSync(`${ROOT}/memory/recent-context.md`,
      `[${new Date().toLocaleString("sv-SE", { timeZone: "Asia/Shanghai" }).replace("T", " ")}] [typhoon-watch] 已推送台风预警: ${lines.join("; ").slice(0, 150)}\n`);
    console.log("pushed");
  }
  fs.writeFileSync(STATE_PATH, JSON.stringify(nextState, null, 2) + "\n");
}
