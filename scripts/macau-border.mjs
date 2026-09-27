#!/usr/bin/env node
// macau-border.mjs — 珠海↔澳门各口岸实时通关状况(澳门治安警察局公开接口,无鉴权)。
// 数据源: https://www.fsm.gov.mo/psp/pspmonitor/webservice.asmx/getStatus
// 视角说明: 接口是澳门侧视角。Id: E=入境澳门(即珠海→澳门方向), D=离开澳门(即回珠海方向)。
// 青茂/横琴为"合作查验、一次放行",一个状态即代表整条通道。
// 用法: node scripts/macau-border.mjs [--json]

const PORTS = [
  { name: "拱北↔关闸", pn: ["2"], note: "开放 06:00–次日01:00" },
  { name: "青茂口岸", pn: ["55"], note: "24小时" },
  { name: "横琴口岸", pn: ["5"], note: "24小时" },
  { name: "港珠澳大桥(澳门)", pn: ["16", "17", "18", "19"], note: "24小时" },
];

const STATUS = {
  "1": { text: "畅通", eta: "约10分钟内", rank: 1 },
  "2": { text: "繁忙", eta: "约30分钟", rank: 2 },
  "3": { text: "挤拥", eta: "约45分钟", rank: 3 },
  "4": { text: "分流", eta: "超过1小时", rank: 4 },
  "5": { text: "暂停通行", eta: "", rank: 5 },
};

const asJson = process.argv.includes("--json");

const res = await fetch("https://www.fsm.gov.mo/psp/pspmonitor/webservice.asmx/getStatus", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
  },
  body: "{}",
  signal: AbortSignal.timeout(20000),
});
if (!res.ok) {
  console.error(`FSM API HTTP ${res.status}`);
  process.exit(1);
}
const payload = JSON.parse((await res.json()).d);
if (!payload.Rs) {
  console.error("FSM API returned Rs=false");
  process.exit(1);
}

// 每个口岸按方向取记录;多编号(大桥)取最差状态
const byKey = new Map();
for (const r of payload.Rt) byKey.set(`${r.Pn}:${r.Id}`, r);

function worst(pns, dir) {
  let pick = null;
  for (const pn of pns) {
    const r = byKey.get(`${pn}:${dir}`);
    if (!r || !STATUS[r.St]) continue; // 0/100 = 无数据/不适用
    if (!pick || STATUS[r.St].rank > STATUS[pick.St].rank) pick = r;
  }
  return pick;
}

const time = payload.Rt.find((r) => r.Ti)?.Ti ?? "";
const out = PORTS.map((p) => {
  const go = worst(p.pn, "E");   // 珠海→澳门 = 入境澳门
  const back = worst(p.pn, "D"); // 澳门→珠海 = 离开澳门
  const fmt = (r) => (r ? `${STATUS[r.St].text}${STATUS[r.St].eta ? `(${STATUS[r.St].eta})` : ""}` : "无数据");
  return { port: p.name, note: p.note, go: fmt(go), back: fmt(back),
           goCode: go?.St ?? null, backCode: back?.St ?? null };
});

if (asJson) {
  console.log(JSON.stringify({ time, ports: out }, null, 2));
} else {
  console.log(`澳门口岸通关实况(${time},澳门治安警数据)`);
  for (const p of out) console.log(`${p.port}:去澳门 ${p.go} | 回珠海 ${p.back}(${p.note})`);
}
