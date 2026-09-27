#!/usr/bin/env node
/**
 * memory-apply.mjs — 长期记忆决策执行器。
 *
 * 蒸馏器(LLM)只输出决策 JSON，不再直接改文件；所有写入由本脚本执行。
 * 好处：id 由脚本分配、target 必须真实存在、批次要么全成功要么一条不写。
 *
 * 事实行格式（learned-facts.md）：
 *   - [2026-07-17] {f0142 user high} **打车默认选最便宜的车型** … `#行为`
 *   标记 {id 来源 置信度 [seen:N]}，紧跟日期。没有标记的旧行会被自动补齐。
 * 被取代的事实不留在 learned-facts.md，整行搬到 learned-facts-archive.md 并打
 *   {f0088 infer med superseded→f0231} —— 因为 agent 是直接读整个文件的，
 *   留在原文件里等于没有 supersede。
 * 弱证据（infer/cron 来源，或 low 置信度）不直接生效，先落 learned-facts-pending.md，
 *   等 seen≥2 或来源升级成 user/correct/tool 才晋升；30 天没等到佐证就过期进 archive。
 *
 * 用法：
 *   node scripts/memory-apply.mjs --context            # 打印给蒸馏器的上下文 JSON（会顺带补齐 id）
 *   node scripts/memory-apply.mjs --pending-reviews    # 还欠用户哪些确认（会话启动时读）
 *   node scripts/memory-apply.mjs --stdin              # 从 stdin 读决策 JSON 并执行
 *   node scripts/memory-apply.mjs --file <path>        # 重放一个被拒绝的批次
 *   node scripts/memory-apply.mjs --backfill           # 只补 id，不执行决策
 *   附加：--dry-run  --root <dir>  --quiet
 *
 * 退出码：0 成功 / 2 批次被拒绝（一条都没写）/ 1 用法错误
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const argv = process.argv.slice(2);

function flagValue(name, fallback = null) {
  const i = argv.indexOf(name);
  if (i >= 0 && argv[i + 1] && !argv[i + 1].startsWith("--")) return argv[i + 1];
  return fallback;
}

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(flagValue("--root") || path.join(scriptDir, ".."));
const memoryRoot = path.join(projectRoot, "memory");
const dryRun = argv.includes("--dry-run");
const quiet = argv.includes("--quiet");

const FACTS = "learned-facts.md";
const PENDING = "learned-facts-pending.md";
const ARCHIVE = "learned-facts-archive.md";
const SUMMARY = "conversation-summary.md";
const REVIEW = "review-queue.md";
const APPLY_LOG = "apply-log.md";
const PROFILE = "user-profile.md";
// 人生大事追加进 user-profile 的这一节（只增不改、去重）。蒸馏器不直接编辑
// user-profile，只通过结构化的 life_events 字段喂进来，由本脚本确定性落盘。
const PROFILE_LIFE_SECTION = "近期重大动态";

const SOURCES = ["user", "correct", "tool", "infer", "cron", "unknown"];
const CONFIDENCES = ["high", "med", "low"];
const DECISIONS = ["add", "update", "merge", "add_evidence", "skip_duplicate", "keep_both", "review"];

// 批次上限：LLM 跑飞时的兜底，不是业务约束。
const MAX_SUMMARY_LINES = 5;
const MAX_LIFE_EVENTS = 3;
const MAX_DECISIONS = 12;
const MAX_FACT_CHARS = 500;
const APPLY_LOG_KEEP = 500;

// 给蒸馏器的上下文裁剪参数。
const CONTEXT_RECENT_ENTRIES = 40;
const CONTEXT_SUMMARY_LINES = 30;

// 待裁决条目超过这个天数就不再主动问用户，避免翻旧账（和 pending-followups 同口径）。
const REVIEW_STALE_DAYS = 14;

// 候选事实等不到佐证就过期，避免推测无限期堆着。
const PENDING_TTL_DAYS = 30;

const REVIEW_LINE = /^- \[([ xX])\]\s+(r\d{4,})\s+\[(\d{4}-\d{2}-\d{2})\]\s*(.*)$/;
const REVIEW_ID_RE = /^r\d{4,}$/;
const REVIEW_HIGH_WATER = /^<!-- review-id-high-water: (\d+) -->$/;

const FACT_LINE = /^- \[(\d{4}-\d{2}-\d{2})\]\s*(?:\{([^}]*)\}\s*)?(.*)$/;
const SECTION_LINE = /^##\s+(.+?)\s*$/;
const ID_RE = /^f\d{4,}$/;

// ---------------------------------------------------------------- io helpers

function filePath(name) {
  return path.join(memoryRoot, name);
}

function readText(name, fallback = "") {
  try {
    return fs.readFileSync(filePath(name), "utf8");
  } catch {
    return fallback;
  }
}

function writeTextAtomic(name, text) {
  const target = filePath(name);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const tmp = `${target}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(tmp, text, "utf8");
  fs.renameSync(tmp, target);
}

function backupDir() {
  return path.join(projectRoot, "tmp", "memory-apply");
}

function backup(name, runId) {
  const source = filePath(name);
  if (!fs.existsSync(source)) return "";
  const dir = path.join(backupDir(), "backups", runId);
  fs.mkdirSync(dir, { recursive: true });
  const target = path.join(dir, name.replace(/[\\/]/g, "__"));
  fs.copyFileSync(source, target);
  return target;
}

function localDate(date = new Date()) {
  const offset = -date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() + offset).toISOString().slice(0, 10);
}

function localMinute(date = new Date()) {
  const offset = -date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() + offset).toISOString().slice(0, 16).replace("T", " ");
}

// ------------------------------------------------------------ fact file model

/**
 * 把 learned-facts.md 解析成行数组：事实行结构化，其余原样保留。
 * 只认 `- [YYYY-MM-DD]` 开头的行是事实，别的（说明、案例小节的 `- 现象：`）不碰。
 */
function parseFactFile(text) {
  const rows = [];
  let section = null;
  for (const raw of text.split(/\r?\n/)) {
    const sectionMatch = raw.match(SECTION_LINE);
    if (sectionMatch) {
      section = sectionMatch[1];
      rows.push({ kind: "raw", raw });
      continue;
    }
    const factMatch = raw.match(FACT_LINE);
    if (factMatch) {
      rows.push({
        kind: "fact",
        section,
        date: factMatch[1],
        marker: parseMarker(factMatch[2]),
        text: factMatch[3],
      });
      continue;
    }
    rows.push({ kind: "raw", raw });
  }
  return rows;
}

function parseMarker(body) {
  const marker = { id: null, source: "unknown", confidence: "med", seen: 1, supersededBy: null };
  if (!body) return marker;
  for (const token of body.trim().split(/\s+/)) {
    if (!token) continue;
    if (ID_RE.test(token) && !marker.id) marker.id = token;
    else if (SOURCES.includes(token)) marker.source = token;
    else if (CONFIDENCES.includes(token)) marker.confidence = token;
    else if (token.startsWith("seen:")) {
      const n = Number(token.slice(5));
      if (Number.isInteger(n) && n > 0) marker.seen = n;
    } else if (/^superseded(→|->)/.test(token)) {
      marker.supersededBy = token.replace(/^superseded(→|->)/, "");
    }
  }
  return marker;
}

function renderMarker(marker) {
  const parts = [marker.id, marker.source, marker.confidence];
  if (marker.seen > 1) parts.push(`seen:${marker.seen}`);
  if (marker.supersededBy) parts.push(`superseded→${marker.supersededBy}`);
  return `{${parts.join(" ")}}`;
}

function renderFact(row) {
  return `- [${row.date}] ${renderMarker(row.marker)} ${row.text}`.trimEnd();
}

function renderFactFile(rows) {
  const body = rows.map((row) => (row.kind === "fact" ? renderFact(row) : row.raw)).join("\n");
  return body.endsWith("\n") ? body : `${body}\n`;
}

/** 分类小节名。`## [2026-07-13] 某故障` 这种案例小节不算可写入的分类。 */
function sectionNames(rows) {
  return rows
    .filter((row) => row.kind === "raw" && SECTION_LINE.test(row.raw))
    .map((row) => row.raw.match(SECTION_LINE)[1])
    .filter((name) => !name.startsWith("["));
}

function collectIds(...rowLists) {
  const ids = new Set();
  for (const rows of rowLists) {
    for (const row of rows) {
      if (row.kind === "fact" && row.marker.id) ids.add(row.marker.id);
    }
  }
  return ids;
}

function makeIdAllocator(usedIds) {
  let next = 1;
  for (const id of usedIds) {
    const n = Number(id.slice(1));
    if (Number.isInteger(n) && n >= next) next = n + 1;
  }
  return () => {
    const id = `f${String(next).padStart(4, "0")}`;
    next += 1;
    return id;
  };
}

/** 给没有标记的旧事实行补 id。纯增量，不动正文。 */
function backfillIds(rows, allocId) {
  let filled = 0;
  for (const row of rows) {
    if (row.kind !== "fact" || row.marker.id) continue;
    row.marker.id = allocId();
    filled += 1;
  }
  return filled;
}

function loadWorld() {
  const factRows = parseFactFile(readText(FACTS));
  const pendingRows = parseFactFile(readText(PENDING, pendingHeader()));
  const archiveRows = parseFactFile(readText(ARCHIVE, archiveHeader()));
  const allocId = makeIdAllocator(collectIds(factRows, pendingRows, archiveRows));
  const filled =
    backfillIds(factRows, allocId) + backfillIds(pendingRows, allocId) + backfillIds(archiveRows, allocId);
  return { factRows, pendingRows, archiveRows, allocId, filled };
}

function archiveHeader() {
  return [
    "# 已取代的长期事实",
    "",
    "被新事实取代的旧规则从 `learned-facts.md` 搬到这里，保留审计痕迹。",
    "**这个文件不参与日常判断**，只有排查“它为什么以前是那样做的”时才看。",
    "",
    "---",
    "",
  ].join("\n");
}

function pendingHeader() {
  return [
    "# 待确认的候选事实",
    "",
    "模型推断出来、还没被证实的东西先放这里，**不是生效的规则，不要拿来做判断**。",
    `升级到 \`learned-facts.md\` 需要满足其一：再次被独立印证(seen≥2)、被工具验证、或用户确认。`,
    `${PENDING_TTL_DAYS} 天内没等到佐证的候选会过期，移进 \`learned-facts-archive.md\`。`,
    "",
    "---",
    "",
  ].join("\n");
}

/**
 * 弱证据：模型自己推断的、定时推送里读来的、或者自己都说没把握的。
 * 这类东西不直接进生效规则，先当候选放着等佐证。
 */
function isWeakFact(source, confidence) {
  return source === "infer" || source === "cron" || confidence === "low";
}

function indexById(...rowLists) {
  const map = new Map();
  for (const rows of rowLists) {
    for (const row of rows) {
      if (row.kind === "fact" && row.marker.id) map.set(row.marker.id, row);
    }
  }
  return map;
}

// ------------------------------------------------------------- review queue

function reviewHeader() {
  return [
    "# 记忆待裁决队列",
    "",
    "蒸馏器判不准的记忆冲突排在这里。**下次和用户聊天时顺口问一句确认**，",
    "拿到答复后按结论提交决策(带 resolves)，本条会自动出队。不要堆着不处理。",
    "",
    "---",
    "",
  ].join("\n");
}

/**
 * 队列行结构化，其余(标题、说明)原样保留。
 * 高水位标记单独抽出来：条目出队是直接删行，光看剩下的行会把号发重，
 * 而 apply-log 里的 rNNNN 引用必须永远唯一。
 */
function parseReviewQueue(text) {
  const rows = [];
  let highWater = 0;
  for (const raw of text.split(/\r?\n/)) {
    const marker = raw.match(REVIEW_HIGH_WATER);
    if (marker) {
      const n = Number(marker[1]);
      if (Number.isInteger(n) && n > highWater) highWater = n;
      continue;
    }
    const match = raw.match(REVIEW_LINE);
    if (match) {
      rows.push({ kind: "review", done: match[1] !== " ", id: match[2], date: match[3], text: match[4] });
      const n = Number(match[2].slice(1));
      if (Number.isInteger(n) && n > highWater) highWater = n;
      continue;
    }
    rows.push({ kind: "raw", raw });
  }
  return { rows, highWater };
}

function renderReviewQueue(rows, highWater) {
  const body = rows
    .map((row) => (row.kind === "review" ? `- [${row.done ? "x" : " "}] ${row.id} [${row.date}] ${row.text}` : row.raw))
    .join("\n")
    .replace(/\n*$/, "\n");
  return `${body}\n<!-- review-id-high-water: ${highWater} -->\n`;
}

function loadReviewQueue() {
  const text = readText(REVIEW, reviewHeader());
  const parsed = parseReviewQueue(text.trim() ? text : reviewHeader());
  const queue = { rows: parsed.rows, highWater: parsed.highWater };
  queue.allocId = () => {
    queue.highWater += 1;
    return `r${String(queue.highWater).padStart(4, "0")}`;
  };
  return queue;
}

function daysSince(dateText, today) {
  const [ay, am, ad] = dateText.split("-").map(Number);
  const [by, bm, bd] = today.split("-").map(Number);
  const ms = new Date(by, bm - 1, bd).getTime() - new Date(ay, am - 1, ad).getTime();
  return Math.round(ms / 86_400_000);
}

// -------------------------------------------------------------- batch parsing

function stripCodeFence(text) {
  const trimmed = text.trim();
  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fenced) return fenced[1].trim();
  // 模型偶尔会在 JSON 前后带一句话，取最外层大括号。
  const first = trimmed.indexOf("{");
  const last = trimmed.lastIndexOf("}");
  if (first >= 0 && last > first) return trimmed.slice(first, last + 1);
  return trimmed;
}

class Rejected extends Error {
  constructor(reason) {
    super(reason);
    this.reason = reason;
  }
}

/**
 * 模型写中文时很容易在字符串值里直接用裸 ASCII 引号（说"打开厨房空调"），
 * 那不是合法 JSON。扫一遍：字符串内部遇到的引号，只有后面跟着 , : } ] 或结尾
 * 才算收尾，否则当正文转义掉。只在严格解析失败后才走这条路。
 */
function repairBareQuotes(text) {
  let out = "";
  let inString = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (!inString) {
      out += ch;
      if (ch === '"') inString = true;
      continue;
    }
    if (ch === "\\") {
      out += ch + (text[i + 1] ?? "");
      i += 1;
      continue;
    }
    if (ch === '"') {
      let j = i + 1;
      while (j < text.length && /\s/.test(text[j])) j += 1;
      const next = text[j];
      if (next === undefined || next === "," || next === ":" || next === "}" || next === "]") {
        out += ch;
        inString = false;
      } else {
        out += '\\"';
      }
      continue;
    }
    out += ch;
  }
  return out;
}

function parseBatch(raw) {
  const body = stripCodeFence(raw);
  let parsed;
  let repaired = false;
  try {
    parsed = JSON.parse(body);
  } catch (firstErr) {
    try {
      parsed = JSON.parse(repairBareQuotes(body));
      repaired = true;
    } catch {
      throw new Rejected(`json_parse_failed: ${firstErr.message}`);
    }
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Rejected("batch_not_object");
  }
  const summary = parsed.summary ?? [];
  const decisions = parsed.decisions ?? [];
  const lifeEvents = parsed.life_events ?? [];
  if (!Array.isArray(summary)) throw new Rejected("summary_not_array");
  if (!Array.isArray(decisions)) throw new Rejected("decisions_not_array");
  if (!Array.isArray(lifeEvents)) throw new Rejected("life_events_not_array");
  if (summary.length > MAX_SUMMARY_LINES) throw new Rejected(`summary_over_cap: ${summary.length}`);
  if (lifeEvents.length > MAX_LIFE_EVENTS) throw new Rejected(`life_events_over_cap: ${lifeEvents.length}`);
  if (decisions.length > MAX_DECISIONS) throw new Rejected(`decisions_over_cap: ${decisions.length}`);
  return { summary, decisions, lifeEvents, repaired };
}

// ---------------------------------------------------------------- validation

function requireString(value, field, index) {
  if (typeof value !== "string" || !value.trim()) {
    throw new Rejected(`decision[${index}].${field}_missing`);
  }
  return value.trim();
}

function requireFactText(value, field, index) {
  const text = requireString(value, field, index);
  if (text.includes("\n")) throw new Rejected(`decision[${index}].${field}_multiline`);
  if (text.length > MAX_FACT_CHARS) throw new Rejected(`decision[${index}].${field}_too_long`);
  return text;
}

function requireEnum(value, allowed, field, index, fallback = null) {
  if (value === undefined || value === null || value === "") {
    if (fallback !== null) return fallback;
    throw new Rejected(`decision[${index}].${field}_missing`);
  }
  if (!allowed.includes(value)) throw new Rejected(`decision[${index}].${field}_invalid: ${value}`);
  return value;
}

function requireActiveTarget(id, byId, field, index) {
  if (!ID_RE.test(id || "")) throw new Rejected(`decision[${index}].${field}_malformed: ${id}`);
  const row = byId.get(id);
  if (!row) throw new Rejected(`decision[${index}].${field}_unknown: ${id}`);
  if (row.marker.supersededBy) throw new Rejected(`decision[${index}].${field}_already_superseded: ${id}`);
  return row;
}

function requireIdList(value, field, index) {
  if (!Array.isArray(value) || value.length === 0) throw new Rejected(`decision[${index}].${field}_missing`);
  return value;
}

/**
 * 全批校验。任何一条不合法 → 抛 Rejected → 调用方一条都不写。
 * 这是 fail-closed 的关键：半批应用比不应用更糟，宁可整批重来。
 */
function validateBatch(batch, world, review) {
  const byId = indexById(world.factRows, world.pendingRows);
  const pendingIds = new Set(
    world.pendingRows.filter((row) => row.kind === "fact" && row.marker.id).map((row) => row.marker.id),
  );
  const sections = sectionNames(world.factRows);
  const openReviews = new Map();
  for (const row of review.rows) {
    if (row.kind === "review" && !row.done) openReviews.set(row.id, row);
  }
  const claimed = new Set();
  const claimedReviews = new Set();

  const plan = [];
  batch.decisions.forEach((decision, index) => {
    if (!decision || typeof decision !== "object") throw new Rejected(`decision[${index}]_not_object`);
    // 模型很容易把这个键写成 action。纯粹是键名叫法，没有歧义，收下。
    const kind = requireEnum(decision.decision ?? decision.action, DECISIONS, "decision", index);

    const claim = (id) => {
      if (claimed.has(id)) throw new Rejected(`decision[${index}].target_reused: ${id}`);
      claimed.add(id);
    };

    // 用户答复后，决策可以带 resolves 把待裁决条目一并出队。
    const resolves = [];
    if (decision.resolves !== undefined) {
      for (const id of requireIdList(decision.resolves, "resolves", index)) {
        if (!REVIEW_ID_RE.test(id || "")) throw new Rejected(`decision[${index}].resolves_malformed: ${id}`);
        const row = openReviews.get(id);
        if (!row) throw new Rejected(`decision[${index}].resolves_unknown_or_done: ${id}`);
        if (claimedReviews.has(id)) throw new Rejected(`decision[${index}].resolves_reused: ${id}`);
        claimedReviews.add(id);
        resolves.push(row);
      }
    }
    if (kind === "review" && resolves.length) throw new Rejected(`decision[${index}].review_cannot_resolve`);
    const withResolves = (step) => plan.push({ ...step, resolves });

    if (kind === "add" || kind === "update") {
      const text = requireFactText(decision.text, "text", index);
      const source = requireEnum(decision.source, SOURCES, "source", index);
      const confidence = requireEnum(decision.confidence, CONFIDENCES, "confidence", index);
      const supersedes = kind === "update" ? requireIdList(decision.supersedes, "supersedes", index) : [];
      const targets = supersedes.map((id) => {
        const row = requireActiveTarget(id, byId, "supersedes", index);
        claim(id);
        return row;
      });
      let section = decision.section;
      if (!section && targets.length) section = targets[0].section;
      section = requireString(section, "section", index);
      if (!sections.includes(section)) throw new Rejected(`decision[${index}].section_unknown: ${section}`);
      // 弱证据只能进候选区，绝不能拿去推翻一条已经生效的规则 —— 那种情况应该走 review。
      const weak = isWeakFact(source, confidence);
      if (weak && targets.some((target) => !pendingIds.has(target.marker.id))) {
        throw new Rejected(`decision[${index}].weak_cannot_supersede_active`);
      }
      withResolves({ kind, index, text, source, confidence, section, targets, weak });
      return;
    }

    if (kind === "merge") {
      const target = requireActiveTarget(requireString(decision.target_id, "target_id", index), byId, "target_id", index);
      claim(target.marker.id);
      withResolves({
        kind,
        index,
        target,
        text: requireFactText(decision.text, "text", index),
        source: requireEnum(decision.source, SOURCES, "source", index, target.marker.source),
        confidence: requireEnum(decision.confidence, CONFIDENCES, "confidence", index, target.marker.confidence),
      });
      return;
    }

    if (kind === "add_evidence") {
      const target = requireActiveTarget(requireString(decision.target_id, "target_id", index), byId, "target_id", index);
      claim(target.marker.id);
      withResolves({ kind, index, target, source: requireEnum(decision.source, SOURCES, "source", index, target.marker.source) });
      return;
    }

    if (kind === "skip_duplicate") {
      const target = requireActiveTarget(requireString(decision.target_id, "target_id", index), byId, "target_id", index);
      withResolves({ kind, index, target });
      return;
    }

    if (kind === "keep_both") {
      withResolves({ kind, index, rationale: decision.rationale || "" });
      return;
    }

    // review：不确定谁对的矛盾进人工队列，下次在微信里问用户。
    const conflictWith = requireIdList(decision.conflict_with, "conflict_with", index);
    for (const id of conflictWith) requireActiveTarget(id, byId, "conflict_with", index);
    withResolves({
      kind,
      index,
      conflictWith,
      rationale: requireString(decision.rationale, "rationale", index),
      text: typeof decision.text === "string" ? decision.text.trim() : "",
    });
  });

  const summary = [];
  batch.summary.forEach((line, index) => {
    if (typeof line !== "string" || !line.trim()) throw new Rejected(`summary[${index}]_not_string`);
    const trimmed = line.trim().replace(/^-\s*/, "");
    if (!/^\[\d{4}-\d{2}-\d{2}\]/.test(trimmed)) throw new Rejected(`summary[${index}]_bad_prefix`);
    if (trimmed.includes("\n")) throw new Rejected(`summary[${index}]_multiline`);
    summary.push(trimmed);
  });

  const lifeEvents = [];
  (batch.lifeEvents ?? []).forEach((line, index) => {
    if (typeof line !== "string" || !line.trim()) throw new Rejected(`life_events[${index}]_not_string`);
    const trimmed = line.trim().replace(/^-\s*/, "");
    if (!/^\[\d{4}-\d{2}-\d{2}\]/.test(trimmed)) throw new Rejected(`life_events[${index}]_bad_prefix`);
    if (trimmed.includes("\n")) throw new Rejected(`life_events[${index}]_multiline`);
    lifeEvents.push(trimmed);
  });

  return { plan, summary, lifeEvents };
}

// -------------------------------------------------------------------- applying

function insertAfterLastFactOfSection(rows, section, newRow, { createMissing = false } = {}) {
  let insertAt = -1;
  let inSection = false;
  for (let i = 0; i < rows.length; i += 1) {
    const row = rows[i];
    if (row.kind === "raw" && SECTION_LINE.test(row.raw)) {
      const name = row.raw.match(SECTION_LINE)[1];
      if (name === section) {
        inSection = true;
        insertAt = i + 1;
      } else if (inSection) {
        break;
      }
      continue;
    }
    if (inSection && row.kind === "fact") insertAt = i + 1;
  }
  if (insertAt < 0) {
    // 候选文件按需长出分类，和 learned-facts.md 的分类一一对应。
    if (!createMissing) throw new Rejected(`section_vanished: ${section}`);
    while (rows.length && rows[rows.length - 1].kind === "raw" && !rows[rows.length - 1].raw.trim()) rows.pop();
    rows.push({ kind: "raw", raw: "" }, { kind: "raw", raw: `## ${section}` }, { kind: "raw", raw: "" });
    insertAt = rows.length;
  }
  newRow.section = section;
  rows.splice(insertAt, 0, newRow);
}

function removeRow(world, row) {
  for (const rows of [world.factRows, world.pendingRows]) {
    const at = rows.indexOf(row);
    if (at >= 0) {
      rows.splice(at, 1);
      return rows;
    }
  }
  return null;
}

/**
 * 候选区的确定性维护，每次执行后都跑一遍（对应 omi 的 maintenance cron）。
 * 晋升条件：再次被独立印证(seen≥2) 或 来源升级成 user/correct/tool。
 * 过期条件：超过 PENDING_TTL_DAYS 还是孤证 —— 推测不能无限期赖着。
 */
function sweepPending(world, today) {
  const events = [];

  // agent 在对话里是直接写 learned-facts.md 的，绕过了入口闸门。
  // 把已经躺在生效区、但还是孤证的弱证据降级到候选，让闸门对那条路径也生效。
  for (const row of [...world.factRows]) {
    if (row.kind !== "fact" || !row.marker.id || row.marker.supersededBy) continue;
    if (!isWeakFact(row.marker.source, row.marker.confidence)) continue;
    if (row.marker.seen >= 2) continue;
    removeRow(world, row);
    insertAfterLastFactOfSection(world.pendingRows, row.section, row, { createMissing: true });
    events.push({
      kind: "demote",
      id: row.marker.id,
      reason: `${row.marker.source}/${row.marker.confidence}`,
      text: row.text,
    });
  }

  for (const row of [...world.pendingRows]) {
    if (row.kind !== "fact" || !row.marker.id || row.marker.supersededBy) continue;
    // 晋升条件必须和入口的弱证据判定用同一个谓词，否则 user/low 这种会
    // 前脚被判弱、后脚又因为 source=user 被捞回来，自相矛盾。
    const corroborated = row.marker.seen >= 2;
    const trusted = !isWeakFact(row.marker.source, row.marker.confidence);
    if (corroborated || trusted) {
      const section = row.section || sectionNames(world.factRows)[0];
      if (!section) continue; // 没有任何分类可落，留在候选区等下次
      removeRow(world, row);
      insertAfterLastFactOfSection(world.factRows, section, row, { createMissing: true });
      events.push({
        kind: "promote",
        id: row.marker.id,
        reason: trusted ? `source:${row.marker.source}` : `seen:${row.marker.seen}`,
        text: row.text,
      });
      continue;
    }
    if (daysSince(row.date, today) > PENDING_TTL_DAYS) {
      removeRow(world, row);
      row.marker.supersededBy = "expired";
      world.archiveRows.push({ ...row, section: null });
      events.push({ kind: "expire", id: row.marker.id, text: row.text });
    }
  }
  return events;
}

function applyPlan(plan, world, today) {
  const events = [];
  for (const step of plan) {
    // 被答复的待裁决条目在这里出队，日志里留痕。
    for (const row of step.resolves) {
      row.done = true;
      events.push({ kind: "resolved", id: row.id, text: row.text });
    }
    if (step.kind === "add" || step.kind === "update") {
      const id = world.allocId();
      const row = {
        kind: "fact",
        section: step.section,
        date: today,
        marker: { id, source: step.source, confidence: step.confidence, seen: 1, supersededBy: null },
        text: step.text,
      };
      if (step.weak) {
        insertAfterLastFactOfSection(world.pendingRows, step.section, row, { createMissing: true });
      } else {
        insertAfterLastFactOfSection(world.factRows, step.section, row);
      }
      for (const target of step.targets) {
        target.marker.supersededBy = id;
        removeRow(world, target);
        world.archiveRows.push({ ...target, section: null });
      }
      events.push({
        kind: step.kind,
        id,
        pending: step.weak,
        supersedes: step.targets.map((t) => t.marker.id),
        section: step.section,
        text: step.text,
        source: step.source,
        confidence: step.confidence,
      });
      continue;
    }

    if (step.kind === "merge") {
      step.target.text = step.text;
      step.target.date = today;
      step.target.marker.source = step.source;
      step.target.marker.confidence = step.confidence;
      step.target.marker.seen += 1;
      events.push({ kind: "merge", id: step.target.marker.id, text: step.text, seen: step.target.marker.seen });
      continue;
    }

    if (step.kind === "add_evidence") {
      step.target.marker.seen += 1;
      step.target.date = today;
      if (step.source !== "infer") step.target.marker.source = step.source;
      events.push({ kind: "add_evidence", id: step.target.marker.id, seen: step.target.marker.seen });
      continue;
    }

    if (step.kind === "skip_duplicate") {
      events.push({ kind: "skip_duplicate", id: step.target.marker.id });
      continue;
    }

    if (step.kind === "keep_both") {
      events.push({ kind: "keep_both", rationale: step.rationale });
      continue;
    }

    events.push({ kind: "review", conflictWith: step.conflictWith, rationale: step.rationale, text: step.text });
  }
  return events;
}

function appendSummaryLines(existingText, lines) {
  if (!lines.length) return { text: existingText, added: [] };
  const existing = new Set(
    existingText
      .split(/\r?\n/)
      .map((line) => line.trim().replace(/^-\s*/, ""))
      .filter(Boolean),
  );
  const added = lines.filter((line) => !existing.has(line));
  if (!added.length) return { text: existingText, added: [] };
  const base = existingText.endsWith("\n") ? existingText : `${existingText}\n`;
  return { text: `${base}${added.join("\n")}\n`, added };
}

// 人生大事追加进 user-profile 的「近期重大动态」节，只增不改、去重。
// 找到标题含 PROFILE_LIFE_SECTION 的那节，插到该节最后一个 `- ` 条目之后；
// 该节不存在就在文末新建。绝不重写或删除已有内容 —— profile 是手工维护的正本，
// 这里只做安全的 append。
function appendLifeEvents(existingText, lines) {
  if (!lines.length) return { text: existingText, added: [] };
  const bullets = lines.map((line) => `- ${line.replace(/^-\s*/, "")}`);
  const existing = new Set(
    existingText
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean),
  );
  const added = bullets.filter((b) => !existing.has(b.trim()));
  if (!added.length) return { text: existingText, added: [] };

  const rows = existingText.split(/\r?\n/);
  // 定位「近期重大动态」节的标题行。
  let headerIdx = -1;
  for (let i = 0; i < rows.length; i++) {
    const m = rows[i].match(SECTION_LINE);
    if (m && m[1].includes(PROFILE_LIFE_SECTION)) {
      headerIdx = i;
      break;
    }
  }

  if (headerIdx === -1) {
    // 没有这节 —— 在文末新建。
    const base = existingText.endsWith("\n") ? existingText : `${existingText}\n`;
    const block = `\n## ${PROFILE_LIFE_SECTION}\n${added.join("\n")}\n`;
    return { text: `${base}${block}`, added };
  }

  // 找到该节结束位置（下一个 `## ` 标题，或文件结尾），插到节内最后一个条目后。
  let end = rows.length;
  for (let i = headerIdx + 1; i < rows.length; i++) {
    if (SECTION_LINE.test(rows[i])) {
      end = i;
      break;
    }
  }
  let insertAt = headerIdx + 1;
  for (let i = headerIdx + 1; i < end; i++) {
    if (rows[i].trim()) insertAt = i + 1;
  }
  rows.splice(insertAt, 0, ...added);
  let text = rows.join("\n");
  if (!text.endsWith("\n")) text += "\n";
  return { text, added };
}

/**
 * 新的 review 入队 + 已答复条目出队。出队是直接删行：队列是工作清单，
 * 审计痕迹留在 apply-log.md 里，不在这儿堆历史。
 */
function updateReviewQueue(review, events, today) {
  const resolvedIds = new Set(events.filter((event) => event.kind === "resolved").map((event) => event.id));
  const newReviews = events.filter((event) => event.kind === "review");
  if (!resolvedIds.size && !newReviews.length) return { text: null, added: 0, resolved: 0 };

  const rows = review.rows.filter((row) => !(row.kind === "review" && resolvedIds.has(row.id)));
  for (const event of newReviews) {
    const id = review.allocId();
    event.id = id;
    const proposal = event.text ? ` 建议改成：${event.text}` : "";
    rows.push({
      kind: "review",
      done: false,
      id,
      date: today,
      text: `冲突 ${event.conflictWith.join(", ")} — ${event.rationale}${proposal}`,
    });
  }
  const text = renderReviewQueue(rows, review.highWater);
  return { text, added: newReviews.length, resolved: resolvedIds.size };
}

function appendApplyLog(existingText, events, stamp) {
  if (!events.length) return existingText;
  const header = existingText.trim()
    ? existingText
    : ["# 记忆写入日志", "", "`scripts/memory-apply.mjs` 每次执行的决策流水，供排查“这条事实哪来的”。", "", "---", ""].join("\n");
  const lines = events.map((event) => {
    switch (event.kind) {
      case "add":
        return `- [${stamp}] add${event.pending ? "(pending)" : ""} ${event.id} (${event.source}/${event.confidence}) «${event.text}»`;
      case "update":
        return `- [${stamp}] update${event.pending ? "(pending)" : ""} ${event.supersedes.join(",")}→${event.id} (${event.source}/${event.confidence}) «${event.text}»`;
      case "promote":
        return `- [${stamp}] promote ${event.id} (${event.reason}) «${event.text}»`;
      case "demote":
        return `- [${stamp}] demote ${event.id} (${event.reason}) «${event.text}»`;
      case "expire":
        return `- [${stamp}] expire ${event.id} «${event.text}»`;
      case "merge":
        return `- [${stamp}] merge ${event.id} seen:${event.seen} «${event.text}»`;
      case "add_evidence":
        return `- [${stamp}] add_evidence ${event.id} seen:${event.seen}`;
      case "skip_duplicate":
        return `- [${stamp}] skip_duplicate ${event.id}`;
      case "keep_both":
        return `- [${stamp}] keep_both — ${event.rationale}`;
      case "resolved":
        return `- [${stamp}] resolved ${event.id} — ${event.text}`;
      default:
        return `- [${stamp}] review ${event.id ?? "?"} ${event.conflictWith.join(",")} — ${event.rationale}`;
    }
  });
  const base = header.endsWith("\n") ? header : `${header}\n`;
  const merged = `${base}${lines.join("\n")}\n`;
  const allLines = merged.split("\n");
  const logLines = allLines.filter((line) => line.startsWith("- ["));
  if (logLines.length <= APPLY_LOG_KEEP) return merged;
  const keep = new Set(logLines.slice(-APPLY_LOG_KEEP));
  return allLines.filter((line) => !line.startsWith("- [") || keep.has(line)).join("\n");
}

// --------------------------------------------------------------------- modes

function saveRejected(raw, reason) {
  const dir = path.join(backupDir(), "rejected");
  fs.mkdirSync(dir, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const target = path.join(dir, `${stamp}.json`);
  fs.writeFileSync(target, `${raw}\n`, "utf8");
  fs.appendFileSync(path.join(dir, "reasons.log"), `${stamp}\t${reason}\n`, "utf8");
  return target;
}

function commitWorld(world, runId) {
  const written = [];
  const targets = [
    [FACTS, renderFactFile(world.factRows), ""],
    [PENDING, renderFactFile(world.pendingRows), pendingHeader()],
    [ARCHIVE, renderFactFile(world.archiveRows), archiveHeader()],
  ];
  for (const [name, next, fallback] of targets) {
    if (next === readText(name, fallback)) continue;
    backup(name, runId);
    if (!dryRun) writeTextAtomic(name, next);
    written.push(name);
  }
  return written;
}

function runContext() {
  const world = loadWorld();
  const today = localDate();
  const runId = new Date().toISOString().replace(/[:.]/g, "-");
  // 蒸馏前先做一次候选区维护，模型看到的就是维护后的状态。
  const sweep = sweepPending(world, today);
  // 补齐的 id 立刻落盘，蒸馏器看到的 id 必须和磁盘一致。
  // 无条件调用：commitWorld 内部逐文件比对，没变化就不写。
  const written = commitWorld(world, runId);
  if (sweep.length && !dryRun) writeTextAtomic(APPLY_LOG, appendApplyLog(readText(APPLY_LOG), sweep, localMinute()));

  const describe = (row) => ({
    id: row.marker.id,
    date: row.date,
    section: row.section,
    source: row.marker.source,
    confidence: row.marker.confidence,
    seen: row.marker.seen,
    text: row.text,
  });
  const isLive = (row) => row.kind === "fact" && !row.marker.supersededBy;
  const facts = world.factRows.filter(isLive).map(describe);
  const pendingFacts = world.pendingRows.filter(isLive).map(describe);

  const recentEntries = readText("recent-context.md")
    .split(/\r?\n/)
    .filter((line) => line.trim().startsWith("["))
    .slice(-CONTEXT_RECENT_ENTRIES);

  const summaryLines = readText(SUMMARY)
    .split(/\r?\n/)
    .filter((line) => /^\[?\d{4}-\d{2}-\d{2}/.test(line.trim().replace(/^-\s*/, "")))
    .slice(-CONTEXT_SUMMARY_LINES);

  // 已经在队列里等用户答复的冲突要告诉蒸馏器，否则它每晚重复入队同一件事。
  const openReviews = loadReviewQueue()
    .rows.filter((row) => row.kind === "review" && !row.done)
    .map((row) => ({ id: row.id, date: row.date, text: row.text }));

  return {
    ok: true,
    mode: "context",
    today,
    sections: sectionNames(world.factRows),
    facts,
    pending_facts: pendingFacts,
    open_reviews: openReviews,
    swept: sweep.length,
    recent_context: recentEntries,
    recent_summary: summaryLines,
    backfilled_ids: world.filled,
    written,
  };
}

function runBackfill() {
  const world = loadWorld();
  const runId = new Date().toISOString().replace(/[:.]/g, "-");
  const written = world.filled > 0 ? commitWorld(world, runId) : [];
  return { ok: true, mode: "backfill", backfilled_ids: world.filled, written };
}

function runApply(raw) {
  const world = loadWorld();
  const today = localDate();
  const stamp = localMinute();
  const runId = new Date().toISOString().replace(/[:.]/g, "-");

  const review = loadReviewQueue();
  let batch;
  let validated;
  try {
    batch = parseBatch(raw);
    validated = validateBatch(batch, world, review);
  } catch (err) {
    if (!(err instanceof Rejected)) throw err;
    const saved = dryRun ? "" : saveRejected(raw, err.reason);
    // fail-closed：一条都不写。原始输出存盘，可用 --file 重放。
    return { ok: false, mode: "apply", rejected: err.reason, saved, applied: 0 };
  }

  const events = applyPlan(validated.plan, world, today);
  events.push(...sweepPending(world, today));
  const written = commitWorld(world, runId);

  const summaryResult = appendSummaryLines(readText(SUMMARY), validated.summary);
  if (summaryResult.added.length) {
    backup(SUMMARY, runId);
    if (!dryRun) writeTextAtomic(SUMMARY, summaryResult.text);
    written.push(SUMMARY);
  }

  const lifeResult = appendLifeEvents(readText(PROFILE), validated.lifeEvents ?? []);
  if (lifeResult.added.length) {
    backup(PROFILE, runId);
    if (!dryRun) writeTextAtomic(PROFILE, lifeResult.text);
    written.push(PROFILE);
  }

  const reviewResult = updateReviewQueue(review, events, today);
  if (reviewResult.text !== null) {
    backup(REVIEW, runId);
    if (!dryRun) writeTextAtomic(REVIEW, reviewResult.text);
    written.push(REVIEW);
  }

  if (events.length && !dryRun) {
    writeTextAtomic(APPLY_LOG, appendApplyLog(readText(APPLY_LOG), events, stamp));
    written.push(APPLY_LOG);
  }

  const counts = {};
  for (const event of events) counts[event.kind] = (counts[event.kind] || 0) + 1;

  return {
    ok: true,
    mode: "apply",
    dry_run: dryRun,
    json_repaired: batch.repaired,
    backfilled_ids: world.filled,
    decisions: events.length,
    counts,
    summary_added: summaryResult.added.length,
    life_events_added: lifeResult.added.length,
    review_added: reviewResult.added,
    review_resolved: reviewResult.resolved,
    written,
  };
}

/**
 * 会话启动时读：还欠用户哪些确认。只给近期的，超过 REVIEW_STALE_DAYS
 * 的不再主动问（和 pending-followups 一个口径，不翻旧账）。
 */
function runPendingReviews() {
  const review = loadReviewQueue();
  const today = localDate();
  const open = review.rows.filter((row) => row.kind === "review" && !row.done);
  const fresh = [];
  let stale = 0;
  for (const row of open) {
    const age = daysSince(row.date, today);
    if (age > REVIEW_STALE_DAYS) {
      stale += 1;
      continue;
    }
    fresh.push({ id: row.id, date: row.date, age_days: age, text: row.text });
  }
  return { ok: true, mode: "pending-reviews", today, reviews: fresh, stale_hidden: stale };
}

function readStdin() {
  try {
    return fs.readFileSync(0, "utf8");
  } catch {
    return "";
  }
}

function main() {
  if (!fs.existsSync(memoryRoot)) {
    process.stdout.write(`${JSON.stringify({ ok: false, error: `memory_dir_missing: ${memoryRoot}` })}\n`);
    process.exitCode = 1;
    return;
  }

  let result;
  if (argv.includes("--context")) {
    result = runContext();
  } else if (argv.includes("--backfill")) {
    result = runBackfill();
  } else if (argv.includes("--pending-reviews")) {
    result = runPendingReviews();
  } else {
    const fromFile = flagValue("--file");
    const raw = fromFile ? fs.readFileSync(path.resolve(fromFile), "utf8") : readStdin();
    if (!raw.trim()) {
      result = { ok: false, mode: "apply", rejected: "empty_input", applied: 0 };
    } else {
      result = runApply(raw);
    }
    if (!result.ok) process.exitCode = 2;
  }

  if (!quiet) process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

main();
