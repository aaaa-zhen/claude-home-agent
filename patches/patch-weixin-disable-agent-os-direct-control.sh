#!/bin/bash
# Disable the experimental direct Agent OS home-control WeChat gateway.
set -euo pipefail

targets=(
  "/Users/zhen/home-agent/weixin-agent/node_modules/weixin-agent-sdk/dist/index.mjs"
  "/opt/homebrew/lib/node_modules/weixin-acp/node_modules/weixin-agent-sdk/dist/index.mjs"
  /Users/zhen/.npm/_npx/*/node_modules/weixin-agent-sdk/dist/index.mjs
)

patched=0
for target in "${targets[@]}"; do
  [ -f "$target" ] || continue

  python3 - "$target" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()
original = text
old = '\tif (await maybeHandleAgentOSControl(textBody, full, deps, requestId, requestStartedAt)) return;\n'
new = '\t// direct Agent OS home-control gateway disabled: keep accuracy by routing through the main agent.\n'
if old in text:
    text = text.replace(old, new, 1)

helper_start = "// weixin agent os control patch: route fast home tasks through Agent OS v1."
helper_end = "/** Find the first downloadable media item from a message. */"
if helper_start in text and helper_end in text:
    start = text.index(helper_start)
    end = text.index(helper_end, start)
    text = text[:start] + text[end:]

if text != original:
    path.write_text(text)
    print(f"[patch] disabled direct agent os control gateway: {path}")
else:
    print(f"[patch] direct agent os control gateway already disabled: {path}")
PY
  patched=1
done

if [ "$patched" = 0 ]; then
  echo "[patch] no weixin-agent-sdk bundles found"
fi
