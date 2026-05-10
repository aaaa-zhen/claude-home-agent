#!/bin/bash
# Patch weixin-acp ResponseCollector to support [send_file:/path] in text responses.
# The package may be installed globally or under npm's temporary npx cache.
set -euo pipefail

CANDIDATES=()
if [ -d /home/ubuntu/.npm-global/lib/node_modules/weixin-acp/dist ]; then
    while IFS= read -r file; do CANDIDATES+=("$file"); done < <(
        find /home/ubuntu/.npm-global/lib/node_modules/weixin-acp/dist \
            -maxdepth 1 -type f -name 'acp-agent-*.mjs' 2>/dev/null | sort
    )
fi
if [ -d /home/ubuntu/.npm/_npx ]; then
    while IFS= read -r file; do CANDIDATES+=("$file"); done < <(
        find /home/ubuntu/.npm/_npx -path '*/weixin-acp/dist/acp-agent-*.mjs' \
            -type f 2>/dev/null | sort
    )
fi

if [ "${#CANDIDATES[@]}" -eq 0 ]; then
    echo "[patch] weixin-acp bundle not found, skipping"
    exit 0
fi

PATCHED=0
for TARGET in "${CANDIDATES[@]}"; do
    if grep -q "send_file patch start" "$TARGET"; then
        echo "[patch] send_file patch already applied: $TARGET"
        continue
    fi

    if ! grep -q 'const text = this\.textChunks\.join("");' "$TARGET"; then
        echo "[patch] unsupported bundle shape, skipping: $TARGET"
        continue
    fi

    echo "[patch] Patching $TARGET ..."
    TARGET="$TARGET" node --input-type=module <<'NODE'
import fs from 'node:fs';
const target = process.env.TARGET;
let src = fs.readFileSync(target, 'utf8');
const before = `\t\tconst text = this.textChunks.join("");\n\t\tif (text) response.text = text;\n\t\tif (this.imageData) {`;
const after = `\t\tlet text = this.textChunks.join("");\n\t\t// --- send_file patch start ---\n\t\tconst sendFileMatch = text.match(/\\[send_file:([^\\]]+)\\]/);\n\t\tif (sendFileMatch) {\n\t\t\tconst sendFilePath = sendFileMatch[1].trim();\n\t\t\ttext = text.replace(/\\s*\\[send_file:[^\\]]+\\]\\s*/g, "").trim();\n\t\t\tresponse.media = { type: "file", url: sendFilePath };\n\t\t}\n\t\t// --- send_file patch end ---\n\t\tif (text) response.text = text;\n\t\tif (this.imageData && !response.media) {`;
if (!src.includes(before)) {
  throw new Error('expected ResponseCollector block not found');
}
src = src.replace(before, after);
fs.writeFileSync(target, src, 'utf8');
NODE
    PATCHED=$((PATCHED + 1))
done

if [ "$PATCHED" -eq 0 ]; then
    echo "[patch] no new bundles patched"
else
    echo "[patch] patched $PATCHED bundle(s)"
fi
