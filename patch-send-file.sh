#!/bin/bash
# Patch weixin-acp ResponseCollector to support [send_file:/path] in text responses
# This adds file/video/image sending capability via the [send_file:] tag

TARGET=$(find /home/ubuntu/.npm/_npx -name "acp-agent-*.mjs" -path "*/weixin-acp/dist/*" 2>/dev/null | head -1)

if [ -z "$TARGET" ]; then
    echo "[patch] weixin-acp bundle not found, skipping"
    exit 0
fi

if grep -q "send_file" "$TARGET"; then
    echo "[patch] send_file patch already applied"
    exit 0
fi

echo "[patch] Patching $TARGET ..."

sed -i 's/async toResponse() {/async toResponse() {\
		\/\/ --- send_file patch start ---/' "$TARGET"

sed -i '/const text = this\.textChunks\.join("");/c\
\t\tlet text = this.textChunks.join("");\
\t\tconst sendFileMatch = text.match(/\\[send_file:([^\\]]+)\\]/);\
\t\tif (sendFileMatch) {\
\t\t\tconst sendFilePath = sendFileMatch[1].trim();\
\t\t\ttext = text.replace(/\\s*\\[send_file:[^\\]]+\\]\\s*/g, "").trim();\
\t\t\tresponse.media = { type: "file", url: sendFilePath };\
\t\t}\
\t\t\/\/ --- send_file patch end ---' "$TARGET"

sed -i 's/if (this\.imageData) {/if (this.imageData \&\& !response.media) {/' "$TARGET"

echo "[patch] Done"
