#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { isConversationLine } from "./conversation-context.mjs";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const memoryDir = path.join(projectRoot, "memory");
const handoffPath = path.join(memoryDir, "session-handoff.md");
const recentContextPath = path.join(memoryDir, "recent-context.md");
const sessionStatePath = path.join(memoryDir, "session-state.json");
const sessionCheckpointPath = path.join(memoryDir, "session-checkpoint.json");
const previewManifestPath = path.join(projectRoot, "projects", "previews", "manifest.json");

const args = process.argv.slice(2);
const opts = {
  reason: "manual",
  source: "unknown",
  dryRun: false,
};

for (let i = 0; i < args.length; i += 1) {
  const arg = args[i];
  if (arg === "--reason") opts.reason = args[++i] || opts.reason;
  else if (arg === "--source") opts.source = args[++i] || opts.source;
  else if (arg === "--dry-run") opts.dryRun = true;
}

function readText(filePath) {
  try {
    return fs.readFileSync(filePath, "utf8");
  } catch {
    return "";
  }
}

function readJson(filePath, fallback) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return fallback;
  }
}

function redact(value) {
  return String(value ?? "")
    .replace(/([?&](?:k|token|key|auth|access_token)=)[^&\s)]+/gi, "$1[redacted]")
    .replace(/(Authorization:\s*Bearer\s+)[A-Za-z0-9._~+/=-]+/gi, "$1[redacted]")
    .replace(/\b[A-Za-z0-9_-]{32,}\b/g, "[redacted]")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 240);
}

function recentContextEntries(limit = 5) {
  const lines = readText(recentContextPath).split(/\r?\n/);
  return lines
    .filter(isConversationLine)
    .slice(-limit)
    .map(redact);
}

function gitStatus(limit = 12) {
  try {
    const out = execFileSync("git", ["status", "--short", "--untracked-files=all"], {
      cwd: projectRoot,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 3000,
    });
    const entries = out
      .split(/\r?\n/)
      .map((line) => line.trimEnd())
      .filter(Boolean)
      .filter((line) => !line.includes(".env"))
      .map((line) => {
        const filePart = line.slice(3).split(" -> ").pop();
        const absolutePath = path.join(projectRoot, filePart);
        let mtimeMs = 0;
        try {
          mtimeMs = fs.statSync(absolutePath).mtimeMs;
        } catch {}
        return { line, mtimeMs };
      })
      .sort((a, b) => b.mtimeMs - a.mtimeMs);
    const output = entries.slice(0, limit).map((entry) => redact(entry.line));
    if (entries.length > limit) output.push(`... ${entries.length - limit} more changed files`);
    return output;
  } catch {
    return [];
  }
}

function activePreviews(limit = 8) {
  const manifest = readJson(previewManifestPath, { previews: {} });
  return Object.values(manifest.previews || {})
    .sort((a, b) => String(b.updatedAt || b.createdAt || "").localeCompare(String(a.updatedAt || a.createdAt || "")))
    .slice(0, limit)
    .map((item) => {
      const title = item.title ? ` - ${redact(item.title)}` : "";
      const updated = item.updatedAt || item.createdAt || "unknown";
      return `${redact(item.slug)}${title} (${updated})`;
    });
}

function currentState() {
  const state = readJson(sessionStatePath, {});
  return {
    lastActivity: state.last_activity || "unknown",
    lastReset: state.last_reset || "unknown",
  };
}

function currentCheckpoint() {
  const checkpoint = readJson(sessionCheckpointPath, null);
  if (!checkpoint || typeof checkpoint !== "object") return null;
  return checkpoint;
}

function appendCheckpoint(lines, checkpoint) {
  if (!checkpoint) return;
  lines.push(`- checkpoint: ${redact(checkpoint.generated_at || "unknown")}`);
  lines.push(`- currentTopic: ${redact(checkpoint.current_topic || "unknown")}`);
  lines.push(`- userGoal: ${redact(checkpoint.user_goal || "unknown")}`);
  lines.push(`- continuationStyle: ${redact(checkpoint.continuation_style || "continue")}`);
  const sections = [
    ["decisions", checkpoint.decisions],
    ["openLoops", checkpoint.open_loops],
    ["activeTasks", checkpoint.active_tasks],
    ["importantEntities", checkpoint.important_entities],
  ];
  for (const [name, values] of sections) {
    if (!Array.isArray(values) || !values.length) continue;
    lines.push(`- ${name}:`);
    for (const value of values.slice(0, 10)) lines.push(`  - ${redact(value)}`);
  }
}

function buildEntry() {
  const now = new Date().toISOString();
  const state = currentState();
  const changed = gitStatus();
  const recent = recentContextEntries();
  const previews = activePreviews();
  const checkpoint = currentCheckpoint();

  const lines = [
    `## ${now}`,
    "",
    `- reason: ${redact(opts.reason)}`,
    `- source: ${redact(opts.source)}`,
    `- lastActivity: ${redact(state.lastActivity)}`,
    `- lastReset: ${redact(state.lastReset)}`,
  ];

  appendCheckpoint(lines, checkpoint);

  if (changed.length) {
    lines.push("- changedFiles:");
    for (const item of changed) lines.push(`  - ${item}`);
  }
  if (previews.length) {
    lines.push("- activePreviews:");
    for (const item of previews) lines.push(`  - ${item}`);
  }
  if (!checkpoint && recent.length) {
    lines.push("- recentContext:");
    for (const item of recent) lines.push(`  - ${item}`);
  }
  lines.push("");
  return lines.join("\n");
}

function existingEntries(text) {
  return text
    .split(/\n(?=## \d{4}-\d{2}-\d{2}T)/)
    .map((entry) => entry.trim())
    .filter((entry) => entry.startsWith("## "));
}

function main() {
  fs.mkdirSync(memoryDir, { recursive: true });
  const header = [
    "# Session Handoff",
    "",
    "只记录跨重启继续工作需要的轻量交接。不要写完整聊天、密钥、token、临时猜测或可从实时工具重新查询的信息。",
    "",
  ].join("\n");
  const current = readText(handoffPath);
  const entries = [buildEntry().trim(), ...existingEntries(current)].slice(0, 12);
  const output = `${header}${entries.join("\n\n")}\n`;
  if (!opts.dryRun) fs.writeFileSync(handoffPath, output, "utf8");
  process.stdout.write(`${JSON.stringify({
    ok: true,
    dryRun: opts.dryRun,
    file: handoffPath,
    entries: entries.length,
  }, null, 2)}\n`);
}

main();
