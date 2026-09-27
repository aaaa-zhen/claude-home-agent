#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const jsonOutput = args.includes("--json");
const dryRun = args.includes("--dry-run");
const separator = args.indexOf("--");
const text = (separator >= 0 ? args.slice(separator + 1) : args.filter((arg) => !arg.startsWith("--"))).join(" ").trim();

const climates = [
  { id: "climate.gree", name: "客厅空调", rooms: ["客厅", "厅"] },
  { id: "climate.gree_e6d9", name: "主卧空调", rooms: ["主卧", "卧室", "睡房"] },
  { id: "climate.studioroom", name: "书房空调", rooms: ["书房"] },
];

const lights = [
  { id: "switch.living_room_ambient_light", name: "客厅氛围灯", rooms: ["客厅", "厅"], aliases: ["氛围灯", "客厅灯", "灯"] },
  { id: "switch.living_room_main_light", name: "客厅主灯", rooms: ["客厅", "厅"], aliases: ["主灯", "吊灯"] },
  { id: "switch.dining_room_light", name: "餐厅灯", rooms: ["餐厅", "饭厅"], aliases: ["餐厅灯", "灯"] },
];

function readEnvFile(filePath) {
  if (!fs.existsSync(filePath)) return {};
  const env = {};
  for (const line of fs.readFileSync(filePath, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const index = trimmed.indexOf("=");
    if (index <= 0) continue;
    const key = trimmed.slice(0, index).trim();
    let value = trimmed.slice(index + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    env[key] = value;
  }
  return env;
}

function normalizeHaUrl(url) {
  const clean = String(url ?? "").trim().replace(/\/+$/, "");
  if (!clean) return "";
  return clean.endsWith("/api") ? clean : `${clean}/api`;
}

function normalized(value) {
  return String(value ?? "").replace(/\s+/g, "").toLowerCase();
}

function parseTemperature(input) {
  const match = input.match(/(?:调到|设到|设置到|设为|到)?\s*(1[6-9]|2[0-9]|3[0-2])\s*(?:度|°|c|℃)?/i);
  if (!match) return null;
  return Number(match[1]);
}

function isQuestionLike(input) {
  const compact = normalized(input);
  const asksStatus = /(开没开|关没关|开着|关着|状态|哪些|什么|多少度|吗|么|？|\?)/.test(compact);
  const commandPrefix = /^(帮我|给我|请|把)?(打开|开启|开一下|关闭|关掉|关一下|调到|设到|设置|设为|切到)/.test(compact);
  const commandPattern = /(帮我|给我|请|把).*(打开|开启|关闭|关掉|调到|设到|设置|设为|切到)/.test(compact);
  return asksStatus && !commandPrefix && !commandPattern;
}

function parseIntent(input) {
  const compact = normalized(input);
  if (!compact || compact.length > 80 || compact.startsWith("/") || isQuestionLike(input)) return null;
  if (!/(空调|灯|氛围灯|主灯|吊灯|餐厅灯)/.test(compact)) return null;
  if (/(怎么|如何|为什么|教程|网页|网站|写一个|做一个|生成)/.test(compact)) return null;

  const temperature = parseTemperature(input);
  const mode = /除湿/.test(compact) ? "dry" : /制热|暖风/.test(compact) ? "heat" : /制冷|冷风/.test(compact) ? "cool" : /送风/.test(compact) ? "fan_only" : null;
  const quiet = /(静音|安静|低噪)/.test(compact);
  const off = /(关闭|关掉|关了|关一下|关空调|关灯|关掉空调|关掉灯)/.test(compact) || /^(把)?(.+)?(空调|灯).*(关|关闭)$/.test(compact);
  const on = /(打开|开启|开一下|开空调|开灯|打开空调|打开灯)/.test(compact) || /^(把)?(.+)?(空调|灯).*(开|打开)$/.test(compact);
  if (!off && !on && !temperature && !mode && !quiet) return null;

  const device = /空调/.test(compact) || temperature || mode || quiet ? "climate" : "light";
  let action = off ? "off" : "on";
  if (temperature) action = "set_temperature";
  if (mode) action = "set_mode";
  if (quiet) action = "quiet";
  return { device, action, temperature, mode, quiet, compact };
}

function includesAny(compact, values) {
  return values.some((value) => compact.includes(value));
}

function selectClimateTargets(compact) {
  if (/(全部|所有|全屋|都).{0,4}空调|空调.{0,4}(全部|所有|全屋|都)/.test(compact)) return climates;
  return climates.filter((item) => includesAny(compact, item.rooms));
}

function selectLightTargets(compact) {
  if (/(全部|所有|全屋|都).{0,4}灯|灯.{0,4}(全部|所有|全屋|都)/.test(compact)) return lights;
  if (/(主灯|吊灯)/.test(compact)) return lights.filter((item) => item.id === "switch.living_room_main_light");
  if (/餐厅|饭厅/.test(compact)) return lights.filter((item) => item.id === "switch.dining_room_light");
  if (/氛围灯|客厅灯|客厅|开灯|关灯|灯/.test(compact)) return lights.filter((item) => item.id === "switch.living_room_ambient_light");
  return [];
}

async function haRequest(haUrl, token, endpoint, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? 2500);
  try {
    const response = await fetch(`${haUrl}${endpoint}`, {
      method: options.method ?? "GET",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
      signal: controller.signal,
    });
    const textBody = await response.text();
    if (!response.ok) throw new Error(`HA HTTP ${response.status}: ${textBody.slice(0, 160)}`);
    return textBody ? JSON.parse(textBody) : null;
  } finally {
    clearTimeout(timeout);
  }
}

async function callService(haUrl, token, domain, service, body) {
  if (dryRun) return;
  await haRequest(haUrl, token, `/services/${domain}/${service}`, {
    method: "POST",
    body,
    timeoutMs: 3500,
  });
}

async function getState(haUrl, token, entityId) {
  return await haRequest(haUrl, token, `/states/${encodeURIComponent(entityId)}`);
}

function stateLabel(entity) {
  if (!entity) return "未知";
  const attrs = entity.attributes || {};
  if (entity.entity_id?.startsWith("climate.")) {
    if (entity.state === "off") return "关着";
    const mode = { cool: "制冷", heat: "制热", dry: "除湿", fan_only: "送风", heat_cool: "自动", auto: "自动" }[entity.state] || entity.state;
    const target = attrs.temperature != null ? `，设定 ${attrs.temperature}°C` : "";
    const fan = attrs.fan_mode ? `，${attrs.fan_mode === "quiet" ? "静音风" : attrs.fan_mode}` : "";
    return `${mode}${target}${fan}`;
  }
  if (entity.state === "on") return "开着";
  if (entity.state === "off") return "关着";
  return entity.state || "未知";
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function actionLabel(intent) {
  if (intent.action === "off") return "关闭";
  if (intent.action === "set_temperature") return `调到 ${intent.temperature}°C`;
  if (intent.action === "set_mode") {
    return {
      cool: "设为制冷",
      heat: "设为制热",
      dry: "设为除湿",
      fan_only: "设为送风",
    }[intent.mode] || `设为 ${intent.mode}`;
  }
  if (intent.action === "quiet") return "设为静音风";
  return "打开";
}

function isUnavailable(entity) {
  return !entity || entity.state === "unknown" || entity.state === "unavailable";
}

function stateMatchesIntent(entity, intent) {
  if (isUnavailable(entity)) return false;
  const attrs = entity.attributes || {};
  if (intent.device === "light") {
    return entity.state === (intent.action === "off" ? "off" : "on");
  }
  if (intent.action === "off") return entity.state === "off";
  if (entity.state === "off") return false;
  if (intent.action === "set_mode") return entity.state === intent.mode;
  if (intent.action === "set_temperature") return Math.abs(Number(attrs.temperature) - intent.temperature) < 0.6;
  if (intent.action === "quiet") return attrs.fan_mode === "quiet";
  return true;
}

async function applyClimate(haUrl, token, intent, targets) {
  const ids = targets.map((item) => item.id);
  if (intent.action === "off") {
    await callService(haUrl, token, "climate", "set_hvac_mode", { entity_id: ids, hvac_mode: "off" });
  } else {
    await callService(haUrl, token, "climate", "turn_on", { entity_id: ids });
    if (intent.mode) await callService(haUrl, token, "climate", "set_hvac_mode", { entity_id: ids, hvac_mode: intent.mode });
    if (intent.temperature) await callService(haUrl, token, "climate", "set_temperature", { entity_id: ids, temperature: intent.temperature });
    if (intent.quiet) await callService(haUrl, token, "climate", "set_fan_mode", { entity_id: ids, fan_mode: "quiet" });
  }
}

async function applyLights(haUrl, token, intent, targets) {
  const ids = targets.map((item) => item.id);
  await callService(haUrl, token, "switch", intent.action === "off" ? "turn_off" : "turn_on", { entity_id: ids });
}

async function waitForTargetState(haUrl, token, intent, item) {
  const deadline = Date.now() + 4500;
  let lastEntity = null;
  let delay = 650;
  let attempts = 0;
  while (Date.now() <= deadline) {
    await sleep(delay);
    delay = 550;
    attempts += 1;
    try {
      lastEntity = await getState(haUrl, token, item.id);
      if (stateMatchesIntent(lastEntity, intent)) {
        return { item, entity: lastEntity, confirmed: true, attempts };
      }
    } catch {
      lastEntity = null;
    }
  }
  return { item, entity: lastEntity, confirmed: false, attempts };
}

async function verifyTargets(haUrl, token, intent, targets) {
  const label = actionLabel(intent);
  if (dryRun) {
    return {
      confirmed: true,
      action: intent.action,
      expected: label,
      targets: targets.map((item) => ({ entity_id: item.id, name: item.name, confirmed: true, dryRun: true })),
      lines: targets.map((item) => `${item.name}：将${label}`),
    };
  }
  const states = await Promise.all(targets.map((item) => waitForTargetState(haUrl, token, intent, item)));
  return {
    action: intent.action,
    expected: label,
    confirmed: states.every((state) => state.confirmed),
    targets: states.map(({ item, entity, confirmed, attempts }) => ({
      entity_id: item.id,
      name: item.name,
      confirmed,
      attempts,
      state: entity?.state ?? null,
      attributes: {
        temperature: entity?.attributes?.temperature ?? null,
        current_temperature: entity?.attributes?.current_temperature ?? null,
        fan_mode: entity?.attributes?.fan_mode ?? null,
        friendly_name: entity?.attributes?.friendly_name ?? null,
      },
    })),
    lines: states.map(({ item, entity, confirmed }) => {
      if (confirmed) return `${item.name}：${stateLabel(entity)}`;
      const current = entity && !isUnavailable(entity) ? `，当前读到：${stateLabel(entity)}` : "";
      return `${item.name}：${label}指令已发出，状态还在同步${current}`;
    }),
  };
}

function response(data) {
  if (jsonOutput) {
    process.stdout.write(`${JSON.stringify(data)}\n`);
    return;
  }
  if (data.handled && data.text) process.stdout.write(`${data.text}\n`);
}

async function main() {
  const intent = parseIntent(text);
  if (!intent) {
    response({ handled: false });
    return;
  }

  const targets = intent.device === "climate" ? selectClimateTargets(intent.compact) : selectLightTargets(intent.compact);
  if (!targets.length) {
    const ask = intent.device === "climate" ? "要操作哪台空调？客厅、主卧、书房？" : "要操作哪盏灯？客厅氛围灯、客厅主灯、餐厅灯？";
    response({ handled: true, text: ask });
    return;
  }

  const env = { ...readEnvFile(path.join(projectRoot, ".env")), ...process.env };
  const haUrl = normalizeHaUrl(env.HA_URL);
  const token = env.HA_TOKEN;
  if (!haUrl || !token) throw new Error("HA_URL or HA_TOKEN missing");

  if (intent.device === "climate") await applyClimate(haUrl, token, intent, targets);
  else await applyLights(haUrl, token, intent, targets);

  const verification = await verifyTargets(haUrl, token, intent, targets);
  const prefix = dryRun ? "会执行：" : verification.confirmed ? "已处理：" : "已发送：";
  response({
    handled: true,
    source: "home_assistant",
    text: `${prefix}${verification.lines.join("；")}`,
    verification,
  });
}

main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  if (jsonOutput) response({ handled: false, error: message });
  else process.stderr.write(`ha-fast-control failed: ${message}\n`);
  process.exitCode = 1;
});
