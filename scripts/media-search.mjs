#!/usr/bin/env node
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const projectRoot = path.resolve(new URL("..", import.meta.url).pathname);
const primaryIndex = path.join(projectRoot, "media", "index.jsonl");
const legacyIndex = path.join(os.homedir(), ".openclaw", "openclaw-weixin", "media-archive", "index.jsonl");

const args = process.argv.slice(2);
const opts = {
  type: "",
  q: "",
  limit: 20,
  indexes: [primaryIndex],
  includeMissing: false,
};

for (let i = 0; i < args.length; i += 1) {
  const arg = args[i];
  if (arg === "--type") opts.type = args[++i] ?? "";
  else if (arg === "--q") opts.q = args[++i] ?? "";
  else if (arg === "--limit") opts.limit = Number(args[++i] ?? "20");
  else if (arg === "--index") opts.indexes = [args[++i] ?? ""];
  else if (arg === "--include-missing") opts.includeMissing = true;
  else if (arg === "--include-legacy") opts.indexes.push(legacyIndex);
}

function readJsonl(filePath) {
  if (!filePath || !fs.existsSync(filePath)) return [];
  return fs.readFileSync(filePath, "utf8")
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return null;
      }
    })
    .filter(Boolean);
}

function normalize(record) {
  const bucket = record.bucket
    || (record.type === "image" ? "images" : record.type === "video" ? "videos" : record.type === "audio" ? "audio" : "files");
  return {
    id: record.id || record.mediaUri?.replace(/^media:\/\//, "") || record.messageId || record.requestId || record.filePath,
    savedAt: record.savedAt || "",
    source: record.source || "",
    messageId: record.messageId || null,
    type: record.type || "",
    bucket,
    mimeType: record.mimeType || "",
    filePath: record.filePath || "",
    exists: record.filePath ? fs.existsSync(record.filePath) : false,
    mediaUri: record.mediaUri || "",
    text: record.caption || record.text || "",
    sizeBytes: record.sizeBytes ?? null,
  };
}

function dedupeKey(record) {
  if (record.messageId) return `message:${record.messageId}`;
  if (record.savedAt && record.filePath) return `time-file:${record.savedAt}:${path.basename(record.filePath)}`;
  return `id:${record.id}`;
}

const q = opts.q.trim().toLowerCase();
const type = opts.type.trim().toLowerCase();
const rows = opts.indexes
  .flatMap(readJsonl)
  .map(normalize)
  .filter((record) => {
    if (!opts.includeMissing && !record.exists) return false;
    if (type && record.type !== type && record.bucket !== type && record.bucket !== `${type}s`) return false;
    if (!q) return true;
    return [record.id, record.savedAt, record.type, record.bucket, record.mimeType, record.filePath, record.mediaUri, record.text]
      .join("\n")
      .toLowerCase()
      .includes(q);
  })
  .sort((a, b) => String(b.savedAt).localeCompare(String(a.savedAt)))
  .filter((record, _index, all) => {
    const key = dedupeKey(record);
    const first = all.findIndex((candidate) => dedupeKey(candidate) === key);
    return all[first] === record;
  })
  .slice(0, Number.isFinite(opts.limit) && opts.limit > 0 ? opts.limit : 20);

process.stdout.write(`${JSON.stringify(rows, null, 2)}\n`);
