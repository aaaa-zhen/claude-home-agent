#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const jsonOutput = args.includes("--json");

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

function readJson(filePath, fallback) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return fallback;
  }
}

function normalizeHaUrl(url) {
  const clean = String(url ?? "").trim().replace(/\/+$/, "");
  if (!clean) return "";
  return clean.endsWith("/api") ? clean : `${clean}/api`;
}

function entityName(entity, fallback) {
  return fallback || entity?.attributes?.friendly_name || entity?.entity_id || "未知设备";
}

function fmtNumber(value) {
  if (value === undefined || value === null || value === "") return "";
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return Number.isInteger(num) ? String(num) : String(Math.round(num * 10) / 10);
}

function hvacLabel(state) {
  return {
    cool: "制冷中",
    heat: "制热中",
    dry: "除湿中",
    fan_only: "送风中",
    heat_cool: "自动中",
    auto: "自动中",
    off: "关着",
    unavailable: "不可用",
    unknown: "未知",
  }[state] || state;
}

function fanLabel(mode) {
  return {
    quiet: "静音风",
    silent: "静音风",
    low: "低风",
    medium: "中风",
    high: "高风",
    auto: "自动风",
  }[mode] || mode;
}

function mediaLabel(state) {
  return {
    playing: "播放中",
    paused: "暂停",
    idle: "待机/空闲",
    standby: "待机",
    off: "关着",
    unavailable: "不可用",
    unknown: "未知",
  }[state] || state;
}

function isUnavailable(entity) {
  return !entity || entity.state === "unavailable" || entity.state === "unknown";
}

function formatClimate(entity, name) {
  if (!entity) return `${name}：未知`;
  const state = entity.state;
  const attrs = entity.attributes || {};
  if (state === "off") return `${name}：关着`;
  if (isUnavailable(entity)) return `${name}：${hvacLabel(state)}`;
  const parts = [`${name}：${hvacLabel(state)}`];
  const current = fmtNumber(attrs.current_temperature);
  const target = fmtNumber(attrs.temperature);
  if (current) parts.push(`${current}°C`);
  if (target) parts.push(`设定 ${target}°C`);
  if (attrs.fan_mode) parts.push(fanLabel(attrs.fan_mode));
  return parts.join("，");
}

function formatSwitch(entity, name) {
  if (!entity) return `${name}：未知`;
  if (entity.state === "on") return `${name}：开着`;
  if (entity.state === "off") return `${name}：关着`;
  return `${name}：${entity.state}`;
}

function formatMedia(entity, name) {
  if (!entity) return `${name}：未知`;
  const attrs = entity.attributes || {};
  const label = mediaLabel(entity.state);
  if ((entity.state === "idle" || entity.state === "standby") && attrs.media_title) {
    return `${name}：${label}，有播放记录`;
  }
  if (entity.state === "playing" && attrs.media_title) {
    return `${name}：播放中，${attrs.media_title}`;
  }
  return `${name}：${label}`;
}

function isActiveDevice(entity) {
  if (!entity || isUnavailable(entity)) return false;
  if (entity.entity_id.startsWith("climate.")) return entity.state !== "off";
  if (entity.entity_id.startsWith("switch.") || entity.entity_id.startsWith("light.")) return entity.state === "on";
  if (entity.entity_id.startsWith("media_player.")) return entity.state !== "off";
  return entity.state === "on";
}

function stateById(states) {
  return new Map(states.map((entity) => [entity.entity_id, entity]));
}

async function fetchStates(haUrl, token) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 2500);
  try {
    const response = await fetch(`${haUrl}/states`, {
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HA HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}

function formatStatus(states, config) {
  const byId = stateById(states);
  const configuredChecks = config.entities?.deviceChecks || [];
  const checks = configuredChecks.map((item) => ({
    id: item.entity_id,
    name: item.name,
    kind: item.entity_id?.split(".")[0] || "",
  }));
  const mediaChecks = [
    { id: "media_player.living_room", name: "HomePod", kind: "media_player" },
    { id: "media_player.living_room_2", name: "Apple TV", kind: "media_player" },
  ];
  const allChecks = [...checks, ...mediaChecks];
  const activeLines = [];
  const offClimates = [];
  const offLights = [];
  const unknown = [];

  for (const check of allChecks) {
    const entity = byId.get(check.id);
    const name = entityName(entity, check.name);
    if (!entity || isUnavailable(entity)) {
      unknown.push(name);
      continue;
    }
    if (isActiveDevice(entity)) {
      if (check.kind === "climate") activeLines.push(formatClimate(entity, name));
      else if (check.kind === "media_player") activeLines.push(formatMedia(entity, name));
      else activeLines.push(formatSwitch(entity, name));
      continue;
    }
    if (check.kind === "climate") offClimates.push(name);
    else if (check.kind === "switch" || check.kind === "light") offLights.push(name);
  }

  const frontDoorId = config.homeAssistant?.frontDoorEntity || "binary_sensor.front_door_contact";
  const frontDoor = byId.get(frontDoorId);
  let doorSummary = "";
  if (frontDoor) {
    if (frontDoor.state === "on") doorSummary = "大门是开的。";
    else if (frontDoor.state === "off") doorSummary = "大门是关的。";
    else doorSummary = `大门状态：${frontDoor.state}。`;
  }

  const lines = [];
  if (activeLines.length) {
    lines.push("家里现在开着：", "");
    for (const line of activeLines) lines.push(`- ${line}`);
  } else {
    lines.push("家里现在没看到明显开着的设备。");
  }

  const summary = [];
  if (offLights.length) summary.push(`灯都关着：${offLights.join("、")}。`);
  if (offClimates.length) summary.push(`${offClimates.join("、")}关着。`);
  if (doorSummary) summary.push(doorSummary);
  if (unknown.length) summary.push(`这些设备状态暂时不可用：${unknown.join("、")}。`);
  if (summary.length) lines.push("", summary.join(" "));

  return lines.join("\n");
}

async function main() {
  const startedAt = performance.now();
  const env = { ...readEnvFile(path.join(projectRoot, ".env")), ...process.env };
  const haUrl = normalizeHaUrl(env.HA_URL);
  const token = env.HA_TOKEN;
  if (!haUrl || !token) throw new Error("HA_URL or HA_TOKEN missing");

  const config = readJson(path.join(projectRoot, "config.json"), {});
  const states = await fetchStates(haUrl, token);
  const text = formatStatus(states, config);
  const durationMs = Math.round(performance.now() - startedAt);
  const configuredChecks = config.entities?.deviceChecks || [];
  const checkedEntityIds = [
    ...configuredChecks.map((item) => item.entity_id).filter(Boolean),
    "media_player.living_room",
    "media_player.living_room_2",
    config.homeAssistant?.frontDoorEntity || "binary_sensor.front_door_contact",
  ];
  const byId = stateById(states);
  const activeEntityIds = checkedEntityIds.filter((entityId) => isActiveDevice(byId.get(entityId)));

  if (jsonOutput) {
    process.stdout.write(`${JSON.stringify({
      ok: true,
      source: "home_assistant",
      durationMs,
      count: states.length,
      checkedEntityIds,
      activeEntityIds,
      text,
    }, null, 2)}\n`);
  } else {
    process.stdout.write(`${text}\n`);
  }
}

main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  if (jsonOutput) process.stdout.write(`${JSON.stringify({ ok: false, error: message }, null, 2)}\n`);
  else process.stderr.write(`ha-fast-status failed: ${message}\n`);
  process.exitCode = 1;
});
