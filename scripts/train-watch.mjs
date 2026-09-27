#!/usr/bin/env node
// train-watch.mjs — 12306 余票监控:登记想要的车票,cron 定时查,有票立刻推微信。
// 数据链路复用 train_query.sh(HA 盒子代理 12306,国内网络)。
//
// 用法(agent 在用户说"帮我盯着X号去XX的票"时调用 add):
//   node scripts/train-watch.mjs add --from 珠海 --to 郑州东 --date 2026-07-10 [--trains G96,G546] [--seat 二等座,硬卧]
//   node scripts/train-watch.mjs list
//   node scripts/train-watch.mjs remove --id <id>
//   node scripts/train-watch.mjs check          # cron 入口;有票→推送并自动停止该监控
import fs from "node:fs";
import { execFileSync } from "node:child_process";
import { sendText } from "/Users/zhen/home-agent/weixin-agent/weixin-send.mjs";

const ROOT = "/Users/zhen/home-agent/weixin-agent";
const STORE = `${ROOT}/memory/train-watches.json`;

const SEAT_FIELDS = {
  "商务座": "business_seat", "一等座": "first_seat", "二等座": "second_seat",
  "软卧": "soft_sleeper", "硬卧": "hard_sleeper", "硬座": "hard_seat", "无座": "no_seat",
};

const args = process.argv.slice(2);
const cmd = args.shift();
function opt(name, dflt) { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : dflt; }

const load = () => { try { return JSON.parse(fs.readFileSync(STORE, "utf8")); } catch { return []; } };
const save = (w) => fs.writeFileSync(STORE, JSON.stringify(w, null, 2) + "\n");
const hasSeat = (v) => v === "有" || (/^\d+$/.test(v ?? "") && Number(v) > 0);

// cron 自管理:有活跃监控才轮询,全部结束自动卸载(用户要求平时零后台轮询)
const CRON_LINE = `*/10 6-23 * * * /opt/homebrew/bin/node ${ROOT}/scripts/train-watch.mjs check >> ${ROOT}/tmp/train-watch.log 2>&1`;
function syncCron(needed) {
  const cur = (() => { try { return execFileSync("crontab", ["-l"], { encoding: "utf8" }); } catch { return ""; } })();
  const has = cur.includes("train-watch.mjs check");
  if (needed === has) return;
  const next = needed
    ? cur.replace(/\n?$/, "\n") + CRON_LINE + "\n"
    : cur.split("\n").filter((l) => !l.includes("train-watch.mjs check")).join("\n") + "\n";
  execFileSync("crontab", ["-"], { input: next });
  console.error(needed ? "cron installed" : "cron removed (no active watches)");
}

if (cmd === "add") {
  const w = load();
  const item = {
    id: `tw${Date.now().toString(36)}`,
    from: opt("--from"), to: opt("--to"), date: opt("--date"),
    trains: (opt("--trains", "") || "").split(",").filter(Boolean),
    seats: (opt("--seat", "二等座") || "二等座").split(",").filter((s) => SEAT_FIELDS[s]),
    created: new Date().toISOString(), active: true,
  };
  if (!item.from || !item.to || !/^\d{4}-\d{2}-\d{2}$/.test(item.date ?? "")) {
    console.error("需要 --from --to --date YYYY-MM-DD"); process.exit(2);
  }
  if (!item.seats.length) item.seats = ["二等座"];
  w.push(item); save(w);
  syncCron(true);
  console.log(JSON.stringify({ ok: true, id: item.id, watch: item }));
} else if (cmd === "list") {
  console.log(JSON.stringify(load(), null, 2));
} else if (cmd === "remove") {
  const id = opt("--id");
  const w = load(); const n = w.length;
  save(w.filter((x) => x.id !== id));
  syncCron(load().some((x) => x.active));
  console.log(JSON.stringify({ ok: true, removed: n - load().length }));
} else if (cmd === "check") {
  const w = load();
  const today = new Date(); today.setHours(0, 0, 0, 0);
  let dirty = false;
  for (const item of w) {
    if (!item.active) continue;
    if (new Date(item.date) < today) { item.active = false; dirty = true; continue; } // 过期自动停
    let trains;
    try {
      const raw = execFileSync("bash", [`${ROOT}/train_query.sh`, item.from, item.to, item.date],
        { encoding: "utf8", timeout: 60000 });
      trains = JSON.parse(raw.slice(raw.indexOf("{"))).trains ?? [];
    } catch (e) { console.error(`query failed for ${item.id}: ${e.message}`); continue; }

    const wanted = trains.filter((t) =>
      (!item.trains.length || item.trains.includes(t.train_no)) &&
      item.seats.some((s) => hasSeat(t[SEAT_FIELDS[s]])));
    if (!wanted.length) { console.log(`${item.id}: no seats yet`); continue; }

    const lines = wanted.slice(0, 6).map((t) => {
      const seatText = item.seats
        .filter((s) => hasSeat(t[SEAT_FIELDS[s]]))
        .map((s) => `${s}:${t[SEAT_FIELDS[s]]}`).join(" ");
      return `${t.train_no} ${t.depart_time}→${t.arrive_time} ${seatText}${t.can_buy === false ? "（12306显示暂不可购,可能是候补/起售中,快去看）" : ""}`;
    });
    await sendText(`🎫 有票了！${item.date} ${item.from}→${item.to}\n${lines.join("\n")}\n\n快去 12306 买，这个监控已自动停止。`);
    fs.appendFileSync(`${ROOT}/memory/recent-context.md`,
      `[${new Date().toLocaleString("sv-SE", { timeZone: "Asia/Shanghai" }).replace("T", " ")}] [train-watch] ${item.date} ${item.from}→${item.to} 出票提醒已推送(${wanted.length}趟)\n`);
    item.active = false; item.notified = new Date().toISOString(); dirty = true;
  }
  if (dirty) save(w);
  syncCron(w.some((x) => x.active));
} else {
  console.error("usage: train-watch.mjs <add|list|remove|check> ..."); process.exit(2);
}
