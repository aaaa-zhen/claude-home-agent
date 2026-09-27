#!/bin/bash
# Patch the generic Agent OS gateway so [send_file:/path] replies are sent as
# WeChat file/media attachments instead of plain text.
set -euo pipefail

targets=(
  "/Users/zhen/home-agent/weixin-agent/node_modules/weixin-agent-sdk/dist/index.mjs"
  "/opt/homebrew/lib/node_modules/weixin-acp/node_modules/weixin-agent-sdk/dist/index.mjs"
  /Users/zhen/.npm/_npx/*/node_modules/weixin-agent-sdk/dist/index.mjs
)

patched=0
for target in "${targets[@]}"; do
  [ -f "$target" ] || continue

  if grep -q "weixin-agent-os-send-file" "$target"; then
    echo "[patch] agent os send_file already applied: $target"
    continue
  fi

  if ! grep -q "weixin agent os generic gateway patch" "$target"; then
    echo "[patch] agent os gateway not present, skipping send_file: $target"
    continue
  fi

  python3 - "$target" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()
before = '''\tconst replyText = payload?.response?.text;
\tconst contextToken = full.context_token;
\tif (!replyText || !contextToken) return false;
\tawait sendMessageWeixin({
\t\tto: full.from_user_id ?? "",
\t\ttext: markdownToPlainText(replyText),
\t\topts: {
\t\t\tbaseUrl: deps.baseUrl,
\t\t\ttoken: deps.token,
\t\t\tcontextToken
\t\t}
\t});
\tlogger.info(`[weixin-agent-os-gateway] handled requestId=${requestId} task=${payload.task?.task_id ?? ""} durationMs=${Date.now() - requestStartedAt}`);
\treturn true;
}
'''
after = '''\tconst replyText = payload?.response?.text;
\tconst contextToken = full.context_token;
\tif (!replyText || !contextToken) return false;
\tconst sendFileMatch = String(replyText).match(/\\[send_file:([^\\]]+)\\]/);
\tif (sendFileMatch) {
\t\tlet sendFilePath = sendFileMatch[1].trim();
\t\tconst caption = String(replyText).replace(/\\s*\\[send_file:[^\\]]+\\]\\s*/g, "").trim();
\t\tif (!path.isAbsolute(sendFilePath)) sendFilePath = path.resolve(root, sendFilePath);
\t\tif (!fs.existsSync(sendFilePath)) {
\t\t\tawait sendMessageWeixin({
\t\t\t\tto: full.from_user_id ?? "",
\t\t\t\ttext: `文件没找到，没法发送：${path.basename(sendFilePath)}`,
\t\t\t\topts: {
\t\t\t\t\tbaseUrl: deps.baseUrl,
\t\t\t\t\ttoken: deps.token,
\t\t\t\t\tcontextToken
\t\t\t\t}
\t\t\t});
\t\t\tlogger.warn(`[weixin-agent-os-send-file] missing requestId=${requestId} path=${sendFilePath}`);
\t\t\treturn true;
\t\t}
\t\tawait sendWeixinMediaFile({
\t\t\tfilePath: sendFilePath,
\t\t\tto: full.from_user_id ?? "",
\t\t\ttext: caption ? markdownToPlainText(caption) : "",
\t\t\topts: {
\t\t\t\tbaseUrl: deps.baseUrl,
\t\t\t\ttoken: deps.token,
\t\t\t\tcontextToken
\t\t\t},
\t\t\tcdnBaseUrl: deps.cdnBaseUrl
\t\t});
\t\tlogger.info(`[weixin-agent-os-send-file] handled requestId=${requestId} task=${payload.task?.task_id ?? ""} path=${sendFilePath} durationMs=${Date.now() - requestStartedAt}`);
\t\treturn true;
\t}
\tawait sendMessageWeixin({
\t\tto: full.from_user_id ?? "",
\t\ttext: markdownToPlainText(replyText),
\t\topts: {
\t\t\tbaseUrl: deps.baseUrl,
\t\t\ttoken: deps.token,
\t\t\tcontextToken
\t\t}
\t});
\tlogger.info(`[weixin-agent-os-gateway] handled requestId=${requestId} task=${payload.task?.task_id ?? ""} durationMs=${Date.now() - requestStartedAt}`);
\treturn true;
}
'''
if before not in text:
    raise SystemExit(f"agent os gateway reply block not found in {path}")
path.write_text(text.replace(before, after, 1))
PY
  echo "[patch] agent os send_file applied: $target"
  patched=1
done

if [ "$patched" = 0 ]; then
  echo "[patch] no new agent os send_file bundles patched"
fi
