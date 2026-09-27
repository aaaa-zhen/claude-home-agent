#!/bin/bash
# Patch weixin-acp ResponseCollector to send only the final Codex assistant message.
# Codex can emit user-visible progress messages before the final answer; WeChat
# should receive the last assistant message only.
set -euo pipefail

CANDIDATES=()
if [ -d /Users/zhen/home-agent/weixin-agent/node_modules/weixin-acp/dist ]; then
    while IFS= read -r file; do CANDIDATES+=("$file"); done < <(
        find /Users/zhen/home-agent/weixin-agent/node_modules/weixin-acp/dist \
            -maxdepth 1 -type f -name 'acp-agent-*.mjs' 2>/dev/null | sort
    )
fi
if [ -d /opt/homebrew/lib/node_modules/weixin-acp/dist ]; then
    while IFS= read -r file; do CANDIDATES+=("$file"); done < <(
        find /opt/homebrew/lib/node_modules/weixin-acp/dist \
            -maxdepth 1 -type f -name 'acp-agent-*.mjs' 2>/dev/null | sort
    )
fi
if [ -d /Users/zhen/.npm/_npx ]; then
    while IFS= read -r file; do CANDIDATES+=("$file"); done < <(
        find /Users/zhen/.npm/_npx -path '*/weixin-acp/dist/acp-agent-*.mjs' \
            -type f 2>/dev/null | sort
    )
fi

if [ "${#CANDIDATES[@]}" -eq 0 ]; then
    echo "[patch] weixin-acp bundle not found, skipping codex final-message patch"
    exit 0
fi

PATCHED=0
for TARGET in "${CANDIDATES[@]}"; do
    if grep -q "codex final message patch" "$TARGET"; then
        echo "[patch] codex final-message patch already applied: $TARGET"
        continue
    fi

    echo "[patch] Patching Codex final-message behavior: $TARGET ..."
    TARGET="$TARGET" node --input-type=module <<'NODE'
import fs from 'node:fs';
const target = process.env.TARGET;
let src = fs.readFileSync(target, 'utf8');

const fieldsBefore = `\ttextChunks = [];\n\timageData = null;`;
const fieldsAfter = `\ttextChunks = [];\n\t// codex final message patch: keep ACP message boundaries so progress updates are not sent to WeChat.\n\ttextMessageIds = [];\n\ttextChunksByMessageId = new Map();\n\timageData = null;`;
if (!src.includes(fieldsBefore)) throw new Error('ResponseCollector fields block not found');
src = src.replace(fieldsBefore, fieldsAfter);

const pushBefore = `\t\t\tif (content.type === "text") this.textChunks.push(content.text);`;
const pushAfter = `\t\t\tif (content.type === "text") {\n\t\t\t\tconst messageId = update.messageId ?? "__default__";\n\t\t\t\tif (!this.textChunksByMessageId.has(messageId)) {\n\t\t\t\t\tthis.textChunksByMessageId.set(messageId, []);\n\t\t\t\t\tthis.textMessageIds.push(messageId);\n\t\t\t\t}\n\t\t\t\tthis.textChunksByMessageId.get(messageId).push(content.text);\n\t\t\t\tthis.textChunks.push(content.text);\n\t\t\t}`;
if (!src.includes(pushBefore)) throw new Error('ResponseCollector text push block not found');
src = src.replace(pushBefore, pushAfter);

const textBefore = `\t\tlet text = this.textChunks.join("");`;
const textAfter = `\t\tlet text = this.textChunks.join("");\n\t\t// --- codex final message patch start ---\n\t\tif (this.textMessageIds.length > 1) {\n\t\t\tconst finalMessageId = this.textMessageIds[this.textMessageIds.length - 1];\n\t\t\ttext = (this.textChunksByMessageId.get(finalMessageId) ?? []).join("");\n\t\t}\n\t\t// --- codex final message patch end ---`;
if (!src.includes(textBefore)) throw new Error('ResponseCollector text join block not found');
src = src.replace(textBefore, textAfter);

fs.writeFileSync(target, src, 'utf8');
NODE
    PATCHED=$((PATCHED + 1))
done

if [ "$PATCHED" -eq 0 ]; then
    echo "[patch] no new Codex final-message bundles patched"
else
    echo "[patch] patched $PATCHED Codex final-message bundle(s)"
fi
