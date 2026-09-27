#!/bin/bash
# Prevent bad response.media / [send_file] paths from crashing the whole WeChat turn.
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
    echo "[patch] weixin-agent-sdk bundle not found, skipping"
    exit 0
fi

PATCHED=0
for TARGET in "${CANDIDATES[@]}"; do
    if grep -q "weixin response media guard patch" "$TARGET"; then
        echo "[patch] response media guard already applied: $TARGET"
        continue
    fi

    echo "[patch] Patching response media guard: $TARGET ..."
    TARGET="$TARGET" node --input-type=module <<'NODE'
import fs from 'node:fs';
const target = process.env.TARGET;
let src = fs.readFileSync(target, 'utf8');
const marker = 'weixin response media guard patch';
if (src.includes(marker)) process.exit(0);

const before = `\t\tif (response.media) {\n\t\t\tlet filePath;\n\t\t\tconst mediaUrl = response.media.url;\n\t\t\tif (mediaUrl.startsWith("http://") || mediaUrl.startsWith("https://")) filePath = await downloadRemoteImageToTemp(mediaUrl, path.join(MEDIA_TEMP_DIR, "outbound"));\n\t\t\telse filePath = path.isAbsolute(mediaUrl) ? mediaUrl : path.resolve(mediaUrl);\n\t\t\tawait sendWeixinMediaFile({\n\t\t\t\tfilePath,\n\t\t\t\tto,\n\t\t\t\ttext: response.text ? markdownToPlainText(response.text) : "",\n\t\t\t\topts: {\n\t\t\t\t\tbaseUrl: deps.baseUrl,\n\t\t\t\t\ttoken: deps.token,\n\t\t\t\t\tcontextToken\n\t\t\t\t},\n\t\t\t\tcdnBaseUrl: deps.cdnBaseUrl\n\t\t\t});\n\t\t} else if (response.text) await sendMessageWeixin({`;

const after = `\t\tif (response.media) {\n\t\t\tlet filePath;\n\t\t\tconst mediaUrl = String(response.media.url ?? "");\n\t\t\tif (mediaUrl.startsWith("http://") || mediaUrl.startsWith("https://")) filePath = await downloadRemoteImageToTemp(mediaUrl, path.join(MEDIA_TEMP_DIR, "outbound"));\n\t\t\telse filePath = path.isAbsolute(mediaUrl) ? mediaUrl : path.resolve(mediaUrl);\n\t\t\t// weixin response media guard patch: a bad [send_file] path should not crash the whole turn.\n\t\t\tconst mediaFileOk = mediaUrl && fs.existsSync(filePath) && fs.statSync(filePath).isFile();\n\t\t\tif (!mediaFileOk) {\n\t\t\t\tconst fallbackText = [\n\t\t\t\t\tresponse.text ? markdownToPlainText(response.text) : "",\n\t\t\t\t\t\`文件没找到，没法发送：\${path.basename(filePath)}\`\n\t\t\t\t].filter(Boolean).join("\\n\\n");\n\t\t\t\tlogger.warn(\`[weixin-response-media-guard] missing media path=\${filePath} raw=\${mediaUrl}\`);\n\t\t\t\tawait sendMessageWeixin({\n\t\t\t\t\tto,\n\t\t\t\t\ttext: fallbackText,\n\t\t\t\t\topts: {\n\t\t\t\t\t\tbaseUrl: deps.baseUrl,\n\t\t\t\t\t\ttoken: deps.token,\n\t\t\t\t\t\tcontextToken\n\t\t\t\t\t}\n\t\t\t\t});\n\t\t\t} else await sendWeixinMediaFile({\n\t\t\t\tfilePath,\n\t\t\t\tto,\n\t\t\t\ttext: response.text ? markdownToPlainText(response.text) : "",\n\t\t\t\topts: {\n\t\t\t\t\tbaseUrl: deps.baseUrl,\n\t\t\t\t\ttoken: deps.token,\n\t\t\t\t\tcontextToken\n\t\t\t\t},\n\t\t\t\tcdnBaseUrl: deps.cdnBaseUrl\n\t\t\t});\n\t\t} else if (response.text) await sendMessageWeixin({`;

if (!src.includes(before)) {
  throw new Error('response.media send block not found');
}
src = src.replace(before, after);
fs.writeFileSync(target, src, 'utf8');
NODE
    PATCHED=$((PATCHED + 1))
done

if [ "$PATCHED" -eq 0 ]; then
    echo "[patch] no new response media guard bundles patched"
else
    echo "[patch] patched $PATCHED response media guard bundle(s)"
fi
