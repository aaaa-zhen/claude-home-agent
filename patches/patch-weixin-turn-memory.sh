#!/bin/bash
# Persist direct ACP replies (media/file turns) into recent-context so the v2
# text gateway can keep continuity on the next message.
set -euo pipefail

CANDIDATES=()
for ROOT in \
    /Users/zhen/home-agent/weixin-agent/node_modules \
    /opt/homebrew/lib/node_modules \
    /Users/zhen/.npm/_npx; do
    [ -d "$ROOT" ] || continue
    while IFS= read -r file; do CANDIDATES+=("$file"); done < <(
        find "$ROOT" -path '*/weixin-agent-sdk/dist/index.mjs' -type f 2>/dev/null | sort
    )
done

if [ "${#CANDIDATES[@]}" -eq 0 ]; then
    echo "[patch] weixin-agent-sdk bundle not found, skipping turn memory"
    exit 0
fi

PATCHED=0
for TARGET in "${CANDIDATES[@]}"; do
    if grep -q "weixin turn memory patch" "$TARGET"; then
        if ! grep -q "localTimestampForTurnMemory" "$TARGET"; then
            TARGET="$TARGET" node --input-type=module <<'NODE'
import fs from 'node:fs';

const target = process.env.TARGET;
let src = fs.readFileSync(target, 'utf8');
const anchor = `function safeTurnMemoryText(text, max = 280) {
\treturn String(text ?? "").replace(/\\s+/g, " ").trim().slice(0, max);
}`;
const replacement = `function safeTurnMemoryText(text, max = 280) {
\treturn String(text ?? "").replace(/\\s+/g, " ").trim().slice(0, max);
}
function localTimestampForTurnMemory(date = new Date()) {
\tconst pad = (value) => String(value).padStart(2, "0");
\treturn \`\${date.getFullYear()}-\${pad(date.getMonth() + 1)}-\${pad(date.getDate())} \${pad(date.getHours())}:\${pad(date.getMinutes())}:\${pad(date.getSeconds())}\`;
}`;
if (!src.includes(anchor)) throw new Error('safeTurnMemoryText anchor not found');
src = src.replace(anchor, replacement);
src = src.replace('`[${new Date().toISOString()}] [wechat-direct] ${user} → ${reply}\\n`,', '`[${localTimestampForTurnMemory()}] [wechat-direct] ${user} → ${reply}\\n`,');
fs.writeFileSync(target, src, 'utf8');
NODE
            echo "[patch] weixin turn memory timestamp normalized: $TARGET"
        else
            echo "[patch] weixin turn memory already applied: $TARGET"
        fi
        continue
    fi

    if ! grep -q "MEMORY_RECENT_CONTEXT_PATH" "$TARGET"; then
        echo "[patch] turn memory needs media archive patch first, skipping: $TARGET"
        continue
    fi

    echo "[patch] Patching weixin turn memory: $TARGET ..."
    TARGET="$TARGET" node --input-type=module <<'NODE'
import fs from 'node:fs';

const target = process.env.TARGET;
let src = fs.readFileSync(target, 'utf8');
if (src.includes('weixin turn memory patch')) process.exit(0);

const helperAnchor = `/** Find the first downloadable media item from a message. */`;
const helper = `// weixin turn memory patch: persist direct ACP replies for cross-route continuity.
function safeTurnMemoryText(text, max = 280) {
\treturn String(text ?? "").replace(/\\s+/g, " ").trim().slice(0, max);
}
function localTimestampForTurnMemory(date = new Date()) {
\tconst pad = (value) => String(value).padStart(2, "0");
\treturn \`\${date.getFullYear()}-\${pad(date.getMonth() + 1)}-\${pad(date.getDate())} \${pad(date.getHours())}:\${pad(date.getMinutes())}:\${pad(date.getSeconds())}\`;
}
async function appendWeixinTurnMemory(userText, replyText, requestId) {
\tconst reply = safeTurnMemoryText(replyText, 360);
\tif (!reply) return;
\ttry {
\t\tawait fs$1.mkdir(path.dirname(MEMORY_RECENT_CONTEXT_PATH), { recursive: true });
\t\tconst user = safeTurnMemoryText(userText, 220) || "(媒体消息)";
\t\tawait fs$1.appendFile(
\t\t\tMEMORY_RECENT_CONTEXT_PATH,
\t\t\t\`[\${localTimestampForTurnMemory()}] [wechat-direct] \${user} → \${reply}\\n\`,
\t\t\t"utf-8"
\t\t);
\t} catch (err) {
\t\tlogger.warn(\`[weixin-turn-memory] append failed requestId=\${requestId}: \${String(err)}\`);
\t}
}
`;
if (!src.includes(helperAnchor)) throw new Error('findMediaItem anchor not found');
src = src.replace(helperAnchor, `${helper}${helperAnchor}`);

const doneAnchor = `\t\tlogger.info(\`[weixin-msg] done requestId=\${requestId} durationMs=\${Date.now() - requestStartedAt} response=\${JSON.stringify((response.text ?? "").slice(0, 120))} media=\${response.media?.type ?? "none"}\`);`;
const doneReplacement = `\t\tawait appendWeixinTurnMemory(
\t\t\ttextBody || (media ? "[媒体消息]" : bodyFromItemList(full.item_list)),
\t\t\tresponse.text ?? (response.media ? "[媒体回复]" : ""),
\t\t\trequestId
\t\t);
${doneAnchor}`;
if (!src.includes(doneAnchor)) throw new Error('done logger anchor not found');
src = src.replace(doneAnchor, doneReplacement);

fs.writeFileSync(target, src, 'utf8');
NODE
    PATCHED=$((PATCHED + 1))
done

if [ "$PATCHED" -eq 0 ]; then
    echo "[patch] no new weixin turn memory bundles patched"
else
    echo "[patch] patched $PATCHED weixin turn memory bundle(s)"
fi
