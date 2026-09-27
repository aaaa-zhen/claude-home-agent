#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const memoryRoot = path.join(projectRoot, "memory");
const tmpRoot = path.join(projectRoot, "tmp", "memory-optimize");

const args = process.argv.slice(2);
const mode = args.find((arg) => !arg.startsWith("--")) || "daily";
const dryRun = args.includes("--dry-run");

const MAX_RECENT_ENTRIES = numberArg("--recent-max", 80);
const MAX_HANDOFF_SECTIONS = numberArg("--handoff-max", 12);
const FOLLOWUP_TTL_DAYS = numberArg("--followup-ttl-days", 14);

const runId = new Date().toISOString().replace(/[:.]/g, "-");

function numberArg(name, fallback) {
  const index = args.indexOf(name);
  if (index >= 0 && args[index + 1]) {
    const parsed = Number(args[index + 1]);
    if (Number.isFinite(parsed) && parsed > 0) return parsed;
  }
  return fallback;
}

function localDateOnly(date = new Date()) {
  const offsetMs = -date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() + offsetMs).toISOString().slice(0, 10);
}

function parseDateOnly(value) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day);
}

function daysBetween(a, b) {
  const ms = parseDateOnly(a).getTime() - parseDateOnly(b).getTime();
  return Math.round(ms / 86_400_000);
}

function readText(relativePath, fallback = "") {
  const filePath = path.join(projectRoot, relativePath);
  try {
    return fs.readFileSync(filePath, "utf8");
  } catch {
    return fallback;
  }
}

function writeTextAtomic(filePath, text) {
  const tmpPath = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(tmpPath, text, "utf8");
  fs.renameSync(tmpPath, filePath);
}

function backupFile(relativePath) {
  const source = path.join(projectRoot, relativePath);
  if (!fs.existsSync(source) || dryRun) return "";
  const backupDir = path.join(tmpRoot, "backups", runId);
  fs.mkdirSync(backupDir, { recursive: true });
  const safeName = relativePath.replace(/[\\/]/g, "__");
  const target = path.join(backupDir, safeName);
  fs.copyFileSync(source, target);
  return target;
}

function maybeWrite(relativePath, nextText, changes, reason) {
  const filePath = path.join(projectRoot, relativePath);
  const before = readText(relativePath);
  if (before === nextText) {
    changes.push({ file: relativePath, changed: false, reason });
    return false;
  }
  const backup = backupFile(relativePath);
  if (!dryRun) writeTextAtomic(filePath, nextText);
  changes.push({ file: relativePath, changed: true, reason, backup });
  return true;
}

function isExpiredDate(value, todayText) {
  return parseDateOnly(value).getTime() < parseDateOnly(todayText).getTime();
}

function cleanupExpiredScheduleLines(text, todayText) {
  const lines = text.split(/\r?\n/);
  const output = [];
  const removed = [];
  let inSchedule = false;

  for (const line of lines) {
    if (/^##\s+日程\s*$/.test(line)) {
      inSchedule = true;
      output.push(line);
      continue;
    }
    if (inSchedule && /^##\s+/.test(line)) inSchedule = false;

    if (inSchedule) {
      const dates = [...line.matchAll(/\b(20\d{2}-\d{2}-\d{2})\b/g)].map((match) => match[1]);
      if (dates.some((dateText) => isExpiredDate(dateText, todayText))) {
        removed.push(line);
        continue;
      }
    }
    output.push(line);
  }

  return { text: output.join("\n"), removed };
}

function trimRecentContext(text, maxEntries) {
  const lines = text.replace(/\s+$/u, "").split(/\r?\n/);
  const firstEntry = lines.findIndex((line) => /^\[/.test(line));
  if (firstEntry < 0) return { text: `${lines.join("\n")}\n`, removed: 0 };
  const header = lines.slice(0, firstEntry);
  const entries = lines.slice(firstEntry).filter(Boolean);
  const kept = entries.slice(-maxEntries);
  const dropped = entries.slice(0, Math.max(0, entries.length - kept.length));
  archiveToDaily(dropped);
  return {
    text: [...header, ...kept].join("\n") + "\n",
    removed: dropped.length,
  };
}

// 裁掉的条目不丢弃:按日期归档到 memory/daily/YYYY-MM-DD.md(每日工作层,只索引不注入)
function archiveToDaily(droppedLines) {
  if (!droppedLines.length || dryRun) return;
  const dailyDir = path.join(memoryRoot, "daily");
  fs.mkdirSync(dailyDir, { recursive: true });
  const byDate = new Map();
  for (const line of droppedLines) {
    const m = line.match(/^\[(20\d{2}-\d{2}-\d{2})/);
    const date = m ? m[1] : "undated";
    if (!byDate.has(date)) byDate.set(date, []);
    byDate.get(date).push(line);
  }
  for (const [date, lines] of byDate) {
    const file = path.join(dailyDir, `${date}.md`);
    const existing = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : `# ${date} 对话流水（自动归档）\n\n`;
    const fresh = lines.filter((l) => !existing.includes(l));
    if (fresh.length) fs.writeFileSync(file, existing.replace(/\s*$/u, "\n") + fresh.join("\n") + "\n");
  }
}

function trimSessionHandoff(text, maxSections) {
  const lines = text.replace(/\s+$/u, "").split(/\r?\n/);
  const starts = [];
  lines.forEach((line, index) => {
    if (/^##\s+\S/.test(line)) starts.push(index);
  });
  if (starts.length <= maxSections) return { text: `${lines.join("\n")}\n`, removed: 0 };
  const header = lines.slice(0, starts[0]);
  const sections = starts.map((start, index) => {
    const end = starts[index + 1] ?? lines.length;
    return lines.slice(start, end);
  });
  const kept = sections.slice(0, maxSections).flat();
  return {
    text: [...header, ...kept].join("\n") + "\n",
    removed: sections.length - maxSections,
  };
}

function archiveStaleFollowups(pendingText, archiveText, todayText, ttlDays) {
  const cutoff = new Date(parseDateOnly(todayText).getTime() - ttlDays * 86_400_000);
  const cutoffText = localDateOnly(cutoff);
  const pendingLines = pendingText.replace(/\s+$/u, "").split(/\r?\n/);
  const kept = [];
  const stale = [];

  for (const line of pendingLines) {
    const match = line.match(/^\[(20\d{2}-\d{2}-\d{2})\]\s+(.+)/);
    if (!match) {
      kept.push(line);
      continue;
    }
    const dateText = match[1];
    if (parseDateOnly(dateText).getTime() < parseDateOnly(cutoffText).getTime()) {
      stale.push(line);
    } else {
      kept.push(line);
    }
  }

  if (!stale.length) {
    return { pending: pendingText, archive: archiveText, archived: [] };
  }

  const suffix = ` — 归档于 ${todayText}（超过 ${ttlDays} 天未重新确认，不主动提醒）`;
  const archiveAppend = stale.map((line) => `${line}${suffix}`).join("\n");
  const nextArchive = `${archiveText.replace(/\s*$/u, "\n")}${archiveAppend}\n`;
  return {
    pending: `${kept.join("\n")}\n`,
    archive: nextArchive,
    archived: stale,
  };
}

function normalizeLine(line) {
  return line.replace(/\s+/g, " ").trim().toLowerCase();
}

function duplicateBullets(relativePath) {
  const lines = readText(relativePath).split(/\r?\n/);
  const seen = new Map();
  for (const [index, line] of lines.entries()) {
    if (!/^\s*-\s+/.test(line)) continue;
    const key = normalizeLine(line);
    if (!key || key.length < 16) continue;
    const item = seen.get(key) || { line: line.trim(), lines: [] };
    item.lines.push(index + 1);
    seen.set(key, item);
  }
  return [...seen.values()].filter((item) => item.lines.length > 1);
}

function stalePatternFindings() {
  const rules = [
    { name: "old-linux-home", pattern: /\/home\/ubuntu/i },
    { name: "ubuntu-crontab", pattern: /ubuntu\s+crontab/i },
    { name: "systemd-runtime", pattern: /\bsystemd\b/i },
    { name: "journalctl-runtime", pattern: /\bjournalctl\b/i },
    { name: "windows-drive", pattern: /\b[A-Z]:\\/ },
    { name: "windows-c-drive-note", pattern: /C\s*盘/ },
    { name: "old-vps-current", pattern: /腾讯云|东京\s*VPS|Linux\s*VPS/i },
  ];
  const files = [
    "AGENTS.md",
    "memory/index.md",
    "memory/user-profile.md",
    "memory/learned-facts.md",
    "memory/devices.md",
    "memory/pending-followups.md",
    "memory/recent-context.md",
    "memory/session-handoff.md",
  ];
  const findings = [];
  for (const relativePath of files) {
    const text = readText(relativePath);
    if (!text) continue;
    text.split(/\r?\n/).forEach((line, index) => {
      for (const rule of rules) {
        if (rule.pattern.test(line)) {
          findings.push({ file: relativePath, line: index + 1, rule: rule.name, text: line.trim().slice(0, 160) });
        }
      }
    });
  }
  return findings;
}

function fileStats(relativePath) {
  const text = readText(relativePath);
  return {
    file: relativePath,
    lines: text ? text.split(/\r?\n/).length : 0,
    chars: text.length,
  };
}

function pendingAges(todayText) {
  return readText("memory/pending-followups.md")
    .split(/\r?\n/)
    .map((line, index) => {
      const match = line.match(/^\[(20\d{2}-\d{2}-\d{2})\]\s+(.+)/);
      if (!match) return null;
      return { line: index + 1, date: match[1], ageDays: daysBetween(todayText, match[1]), text: match[2].slice(0, 140) };
    })
    .filter(Boolean);
}

function runDaily() {
  const todayText = localDateOnly();
  const changes = [];
  const details = {};

  const profile = cleanupExpiredScheduleLines(readText("memory/user-profile.md"), todayText);
  details.expiredScheduleLines = profile.removed.length;
  maybeWrite("memory/user-profile.md", profile.text, changes, "remove expired dated schedule lines");

  const recent = trimRecentContext(readText("memory/recent-context.md"), MAX_RECENT_ENTRIES);
  details.trimmedRecentEntries = recent.removed;
  maybeWrite("memory/recent-context.md", recent.text, changes, `keep latest ${MAX_RECENT_ENTRIES} recent-context entries`);

  const handoff = trimSessionHandoff(readText("memory/session-handoff.md"), MAX_HANDOFF_SECTIONS);
  details.trimmedHandoffSections = handoff.removed;
  maybeWrite("memory/session-handoff.md", handoff.text, changes, `keep latest ${MAX_HANDOFF_SECTIONS} session handoff sections`);

  const followups = archiveStaleFollowups(
    readText("memory/pending-followups.md"),
    readText("memory/followups-archive.md"),
    todayText,
    FOLLOWUP_TTL_DAYS,
  );
  details.archivedFollowups = followups.archived.length;
  maybeWrite("memory/pending-followups.md", followups.pending, changes, "remove stale followups from active list");
  maybeWrite("memory/followups-archive.md", followups.archive, changes, "append stale followups to archive");

  return {
    ok: true,
    mode: "daily",
    dryRun,
    today: todayText,
    details,
    changedFiles: changes.filter((item) => item.changed).map((item) => item.file),
    changes,
  };
}

function renderWeeklyReport(dailyResult) {
  const todayText = localDateOnly();
  const stats = [
    "memory/user-profile.md",
    "memory/learned-facts.md",
    "memory/pending-followups.md",
    "memory/recent-context.md",
    "memory/session-handoff.md",
  ].map(fileStats);
  const duplicates = [
    ...duplicateBullets("memory/user-profile.md").map((item) => ({ file: "memory/user-profile.md", ...item })),
    ...duplicateBullets("memory/learned-facts.md").map((item) => ({ file: "memory/learned-facts.md", ...item })),
  ];
  const staleFindings = stalePatternFindings();
  const pending = pendingAges(todayText);

  const lines = [
    "# Memory Optimization Report",
    "",
    `Generated: ${new Date().toISOString()}`,
    `Dry run: ${dryRun ? "yes" : "no"}`,
    "",
    "## Daily Safe Cleanup",
    "",
    `- Expired schedule lines: ${dailyResult.details.expiredScheduleLines}`,
    `- Trimmed recent-context entries: ${dailyResult.details.trimmedRecentEntries}`,
    `- Trimmed session-handoff sections: ${dailyResult.details.trimmedHandoffSections}`,
    `- Archived stale followups: ${dailyResult.details.archivedFollowups}`,
    "",
    "## File Sizes",
    "",
    ...stats.map((item) => `- ${item.file}: ${item.lines} lines, ${item.chars} chars`),
    "",
    "## Active Followups",
    "",
    ...(pending.length ? pending.map((item) => `- line ${item.line}: ${item.ageDays} days old — ${item.text}`) : ["- none"]),
    "",
    "## Duplicate Long-Term Bullets",
    "",
    ...(duplicates.length
      ? duplicates.slice(0, 20).map((item) => `- ${item.file} lines ${item.lines.join(", ")}: ${item.line.slice(0, 140)}`)
      : ["- none found"]),
    "",
    "## Stale Pattern Findings",
    "",
    ...(staleFindings.length
      ? staleFindings.slice(0, 30).map((item) => `- ${item.file}:${item.line} [${item.rule}] ${item.text}`)
      : ["- none found"]),
    "",
    "## Notes",
    "",
    "- This report does not rewrite user-profile.md or learned-facts.md semantic content.",
    "- Review duplicate/stale findings manually before changing durable preferences or rules.",
    "",
  ];
  return `${lines.join("\n")}\n`;
}

function runWeekly() {
  const dailyResult = runDaily();
  const report = renderWeeklyReport(dailyResult);
  const reportPath = "memory/memory-optimization-report.md";
  const changes = [...dailyResult.changes];
  maybeWrite(reportPath, report, changes, "write weekly memory optimization report");
  return {
    ...dailyResult,
    mode: "weekly",
    report: path.join(projectRoot, reportPath),
    changedFiles: changes.filter((item) => item.changed).map((item) => item.file),
    changes,
  };
}

function main() {
  fs.mkdirSync(tmpRoot, { recursive: true });
  let result;
  if (mode === "daily") result = runDaily();
  else if (mode === "weekly" || mode === "report") result = runWeekly();
  else {
    throw new Error(`unknown mode: ${mode}`);
  }
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

try {
  main();
} catch (error) {
  process.stdout.write(`${JSON.stringify({
    ok: false,
    mode,
    dryRun,
    error: error instanceof Error ? error.message : String(error),
  }, null, 2)}\n`);
  process.exitCode = 1;
}
