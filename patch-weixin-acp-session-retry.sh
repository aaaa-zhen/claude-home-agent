#!/bin/bash
# Make weixin-acp recover automatically when the ACP agent loses a session.
set -euo pipefail

targets=(
  "/home/ubuntu/weixin-agent/node_modules/weixin-acp/dist/acp-agent-BUZjysVy.mjs"
  "/home/ubuntu/.npm-global/lib/node_modules/weixin-acp/dist/acp-agent-BUZjysVy.mjs"
  /home/ubuntu/.npm/_npx/*/node_modules/weixin-acp/dist/acp-agent-BUZjysVy.mjs
)

patched=0
for target in "${targets[@]}"; do
  [ -f "$target" ] || continue

  if grep -q "recoverable session error" "$target"; then
    echo "[patch] session retry patch already applied: $target"
    continue
  fi

  python3 - "$target" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()

helper_anchor = """function log(msg) {
\tconsole.log(`[acp] ${msg}`);
}
/**"""
helper_replacement = """function log(msg) {
\tconsole.log(`[acp] ${msg}`);
}
function isRecoverableSessionError(err) {
\tconst message = err?.message ?? "";
\tconst details = err?.data?.details ?? "";
\treturn details === "Session not found" || message.includes("Session not found") || message.includes("Claude Agent process exited unexpectedly");
}
/**"""

chat_old = """\tasync chat(request) {
\t\tconst conn = await this.connection.ensureReady();
\t\tconst sessionId = await this.getOrCreateSession(request.conversationId, conn);
\t\tconst blocks = await convertRequestToContentBlocks(request);
\t\tif (blocks.length === 0) return { text: "" };
\t\tlog(`prompt: "${request.text?.slice(0, 50) || (request.media ? `[${request.media.type}]` : "")}" (session=${sessionId})`);
\t\tconst collector = new ResponseCollector();
\t\tthis.connection.registerCollector(sessionId, collector);
\t\ttry {
\t\t\tawait conn.prompt({
\t\t\t\tsessionId,
\t\t\t\tprompt: blocks
\t\t\t});
\t\t} finally {
\t\t\tthis.connection.unregisterCollector(sessionId);
\t\t}
\t\tconst response = await collector.toResponse();
\t\tlog(`response: ${response.text?.slice(0, 80) ?? "[no text]"}${response.media ? " +media" : ""}`);
\t\treturn response;
\t}"""
chat_new = """\tasync chat(request) {
\t\tconst blocks = await convertRequestToContentBlocks(request);
\t\tif (blocks.length === 0) return { text: "" };
\t\tfor (let attempt = 0; attempt < 2; attempt++) {
\t\t\tconst conn = await this.connection.ensureReady();
\t\t\tconst sessionId = await this.getOrCreateSession(request.conversationId, conn);
\t\t\tlog(`prompt: "${request.text?.slice(0, 50) || (request.media ? `[${request.media.type}]` : "")}" (session=${sessionId})`);
\t\t\tconst collector = new ResponseCollector();
\t\t\tthis.connection.registerCollector(sessionId, collector);
\t\t\ttry {
\t\t\t\tawait conn.prompt({
\t\t\t\t\tsessionId,
\t\t\t\t\tprompt: blocks
\t\t\t\t});
\t\t\t} catch (err) {
\t\t\t\tif (!isRecoverableSessionError(err) || attempt === 1) throw err;
\t\t\t\tlog(`recoverable session error: ${err?.data?.details ?? err?.message ?? err}; recreating ACP session`);
\t\t\t\tthis.clearSession(request.conversationId);
\t\t\t\tthis.connection.dispose();
\t\t\t\tcontinue;
\t\t\t} finally {
\t\t\t\tthis.connection.unregisterCollector(sessionId);
\t\t\t}
\t\t\tconst response = await collector.toResponse();
\t\t\tlog(`response: ${response.text?.slice(0, 80) ?? "[no text]"}${response.media ? " +media" : ""}`);
\t\t\treturn response;
\t\t}
\t\tthrow new Error("ACP session recovery failed");
\t}"""

if helper_anchor not in text:
    raise SystemExit(f"helper anchor not found in {path}")
if chat_old not in text:
    raise SystemExit(f"chat block anchor not found in {path}")

text = text.replace(helper_anchor, helper_replacement, 1)
text = text.replace(chat_old, chat_new, 1)
path.write_text(text)
PY

  echo "[patch] session retry patch applied: $target"
  patched=1
done

if [ "$patched" = 0 ]; then
  echo "[patch] no new weixin-acp session retry bundles patched"
fi
