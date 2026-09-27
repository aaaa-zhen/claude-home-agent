#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const profilePath = path.join(projectRoot, "memory", "user-profile.md");
const dryRun = process.argv.includes("--dry-run");

function localDateOnly(date = new Date()) {
  const offsetMs = -date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() + offsetMs).toISOString().slice(0, 10);
}

function parseDateOnly(value) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day);
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
      const hasExpiredDate = dates.some((dateText) => isExpiredDate(dateText, todayText));
      if (hasExpiredDate) {
        removed.push(line);
        continue;
      }
    }
    output.push(line);
  }

  return { text: output.join("\n"), removed };
}

function main() {
  const todayText = localDateOnly();
  const before = fs.readFileSync(profilePath, "utf8");
  const result = cleanupExpiredScheduleLines(before, todayText);

  if (!dryRun && result.text !== before) fs.writeFileSync(profilePath, result.text, "utf8");

  process.stdout.write(`${JSON.stringify({
    ok: true,
    dryRun,
    today: todayText,
    file: profilePath,
    removedLines: result.removed.length,
  }, null, 2)}\n`);
}

main();
