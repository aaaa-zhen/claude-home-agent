#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { sendText } from "../weixin-send.mjs";

const args = process.argv.slice(2);
let id = "";
let message = "";

for (let i = 0; i < args.length; i += 1) {
  if (args[i] === "--id") id = args[++i] || "";
  else if (args[i] === "--message") message = args[++i] || "";
}

if (!id || !message) {
  console.error("Usage: send-once-reminder.mjs --id <id> --message <message>");
  process.exit(2);
}

await sendText(message);

try {
  const current = execFileSync("crontab", ["-l"], { encoding: "utf8" });
  const updated = current
    .split("\n")
    .filter((line) => !line.includes(`--id ${id}`))
    .join("\n")
    .trimEnd() + "\n";
  execFileSync("crontab", ["-"], { input: updated });
} catch {
  // Sending the reminder is the important part. Cron cleanup can be retried manually.
}
