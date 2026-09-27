import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const script = path.join(projectRoot, "scripts", "memory-apply.mjs");

const BASE_FACTS = `# 学到的知识

本文件只放会影响日常判断的长期规则。

---

## 行为规则

- [2026-04-02] 用户用英文写消息时用英文回复。\`#行为\`
- [2026-07-17] {f0900 user high} **打车默认选最便宜的车型**。\`#行为\` \`#打车\`

## 设备操作

- [2026-06-25] HA 本机局域网 API 优先，不要走外网域名。\`#部署\` \`#HA\`

## [2026-07-13] agent-browser CDP 被代理拦截 #浏览器
- 现象：连 127.0.0.1:9333 报 502。
- 修复：先 unset ALL_PROXY。
`;

function makeRoot(facts = BASE_FACTS, extra = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "mem-apply-"));
  const memory = path.join(root, "memory");
  fs.mkdirSync(memory, { recursive: true });
  fs.writeFileSync(path.join(memory, "learned-facts.md"), facts, "utf8");
  fs.writeFileSync(path.join(memory, "conversation-summary.md"), "# 对话历史摘要\n\n---\n\n[2026-07-25] 旧话题 — 已存在\n", "utf8");
  fs.writeFileSync(path.join(memory, "recent-context.md"), "# 短期\n[2026-07-26 10:00] 用户：测试\n", "utf8");
  for (const [name, content] of Object.entries(extra)) {
    fs.writeFileSync(path.join(memory, name), content, "utf8");
  }
  return root;
}

function run(root, args, input) {
  const result = { status: 0, stdout: "" };
  try {
    result.stdout = execFileSync(process.execPath, [script, "--root", root, ...args], {
      input: input ?? "",
      encoding: "utf8",
    });
  } catch (err) {
    result.status = err.status ?? 1;
    result.stdout = err.stdout ?? "";
  }
  return { ...result, json: JSON.parse(result.stdout) };
}

function facts(root) {
  return fs.readFileSync(path.join(root, "memory", "learned-facts.md"), "utf8");
}

function archive(root) {
  const target = path.join(root, "memory", "learned-facts-archive.md");
  return fs.existsSync(target) ? fs.readFileSync(target, "utf8") : "";
}

test("--context 给没有标记的旧事实补 id 并落盘", () => {
  const root = makeRoot();
  const { json } = run(root, ["--context"]);

  assert.equal(json.ok, true);
  assert.equal(json.backfilled_ids, 2); // 两条无标记事实
  assert.deepEqual(json.sections, ["行为规则", "设备操作"]); // 案例小节不算分类
  assert.equal(json.facts.length, 3);

  const text = facts(root);
  assert.match(text, /- \[2026-04-02\] \{f0901 unknown med\} 用户用英文/);
  assert.match(text, /- \[2026-07-17\] \{f0900 user high\} \*\*打车默认/); // 已有标记不变
  // 案例小节的非事实行原样保留
  assert.match(text, /^- 现象：连 127\.0\.0\.1:9333 报 502。$/m);
});

test("--context 是幂等的，二次运行不再分配 id", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  const first = facts(root);
  const { json } = run(root, ["--context"]);
  assert.equal(json.backfilled_ids, 0);
  assert.equal(facts(root), first);
});

test("update 把旧事实搬进 archive 并打 superseded 标记", () => {
  const root = makeRoot();
  const ctx = run(root, ["--context"]).json;
  const target = ctx.facts.find((f) => f.text.includes("局域网 API 优先"));

  const { json, status } = run(root, ["--stdin"], JSON.stringify({
    summary: ["[2026-07-26] HA 网络 — 离家时本地控制失效"],
    decisions: [{
      decision: "update",
      text: "**控制家居前先判断 Mac 是否在 192.168.1.x 网段**，离家时本地 HA 直连失败。`#部署` `#HA`",
      supersedes: [target.id],
      source: "user",
      confidence: "high",
      rationale: "用户 7-20 明确说离家失效",
    }],
  }));

  assert.equal(status, 0);
  assert.equal(json.ok, true);
  assert.equal(json.counts.update, 1);
  assert.equal(json.summary_added, 1);

  const text = facts(root);
  assert.doesNotMatch(text, /局域网 API 优先/); // 旧事实已移出活跃文件
  assert.match(text, /\{f\d{4} user high\} \*\*控制家居前先判断/);

  const archived = archive(root);
  assert.match(archived, new RegExp(`\\{${target.id} \\w+ \\w+ superseded→f\\d{4}\\}`));
  assert.match(archived, /局域网 API 优先/);
});

test("新事实插入到指定分类的末尾", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  run(root, ["--stdin"], JSON.stringify({
    decisions: [{
      decision: "add",
      section: "行为规则",
      text: "**新规则**。`#行为`",
      source: "correct",
      confidence: "high",
    }],
  }));

  const lines = facts(root).split("\n");
  const added = lines.findIndex((line) => line.includes("**新规则**"));
  const deviceHeader = lines.findIndex((line) => line === "## 设备操作");
  assert.ok(added > 0 && added < deviceHeader, "新事实应落在行为规则小节内");
});

test("target_id 不存在时整批拒绝，一个字都不写", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  const before = facts(root);
  const summaryBefore = fs.readFileSync(path.join(root, "memory", "conversation-summary.md"), "utf8");

  const { json, status } = run(root, ["--stdin"], JSON.stringify({
    summary: ["[2026-07-26] 这条摘要不应该被写入"],
    decisions: [
      { decision: "add", section: "行为规则", text: "合法的一条", source: "user", confidence: "high" },
      { decision: "merge", target_id: "f9999", text: "指向不存在的 id" },
    ],
  }));

  assert.equal(status, 2);
  assert.equal(json.ok, false);
  assert.match(json.rejected, /target_id_unknown: f9999/);
  assert.equal(facts(root), before, "learned-facts 不能被部分写入");
  assert.equal(fs.readFileSync(path.join(root, "memory", "conversation-summary.md"), "utf8"), summaryBefore);
  assert.ok(fs.existsSync(json.saved), "被拒绝的原始输出应存盘可重放");
});

test("JSON 解析失败时拒绝并存盘", () => {
  const root = makeRoot();
  const before = facts(root);
  const { json, status } = run(root, ["--stdin"], "抱歉，我没有找到需要蒸馏的内容。");
  assert.equal(status, 2);
  assert.match(json.rejected, /json_parse_failed|batch_not_object/);
  assert.equal(facts(root), before);
});

test("模型带 markdown 围栏时仍能解析", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  const { json, status } = run(root, ["--stdin"], "```json\n{\"summary\":[],\"decisions\":[]}\n```");
  assert.equal(status, 0);
  assert.equal(json.ok, true);
  assert.equal(json.decisions, 0);
});

test("非法枚举值被拒绝", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  const { json, status } = run(root, ["--stdin"], JSON.stringify({
    decisions: [{ decision: "add", section: "行为规则", text: "x", source: "guessing", confidence: "high" }],
  }));
  assert.equal(status, 2);
  assert.match(json.rejected, /source_invalid: guessing/);
});

test("超出批次上限被拒绝", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  const { json, status } = run(root, ["--stdin"], JSON.stringify({
    summary: Array.from({ length: 6 }, (_, i) => `[2026-07-26] 第 ${i} 条`),
    decisions: [],
  }));
  assert.equal(status, 2);
  assert.match(json.rejected, /summary_over_cap/);
});

test("同一条事实在一批里被两次操作时拒绝", () => {
  const root = makeRoot();
  const ctx = run(root, ["--context"]).json;
  const id = ctx.facts[0].id;
  const { json, status } = run(root, ["--stdin"], JSON.stringify({
    decisions: [
      { decision: "add_evidence", target_id: id },
      { decision: "merge", target_id: id, text: "又改一遍" },
    ],
  }));
  assert.equal(status, 2);
  assert.match(json.rejected, /target_reused/);
});

test("add_evidence 累加 seen 并刷新日期", () => {
  const root = makeRoot();
  const ctx = run(root, ["--context"]).json;
  const target = ctx.facts.find((f) => f.text.includes("英文回复"));

  run(root, ["--stdin"], JSON.stringify({ decisions: [{ decision: "add_evidence", target_id: target.id, source: "user" }] }));
  run(root, ["--stdin"], JSON.stringify({ decisions: [{ decision: "add_evidence", target_id: target.id, source: "user" }] }));

  assert.match(facts(root), new RegExp(`\\{${target.id} user med seen:3\\}`));
});

test("review 决策写入待裁决队列而不是改事实", () => {
  const root = makeRoot();
  const ctx = run(root, ["--context"]).json;
  const before = facts(root);
  const { json } = run(root, ["--stdin"], JSON.stringify({
    decisions: [{
      decision: "review",
      conflict_with: [ctx.facts[0].id, ctx.facts[1].id],
      rationale: "两条规则都像是当前有效的，不确定哪条该退休",
    }],
  }));

  assert.equal(json.review_added, 1);
  assert.equal(facts(root), before);
  const queue = fs.readFileSync(path.join(root, "memory", "review-queue.md"), "utf8");
  assert.match(queue, /- \[ \] r0001 \[\d{4}-\d{2}-\d{2}\] 冲突 f\d{4}, f\d{4} — 两条规则都像是当前有效的/);
});

test("摘要重复行不会被再次追加", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  const { json } = run(root, ["--stdin"], JSON.stringify({
    summary: ["[2026-07-25] 旧话题 — 已存在"],
    decisions: [],
  }));
  assert.equal(json.summary_added, 0);
});

test("已被取代的事实不能再作为 target", () => {
  const root = makeRoot();
  const ctx = run(root, ["--context"]).json;
  const target = ctx.facts.find((f) => f.text.includes("局域网 API 优先"));
  run(root, ["--stdin"], JSON.stringify({
    decisions: [{ decision: "update", text: "新规则替代它", supersedes: [target.id], source: "user", confidence: "high" }],
  }));

  const { json, status } = run(root, ["--stdin"], JSON.stringify({
    decisions: [{ decision: "merge", target_id: target.id, text: "再改一次" }],
  }));
  assert.equal(status, 2);
  assert.match(json.rejected, /target_id_unknown|already_superseded/);
});

test("--dry-run 不落盘", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  const before = facts(root);
  const { json } = run(root, ["--stdin", "--dry-run"], JSON.stringify({
    decisions: [{ decision: "add", section: "行为规则", text: "干跑不应写入", source: "user", confidence: "high" }],
  }));
  assert.equal(json.ok, true);
  assert.equal(json.decisions, 1);
  assert.equal(facts(root), before);
});

test("写入日志记录每条决策", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  run(root, ["--stdin"], JSON.stringify({
    decisions: [{ decision: "add", section: "设备操作", text: "**新设备事实**", source: "tool", confidence: "high" }],
  }));
  const log = fs.readFileSync(path.join(root, "memory", "apply-log.md"), "utf8");
  assert.match(log, /add f\d{4} \(tool\/high\) «\*\*新设备事实\*\*»/);
});

test("动作键写成 action 时也能识别", () => {
  const root = makeRoot();
  const ctx = run(root, ["--context"]).json;
  const { json, status } = run(root, ["--stdin"], JSON.stringify({
    decisions: [{ action: "add_evidence", target_id: ctx.facts[0].id }],
  }));
  assert.equal(status, 0);
  assert.equal(json.counts.add_evidence, 1);
});

test("中文里的裸 ASCII 引号被修复而不是整批拒绝", () => {
  const root = makeRoot();
  const ctx = run(root, ["--context"]).json;
  const bad = `{"summary":[],"decisions":[{"decision":"add","section":"设备操作","text":"**家里没有厨房空调**：用户会说"打开厨房空调"，实际只有三台。\`#设备\`","source":"user","confidence":"high","rationale":"用户两次说"打开厨房空调""}]}`;
  assert.throws(() => JSON.parse(bad), "前提：这串本身不是合法 JSON");

  const { json, status } = run(root, ["--stdin"], bad);
  assert.equal(status, 0);
  assert.equal(json.json_repaired, true);
  assert.equal(json.counts.add, 1);
  assert.match(facts(root), /用户会说"打开厨房空调"，实际只有三台/);
  assert.ok(ctx.facts.length > 0);
});

test("修复救不回来的仍然整批拒绝", () => {
  const root = makeRoot();
  const before = facts(root);
  const { json, status } = run(root, ["--stdin"], '{"summary": [ , ], "decisions"');
  assert.equal(status, 2);
  assert.match(json.rejected, /json_parse_failed/);
  assert.equal(facts(root), before);
});

test("合法 JSON 不会被误改", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  const { json } = run(root, ["--stdin"], JSON.stringify({
    decisions: [{ decision: "add", section: "行为规则", text: 'A "quoted" B', source: "user", confidence: "med" }],
  }));
  assert.equal(json.json_repaired, false);
  assert.match(facts(root), /A "quoted" B/);
});

// ---------------------------------------------------------------- P2 待裁决队列

function fileReview(root) {
  const ctx = run(root, ["--context"]).json;
  run(root, ["--stdin"], JSON.stringify({
    decisions: [{
      decision: "review",
      conflict_with: [ctx.facts[0].id, ctx.facts[1].id],
      rationale: "两条都像还生效，判不准该退休哪条",
    }],
  }));
  return ctx;
}

test("--pending-reviews 列出还欠用户的确认", () => {
  const root = makeRoot();
  fileReview(root);
  const { json } = run(root, ["--pending-reviews"]);
  assert.equal(json.reviews.length, 1);
  assert.equal(json.reviews[0].id, "r0001");
  assert.equal(json.reviews[0].age_days, 0);
  assert.equal(json.stale_hidden, 0);
});

test("超过 14 天的待裁决不再主动问", () => {
  const root = makeRoot();
  const queue = "# 记忆待裁决队列\n\n---\n\n- [ ] r0007 [2020-01-01] 冲突 f0900 — 陈年旧账\n";
  fs.writeFileSync(path.join(root, "memory", "review-queue.md"), queue, "utf8");
  const { json } = run(root, ["--pending-reviews"]);
  assert.equal(json.reviews.length, 0);
  assert.equal(json.stale_hidden, 1);
});

test("已答复的条目随决策出队", () => {
  const root = makeRoot();
  const ctx = fileReview(root);
  const target = ctx.facts[1];

  const { json, status } = run(root, ["--stdin"], JSON.stringify({
    decisions: [{
      decision: "update",
      text: "**用户裁决后的新规则**。`#行为`",
      supersedes: [target.id],
      source: "correct",
      confidence: "high",
      resolves: ["r0001"],
    }],
  }));

  assert.equal(status, 0);
  assert.equal(json.review_resolved, 1);
  assert.equal(json.counts.update, 1);
  assert.equal(run(root, ["--pending-reviews"]).json.reviews.length, 0);

  const queue = fs.readFileSync(path.join(root, "memory", "review-queue.md"), "utf8");
  assert.doesNotMatch(queue, /r0001/, "出队即删行，历史留在 apply-log");
  assert.match(fs.readFileSync(path.join(root, "memory", "apply-log.md"), "utf8"), /resolved r0001 —/);
});

test("keep_both 也能用来回答“两条都对”", () => {
  const root = makeRoot();
  fileReview(root);
  const { json } = run(root, ["--stdin"], JSON.stringify({
    decisions: [{ decision: "keep_both", rationale: "用户说两条都还生效", resolves: ["r0001"] }],
  }));
  assert.equal(json.review_resolved, 1);
  assert.equal(run(root, ["--pending-reviews"]).json.reviews.length, 0);
});

test("resolves 指向不存在的条目时整批拒绝", () => {
  const root = makeRoot();
  const ctx = fileReview(root);
  const before = facts(root);
  const { json, status } = run(root, ["--stdin"], JSON.stringify({
    decisions: [{ decision: "add_evidence", target_id: ctx.facts[0].id, resolves: ["r0099"] }],
  }));
  assert.equal(status, 2);
  assert.match(json.rejected, /resolves_unknown_or_done: r0099/);
  assert.equal(facts(root), before);
  assert.equal(run(root, ["--pending-reviews"]).json.reviews.length, 1, "队列不能被半途改掉");
});

test("同一条待裁决不能在一批里被解决两次", () => {
  const root = makeRoot();
  const ctx = fileReview(root);
  const { json, status } = run(root, ["--stdin"], JSON.stringify({
    decisions: [
      { decision: "add_evidence", target_id: ctx.facts[0].id, resolves: ["r0001"] },
      { decision: "keep_both", rationale: "重复解决", resolves: ["r0001"] },
    ],
  }));
  assert.equal(status, 2);
  assert.match(json.rejected, /resolves_reused/);
});

test("review 决策自己不能带 resolves", () => {
  const root = makeRoot();
  fileReview(root);
  const ctx = run(root, ["--context"]).json;
  const { json, status } = run(root, ["--stdin"], JSON.stringify({
    decisions: [{ decision: "review", conflict_with: [ctx.facts[0].id], rationale: "x", resolves: ["r0001"] }],
  }));
  assert.equal(status, 2);
  assert.match(json.rejected, /review_cannot_resolve/);
});

test("--context 把未答复的冲突带给蒸馏器，避免重复入队", () => {
  const root = makeRoot();
  fileReview(root);
  const { json } = run(root, ["--context"]);
  assert.equal(json.open_reviews.length, 1);
  assert.equal(json.open_reviews[0].id, "r0001");
});

test("review id 连续分配，不复用已出队的号", () => {
  const root = makeRoot();
  const ctx = fileReview(root);
  run(root, ["--stdin"], JSON.stringify({
    decisions: [{ decision: "keep_both", rationale: "答复了", resolves: ["r0001"] }],
  }));
  run(root, ["--stdin"], JSON.stringify({
    decisions: [{ decision: "review", conflict_with: [ctx.facts[0].id], rationale: "新的冲突" }],
  }));
  const { json } = run(root, ["--pending-reviews"]);
  assert.equal(json.reviews.length, 1);
  assert.equal(json.reviews[0].id, "r0002", "出队后号不能被复用");
});

// ------------------------------------------------------------- P3 候选事实闸门

function pending(root) {
  const target = path.join(root, "memory", "learned-facts-pending.md");
  return fs.existsSync(target) ? fs.readFileSync(target, "utf8") : "";
}

const WEAK = { decision: "add", section: "设备操作", text: "**推测的接入流程**：先开 RTSP 再加 ONVIF。`#设备`", source: "infer", confidence: "med" };

test("infer 来源的新事实进候选区，不进生效规则", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  const { json } = run(root, ["--stdin"], JSON.stringify({ decisions: [WEAK] }));

  assert.equal(json.counts.add, 1);
  assert.doesNotMatch(facts(root), /推测的接入流程/, "弱证据不能进 learned-facts.md");
  assert.match(pending(root), /\{f\d{4} infer med\} \*\*推测的接入流程\*\*/);
  assert.match(pending(root), /^## 设备操作$/m, "候选文件按需长出对应分类");
  assert.match(fs.readFileSync(path.join(root, "memory", "apply-log.md"), "utf8"), /add\(pending\) f\d{4}/);
});

test("cron 来源和 low 置信度同样只能进候选区", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  run(root, ["--stdin"], JSON.stringify({
    decisions: [
      { decision: "add", section: "行为规则", text: "推送里读到的", source: "cron", confidence: "high" },
      { decision: "add", section: "行为规则", text: "自己都没把握的", source: "user", confidence: "low" },
    ],
  }));
  assert.doesNotMatch(facts(root), /推送里读到的|自己都没把握的/);
  assert.match(pending(root), /推送里读到的/);
  assert.match(pending(root), /自己都没把握的/);
});

test("user/tool/correct 来源照旧直接生效", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  run(root, ["--stdin"], JSON.stringify({
    decisions: [{ decision: "add", section: "设备操作", text: "**工具验证过的事实**", source: "tool", confidence: "high" }],
  }));
  assert.match(facts(root), /\*\*工具验证过的事实\*\*/);
  assert.equal(pending(root).includes("工具验证过的事实"), false);
});

test("候选被再次印证后自动晋升(seen≥2)", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  run(root, ["--stdin"], JSON.stringify({ decisions: [WEAK] }));
  const candidate = run(root, ["--context"]).json.pending_facts[0];
  assert.equal(candidate.seen, 1);

  const { json } = run(root, ["--stdin"], JSON.stringify({
    decisions: [{ decision: "add_evidence", target_id: candidate.id }],
  }));

  assert.equal(json.counts.promote, 1);
  assert.match(facts(root), /推测的接入流程/, "晋升后进入生效规则");
  assert.equal(pending(root).includes("推测的接入流程"), false);
  assert.match(fs.readFileSync(path.join(root, "memory", "apply-log.md"), "utf8"), /promote f\d{4} \(seen:2\)/);
});

test("候选被工具验证后晋升，理由记来源", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  run(root, ["--stdin"], JSON.stringify({ decisions: [WEAK] }));
  const candidate = run(root, ["--context"]).json.pending_facts[0];

  const { json } = run(root, ["--stdin"], JSON.stringify({
    decisions: [{ decision: "merge", target_id: candidate.id, text: "**实测过的接入流程**：端口 2020。`#设备`", source: "tool", confidence: "high" }],
  }));

  assert.equal(json.counts.promote, 1);
  assert.match(facts(root), /\*\*实测过的接入流程\*\*/);
  assert.match(fs.readFileSync(path.join(root, "memory", "apply-log.md"), "utf8"), /promote f\d{4} \(source:tool\)/);
});

test("晋升落回它原来的分类", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  run(root, ["--stdin"], JSON.stringify({ decisions: [WEAK] }));
  const candidate = run(root, ["--context"]).json.pending_facts[0];
  run(root, ["--stdin"], JSON.stringify({ decisions: [{ decision: "add_evidence", target_id: candidate.id }] }));

  const lines = facts(root).split("\n");
  const idx = lines.findIndex((line) => line.includes("推测的接入流程"));
  const deviceHeader = lines.findIndex((line) => line === "## 设备操作");
  assert.ok(deviceHeader >= 0 && idx > deviceHeader, "应落在设备操作小节内");
});

test("超过 30 天还是孤证的候选过期进 archive", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  run(root, ["--stdin"], JSON.stringify({ decisions: [WEAK] }));

  // 把候选的日期改老，模拟等了很久没人佐证
  const aged = pending(root).replace(/^- \[\d{4}-\d{2}-\d{2}\]/m, "- [2020-01-01]");
  fs.writeFileSync(path.join(root, "memory", "learned-facts-pending.md"), aged, "utf8");

  const { json } = run(root, ["--stdin"], JSON.stringify({ decisions: [] }));
  assert.equal(json.counts.expire, 1);
  assert.equal(pending(root).includes("推测的接入流程"), false);
  assert.match(archive(root), /superseded→expired\} \*\*推测的接入流程\*\*/);
  assert.doesNotMatch(facts(root), /推测的接入流程/, "过期的绝不能溜进生效规则");
});

test("推测不能推翻已生效的规则，整批拒绝", () => {
  const root = makeRoot();
  const ctx = run(root, ["--context"]).json;
  const active = ctx.facts.find((f) => f.text.includes("局域网 API 优先"));
  const before = facts(root);

  const { json, status } = run(root, ["--stdin"], JSON.stringify({
    decisions: [{
      decision: "update",
      text: "我猜现在应该走外网了",
      supersedes: [active.id],
      source: "infer",
      confidence: "med",
    }],
  }));

  assert.equal(status, 2);
  assert.match(json.rejected, /weak_cannot_supersede_active/);
  assert.equal(facts(root), before);
});

test("推测可以推翻另一条候选", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  run(root, ["--stdin"], JSON.stringify({ decisions: [WEAK] }));
  const candidate = run(root, ["--context"]).json.pending_facts[0];

  const { json, status } = run(root, ["--stdin"], JSON.stringify({
    decisions: [{
      decision: "update",
      text: "**改过的推测流程**：端口可能是 554。`#设备`",
      supersedes: [candidate.id],
      source: "infer",
      confidence: "med",
    }],
  }));

  assert.equal(status, 0);
  assert.equal(json.counts.update, 1);
  assert.match(pending(root), /改过的推测流程/);
  assert.match(archive(root), new RegExp(`\\{${candidate.id} infer med superseded→f\\d{4}\\}`));
});

test("--context 把候选单独交给蒸馏器，不混进 facts", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  run(root, ["--stdin"], JSON.stringify({ decisions: [WEAK] }));
  const { json } = run(root, ["--context"]);
  assert.equal(json.pending_facts.length, 1);
  assert.equal(json.pending_facts[0].source, "infer");
  assert.equal(json.facts.some((f) => f.text.includes("推测的接入流程")), false);
});

test("--context 也会跑晋升扫描并落盘", () => {
  const root = makeRoot();
  run(root, ["--context"]);
  run(root, ["--stdin"], JSON.stringify({ decisions: [WEAK] }));
  // 手工把 seen 改成 2，模拟别处印证过
  const bumped = pending(root).replace(/\{(f\d{4}) infer med\}/, "{$1 infer med seen:2}");
  fs.writeFileSync(path.join(root, "memory", "learned-facts-pending.md"), bumped, "utf8");

  const { json } = run(root, ["--context"]);
  assert.equal(json.swept, 1);
  assert.equal(json.pending_facts.length, 0);
  assert.match(facts(root), /推测的接入流程/);
});

test("agent 手写进生效区的弱证据会被降级到候选", () => {
  const root = makeRoot(`# 学到的知识

---

## 设备操作

- [2026-07-28] {f0800 infer med} **agent 自己推断后直接写进来的流程**。\`#设备\`
- [2026-07-28] {f0801 tool high} **工具验证过的**。\`#设备\`
`);

  const { json } = run(root, ["--context"]);
  assert.equal(json.swept, 1);
  assert.doesNotMatch(facts(root), /自己推断后直接写进来/, "弱证据要被移出生效区");
  assert.match(facts(root), /工具验证过的/, "强证据不动");
  assert.match(pending(root), /\{f0800 infer med\}/);
  assert.match(fs.readFileSync(path.join(root, "memory", "apply-log.md"), "utf8"), /demote f0800 \(infer\/med\)/);
});

test("legacy 的 unknown 事实不会被降级", () => {
  const root = makeRoot();
  const { json } = run(root, ["--context"]);
  assert.equal(json.swept, 0);
  assert.match(facts(root), /用户用英文/);
  assert.equal(pending(root), "");
});

test("降级和晋升不会来回抖动", () => {
  const root = makeRoot(`# 学到的知识

---

## 设备操作

- [2026-07-28] {f0800 infer med seen:2} **推断的，但已经被印证过两次**。\`#设备\`
`);
  const first = run(root, ["--context"]).json;
  assert.equal(first.swept, 0, "seen>=2 的不该被降级");
  assert.match(facts(root), /已经被印证过两次/);

  const second = run(root, ["--context"]).json;
  assert.equal(second.swept, 0);
  assert.match(facts(root), /已经被印证过两次/);
});
