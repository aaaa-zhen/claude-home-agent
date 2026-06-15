#!/bin/bash
# Patch weixin-acp so voice/audio messages do not become empty Claude prompts.
set -euo pipefail

CANDIDATES=()
if [ -d /home/ubuntu/weixin-agent/node_modules/weixin-acp/dist ]; then
    while IFS= read -r file; do CANDIDATES+=("$file"); done < <(
        find /home/ubuntu/weixin-agent/node_modules/weixin-acp/dist -maxdepth 1 -type f -name 'acp-agent-*.mjs' 2>/dev/null | sort
    )
fi
if [ -d /home/ubuntu/.npm-global/lib/node_modules/weixin-acp/dist ]; then
    while IFS= read -r file; do CANDIDATES+=("$file"); done < <(
        find /home/ubuntu/.npm-global/lib/node_modules/weixin-acp/dist -maxdepth 1 -type f -name 'acp-agent-*.mjs' 2>/dev/null | sort
    )
fi
if [ -d /home/ubuntu/.npm/_npx ]; then
    while IFS= read -r file; do CANDIDATES+=("$file"); done < <(
        find /home/ubuntu/.npm/_npx -path '*/weixin-acp/dist/acp-agent-*.mjs' -type f 2>/dev/null | sort
    )
fi

if [ "${#CANDIDATES[@]}" -eq 0 ]; then
    echo "[patch] weixin-acp bundle not found, skipping audio patch"
    exit 0
fi

PATCHED=0
for TARGET in "${CANDIDATES[@]}"; do
    if grep -q "audio placeholder patch" "$TARGET"; then
        echo "[patch] audio placeholder already applied: $TARGET"
        continue
    fi

    echo "[patch] Patching audio placeholder: $TARGET ..."
    TARGET="$TARGET" node --input-type=module <<'NODE'
import fs from 'node:fs';
const target = process.env.TARGET;
let src = fs.readFileSync(target, 'utf8');
const marker = 'audio placeholder patch';
if (src.includes(marker)) process.exit(0);
const before = `\t\t\tcase "audio":\n\t\t\t\tblocks.push({\n\t\t\t\t\ttype: "audio",\n\t\t\t\t\tdata: base64,\n\t\t\t\t\tmimeType\n\t\t\t\t});\n\t\t\t\tbreak;`;
const after = `\t\t\tcase "audio":\n\t\t\t\t// audio placeholder patch: claude-agent-acp currently drops audio blocks,\n\t\t\t\t// which creates an empty user message and can poison the next turn.\n\t\t\t\tblocks.push({\n\t\t\t\t\ttype: "text",\n\t\t\t\t\ttext: "[用户发送了一条语音消息，但当前微信 Agent 链路暂不支持直接听语音。请简短告诉用户改发文字，或让用户描述语音内容。]"\n\t\t\t\t});\n\t\t\t\tbreak;`;
if (!src.includes(before)) throw new Error('audio block not found');
src = src.replace(before, after);
fs.writeFileSync(target, src, 'utf8');
NODE
    PATCHED=$((PATCHED + 1))
done

if [ "$PATCHED" -eq 0 ]; then
    echo "[patch] no new weixin-acp audio bundles patched"
else
    echo "[patch] patched $PATCHED weixin-acp audio bundle(s)"
fi
