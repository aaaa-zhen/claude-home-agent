#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const previewRoot = path.join(projectRoot, "projects", "previews");
const manifestPath = path.join(previewRoot, "manifest.json");
const defaultBaseUrl = process.env.PREVIEW_PUBLIC_BASE_URL || "https://your-api-domain.example.com/preview";

const args = process.argv.slice(2);
const opts = {
  source: "",
  slug: "",
  title: "",
  baseUrl: defaultBaseUrl,
  json: false,
};

for (let i = 0; i < args.length; i += 1) {
  const arg = args[i];
  if (arg === "--source") opts.source = args[++i] || "";
  else if (arg === "--slug") opts.slug = args[++i] || "";
  else if (arg === "--title") opts.title = args[++i] || "";
  else if (arg === "--base-url") opts.baseUrl = args[++i] || "";
  else if (arg === "--json") opts.json = true;
  else if (!opts.source) opts.source = arg;
}

function usage() {
  return [
    "Usage:",
    "  node scripts/publish-preview.mjs --source <html-file-or-built-dir> [--slug name] [--title title]",
    "",
    "Examples:",
    "  node scripts/publish-preview.mjs tmp/demo.html --slug demo",
    "  node scripts/publish-preview.mjs /path/to/react-app/dist --slug app-demo",
  ].join("\n");
}

function slugify(input) {
  const raw = String(input || "preview").trim().toLowerCase();
  const ascii = raw
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
  return ascii || `preview-${new Date().toISOString().slice(0, 10)}`;
}

function uniqueSlug(base) {
  let slug = base;
  let i = 2;
  while (fs.existsSync(path.join(previewRoot, slug))) {
    slug = `${base}-${i}`;
    i += 1;
  }
  return slug;
}

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

function copyDir(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name === ".git") continue;
    const from = path.join(src, entry.name);
    const to = path.join(dest, entry.name);
    if (entry.isDirectory()) copyDir(from, to);
    else if (entry.isFile()) fs.copyFileSync(from, to);
  }
}

function publishSource(source, dest) {
  const stat = fs.statSync(source);
  fs.rmSync(dest, { recursive: true, force: true });
  fs.mkdirSync(dest, { recursive: true });
  if (stat.isDirectory()) {
    const indexPath = path.join(source, "index.html");
    if (!fs.existsSync(indexPath)) {
      throw new Error(`directory source must contain index.html: ${source}`);
    }
    copyDir(source, dest);
    return;
  }
  if (stat.isFile()) {
    const ext = path.extname(source).toLowerCase();
    if (ext !== ".html" && ext !== ".htm") {
      throw new Error("file source must be .html/.htm, or pass a directory containing index.html");
    }
    fs.copyFileSync(source, path.join(dest, "index.html"));
    return;
  }
  throw new Error(`unsupported source: ${source}`);
}

function buildUrl(baseUrl, slug, token) {
  return `${String(baseUrl).replace(/\/+$/, "")}/${encodeURIComponent(slug)}/?k=${encodeURIComponent(token)}`;
}

function main() {
  if (!opts.source) throw new Error(`${usage()}\n\nMissing --source`);
  const source = path.resolve(opts.source);
  if (!fs.existsSync(source)) throw new Error(`source not found: ${source}`);

  fs.mkdirSync(previewRoot, { recursive: true });
  const sourceBase = fs.statSync(source).isDirectory() ? path.basename(source) : path.basename(source, path.extname(source));
  const slug = uniqueSlug(slugify(opts.slug || sourceBase));
  const token = crypto.randomBytes(24).toString("base64url");
  const dest = path.join(previewRoot, slug, "public");

  publishSource(source, dest);

  const manifest = readManifest();
  manifest.previews ||= {};
  manifest.previews[slug] = {
    slug,
    token,
    title: opts.title || slug,
    source,
    publicDir: dest,
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
  writeManifest(manifest);

  const url = buildUrl(opts.baseUrl, slug, token);
  const result = { ok: true, slug, title: manifest.previews[slug].title, publicDir: dest, url };
  if (opts.json) process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  else process.stdout.write(`Published ${slug}\n${url}\n`);
}

try {
  main();
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  if (opts.json) process.stdout.write(`${JSON.stringify({ ok: false, error: message }, null, 2)}\n`);
  else process.stderr.write(`${message}\n`);
  process.exitCode = 1;
}
