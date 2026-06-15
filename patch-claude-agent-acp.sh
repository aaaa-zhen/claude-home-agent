#!/bin/bash
# Patch claude-agent-acp for the WeChat bridge's sequential prompt flow.
# Claude Code 2.1.x can stream assistant output before replaying the user message,
# so claude-agent-acp 0.23.0 may treat the current result as a background result
# and return the text on the next user prompt. For non-concurrent prompts, mark the
# just-pushed message as the active prompt immediately.
set -euo pipefail

CANDIDATES=()
if [ -f /home/ubuntu/weixin-agent/node_modules/@zed-industries/claude-agent-acp/dist/acp-agent.js ]; then
    CANDIDATES+=("/home/ubuntu/weixin-agent/node_modules/@zed-industries/claude-agent-acp/dist/acp-agent.js")
fi
if [ -f /home/ubuntu/.npm-global/lib/node_modules/@zed-industries/claude-agent-acp/dist/acp-agent.js ]; then
    CANDIDATES+=("/home/ubuntu/.npm-global/lib/node_modules/@zed-industries/claude-agent-acp/dist/acp-agent.js")
fi
if [ -d /home/ubuntu/.npm/_npx ]; then
    while IFS= read -r file; do CANDIDATES+=("$file"); done < <(
        find /home/ubuntu/.npm/_npx -path '*/@zed-industries/claude-agent-acp/dist/acp-agent.js' \
            -type f 2>/dev/null | sort
    )
fi

if [ "${#CANDIDATES[@]}" -eq 0 ]; then
    echo "[patch] claude-agent-acp bundle not found, skipping"
    exit 0
fi

PATCHED=0
for TARGET in "${CANDIDATES[@]}"; do
    if grep -q "weixin sequential prompt replay patch" "$TARGET"; then
        echo "[patch] sequential prompt replay patch already applied: $TARGET"
        continue
    fi

    echo "[patch] Patching sequential prompt replay: $TARGET ..."
    TARGET="$TARGET" node --input-type=module <<'NODE'
import fs from 'node:fs';
const target = process.env.TARGET;
let src = fs.readFileSync(target, 'utf8');
const marker = 'weixin sequential prompt replay patch';
if (src.includes(marker)) process.exit(0);

const before = `        else {\n            session.input.push(userMessage);\n        }\n        session.promptRunning = true;`;
const after = `        else {\n            session.input.push(userMessage);\n            // weixin sequential prompt replay patch: this bridge processes messages one at a time.\n            // Claude Code may stream output before replaying the user message, so do not let\n            // claude-agent-acp classify the current turn as a background task.\n            promptReplayed = true;\n        }\n        session.promptRunning = true;`;
if (!src.includes(before)) throw new Error('expected non-concurrent prompt block not found');
src = src.replace(before, after);
fs.writeFileSync(target, src, 'utf8');
NODE
    PATCHED=$((PATCHED + 1))
done

if [ "$PATCHED" -eq 0 ]; then
    echo "[patch] no new claude-agent-acp bundles patched"
else
    echo "[patch] patched $PATCHED claude-agent-acp bundle(s)"
fi
