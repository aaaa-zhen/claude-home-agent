#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const activeFiles = [
  "AGENTS.md",
  "memory/index.md",
  "memory/user-profile.md",
  "memory/learned-facts.md",
  "memory/devices.md",
  "memory/recent-context.md",
  "memory/session-handoff.md",
  "memory/pending-followups.md",
  "memory/skills/ha-call-service.md",
  "memory/skills/ha-create-automation.md",
  "memory/skills/publish-preview.md",
  "memory/skills/zigbee2mqtt-rename-device.md",
];

const blockedPatterns = [
  { name: "old-linux-home", pattern: /\/home\/ubuntu/i },
  { name: "ubuntu-crontab", pattern: /ubuntu\s+crontab/i },
  { name: "systemd-runtime", pattern: /\bsystemd\b/i },
  { name: "journalctl-runtime", pattern: /\bjournalctl\b/i },
  { name: "windows-drive", pattern: /\b[A-Z]:\\/ },
  { name: "windows-c-drive-note", pattern: /C\s*盘/ },
  { name: "wrong-car", pattern: /领克08/ },
  { name: "removed-teacher", pattern: /Mia Lawrence|Georgia（高加索国家）/i },
  { name: "old-vps-current", pattern: /腾讯云|东京\s*VPS|Linux\s*VPS/i },
];

function scanFile(relativePath) {
  const filePath = path.join(projectRoot, relativePath);
  if (!fs.existsSync(filePath)) return [];
  const lines = fs.readFileSync(filePath, "utf8").split(/\r?\n/);
  const findings = [];
  lines.forEach((line, index) => {
    for (const rule of blockedPatterns) {
      if (rule.pattern.test(line)) {
        findings.push({
          file: relativePath,
          line: index + 1,
          rule: rule.name,
          text: line.trim(),
        });
      }
    }
  });
  return findings;
}

const findings = activeFiles.flatMap(scanFile);
const result = {
  ok: findings.length === 0,
  checkedFiles: activeFiles.length,
  findings,
};

process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
if (findings.length) process.exitCode = 1;
