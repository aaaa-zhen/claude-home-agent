#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const previewRoot = path.join(projectRoot, "projects", "previews");
const manifestPath = path.join(previewRoot, "manifest.json");
const baseUrl = process.env.PREVIEW_PUBLIC_BASE_URL || "https://your-api-domain.example.com/preview";

const args = process.argv.slice(2);
const command = args[0] || "list";
const jsonOutput = args.includes("--json");
const slug = (() => {
  const index = args.indexOf("--slug");
  if (index >= 0) return args[index + 1] || "";
  return args[1] && !args[1].startsWith("--") ? args[1] : "";
})();

function readManifest() {
  try {
    return JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  } catch {
    return { previews: {} };
  }
}

function writeManifest(manifest) {
  fs.mkdirSync(previewRoot, { recursive: true });
  fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
}

function previewUrl(item) {
  return `${baseUrl.replace(/\/+$/, "")}/${encodeURIComponent(item.slug)}/?k=${encodeURIComponent(item.token)}`;
}

function output(data) {
  if (jsonOutput) {
    process.stdout.write(`${JSON.stringify(data, null, 2)}\n`);
    return;
  }
  if (Array.isArray(data.items)) {
    if (!data.items.length) {
      process.stdout.write("No previews.\n");
      return;
    }
    for (const item of data.items) {
      process.stdout.write(`${item.slug}\t${item.title || ""}\t${item.createdAt || ""}\n${item.url}\n`);
    }
    return;
  }
  process.stdout.write(`${JSON.stringify(data, null, 2)}\n`);
}

function requireSlug() {
  if (!slug) throw new Error(`Missing slug. Usage: node scripts/preview-admin.mjs ${command} --slug <slug>`);
}

function main() {
  const manifest = readManifest();
  manifest.previews ||= {};

  if (command === "list") {
    const items = Object.values(manifest.previews)
      .sort((a, b) => String(b.updatedAt || b.createdAt).localeCompare(String(a.updatedAt || a.createdAt)))
      .map((item) => ({ ...item, url: previewUrl(item) }));
    output({ ok: true, items });
    return;
  }

  if (command === "rotate") {
    requireSlug();
    const item = manifest.previews[slug];
    if (!item) throw new Error(`preview not found: ${slug}`);
    item.token = crypto.randomBytes(24).toString("base64url");
    item.updatedAt = new Date().toISOString();
    writeManifest(manifest);
    output({ ok: true, preview: { ...item, url: previewUrl(item) } });
    return;
  }

  if (command === "remove" || command === "delete") {
    requireSlug();
    const item = manifest.previews[slug];
    if (!item) throw new Error(`preview not found: ${slug}`);
    delete manifest.previews[slug];
    writeManifest(manifest);
    const dir = path.join(previewRoot, slug);
    if (!args.includes("--keep-files")) fs.rmSync(dir, { recursive: true, force: true });
    output({ ok: true, removed: slug });
    return;
  }

  throw new Error("Usage: node scripts/preview-admin.mjs [list|rotate|remove] [--slug slug] [--json]");
}

try {
  main();
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  if (jsonOutput) process.stdout.write(`${JSON.stringify({ ok: false, error: message }, null, 2)}\n`);
  else process.stderr.write(`${message}\n`);
  process.exitCode = 1;
}
