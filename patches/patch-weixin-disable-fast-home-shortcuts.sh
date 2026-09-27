#!/bin/bash
# Remove HA status/control shortcuts from patched weixin-agent-sdk bundles.
set -euo pipefail

targets=(
  "/Users/zhen/home-agent/weixin-agent/node_modules/weixin-agent-sdk/dist/index.mjs"
  "/opt/homebrew/lib/node_modules/weixin-acp/node_modules/weixin-agent-sdk/dist/index.mjs"
  /Users/zhen/.npm/_npx/*/node_modules/weixin-agent-sdk/dist/index.mjs
)

changed=0
for target in "${targets[@]}"; do
  [ -f "$target" ] || continue

  python3 - "$target" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()
original = text

text = re.sub(
    r"// weixin fast home status patch:.*?(?=// weixin interrupt fast home patch: handle common device controls|/\*\* Extract raw text)",
    "",
    text,
    flags=re.S,
)
text = re.sub(
    r"// weixin interrupt fast home patch: handle common device controls.*?(?=/\*\* Extract raw text)",
    "",
    text,
    flags=re.S,
)
text = re.sub(
    r"\n\tif \(await maybeHandleFastHome(?:Control|Status)\([^\n]*\)\) return;",
    "",
    text,
)
text = text.replace(
    "// weixin interrupt fast home patch: keep receiving messages while ACP handles a long prompt.",
    "// weixin message queue patch: keep receiving messages while ACP handles a long prompt.",
)

if text != original:
    path.write_text(text)
    print(f"[patch] fast home shortcuts disabled: {path}")
else:
    print(f"[patch] fast home shortcuts already disabled: {path}")
PY

  changed=1
done

if [ "$changed" = 0 ]; then
  echo "[patch] no weixin-agent-sdk bundles found"
fi
