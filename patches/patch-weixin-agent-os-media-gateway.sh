#!/bin/bash
# Route media (image/video/file/voice) messages through the Agent OS v2 gateway too,
# instead of the bypass that sent them to the standalone ACP backend.
#
# Before: processOneMessage skipped maybeHandleAgentOSGateway whenever the message had
# media (findMediaItem), so images went to deps.agent.chat() — a SECOND brain that never
# wrote to the v2 store. Multi-turn about an image broke (the v2 brain never saw it).
#
# After: media is downloaded first, then handed to the gateway via --media/--media-type.
# The v2 Claude brain reads the image, replies, and records the turn (path folded in), so
# follow-up text stays grounded. The ACP backend stays only as a failsafe.
#
# Depends on patch-weixin-agent-os-gateway.sh having injected maybeHandleAgentOSGateway.
set -euo pipefail

targets=(
  "/Users/zhen/home-agent/weixin-agent/node_modules/weixin-agent-sdk/dist/index.mjs"
  "/opt/homebrew/lib/node_modules/weixin-acp/node_modules/weixin-agent-sdk/dist/index.mjs"
  /Users/zhen/.npm/_npx/*/node_modules/weixin-agent-sdk/dist/index.mjs
)

patched=0
for target in "${targets[@]}"; do
  [ -f "$target" ] || continue
  grep -q "weixin agent os generic gateway patch" "$target" || continue   # gateway patch must be present first

  if grep -q "weixin agent os media gateway patch" "$target"; then
    echo "[patch] agent os media gateway already applied: $target"
    continue
  fi

  python3 - "$target" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()

# R1: signature gains mediaPath/mediaType; guard no longer bypasses on media.
old1 = (
    'async function maybeHandleAgentOSGateway(textBody, full, deps, requestId, requestStartedAt) {\n'
    '\tif (process.env.WEIXIN_AGENT_OS_GATEWAY !== "1") return false;\n'
    '\tconst text = String(textBody ?? "").trim();\n'
    '\tif (!text || text.startsWith("/") || findMediaItem(full.item_list)) return false;\n'
)
new1 = (
    '// weixin agent os media gateway patch: media routes through the gateway too.\n'
    'async function maybeHandleAgentOSGateway(textBody, full, deps, requestId, requestStartedAt, mediaPath = "", mediaType = "") {\n'
    '\tif (process.env.WEIXIN_AGENT_OS_GATEWAY !== "1") return false;\n'
    '\tconst text = String(textBody ?? "").trim();\n'
    '\tif (text.startsWith("/")) return false;\n'
    '\tif (!text && !mediaPath) return false;\n'
)

# R2: pass the downloaded media path to the python gateway.
old2 = (
    '\tif (full.context_token) args.push("--context-token", full.context_token);\n'
    '\tif (process.env.WEIXIN_AGENT_OS_GATEWAY_INLINE === "1") args.push("--inline");\n'
)
new2 = (
    '\tif (mediaPath) args.push("--media", mediaPath, "--media-type", mediaType || "image");\n'
    '\tif (full.context_token) args.push("--context-token", full.context_token);\n'
    '\tif (process.env.WEIXIN_AGENT_OS_GATEWAY_INLINE === "1") args.push("--inline");\n'
)

# R3: the early (text) gateway call must not fire for media — media is handled after download.
old3 = (
    '\tif (await maybeHandleAgentOSGateway(textBody, full, deps, requestId, requestStartedAt)) return;\n'
    '\tlet media;\n'
)
new3 = (
    '\tif (!findMediaItem(full.item_list) && await maybeHandleAgentOSGateway(textBody, full, deps, requestId, requestStartedAt)) return;\n'
    '\tlet media;\n'
)

# R4: after media is downloaded+archived, route it through the gateway; ACP stays as failsafe.
old4 = (
    '\tconst request = {\n'
    '\t\tconversationId: full.from_user_id ?? "",\n'
    '\t\ttext: `${bodyFromItemList(full.item_list)}${mediaMemoryNote}`,\n'
    '\t\tmedia\n'
    '\t};\n'
)
new4 = (
    '\tif (media?.filePath && await maybeHandleAgentOSGateway(textBody, full, deps, requestId, requestStartedAt, media.filePath, media.type)) return;\n'
    + old4
)

for label, old, new in (("R1", old1, new1), ("R2", old2, new2), ("R3", old3, new3), ("R4", old4, new4)):
    if old not in text:
        raise SystemExit(f"media gateway patch anchor {label} not found in {path}")
    text = text.replace(old, new, 1)

path.write_text(text)
print(f"[patch] agent os media gateway applied: {path}")
PY

  patched=1
done

if [ "$patched" = 0 ]; then
  echo "[patch] no new agent os media gateway bundles patched"
fi
