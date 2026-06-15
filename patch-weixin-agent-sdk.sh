#!/bin/bash
# Patch weixin-agent-sdk runtime behavior for this bot:
# - structured per-message requestId logs
# - video CDN upload fallback to file attachment on server errors
set -euo pipefail

CANDIDATES=()
for ROOT in \
    /home/ubuntu/weixin-agent/node_modules \
    /home/ubuntu/.npm-global/lib/node_modules \
    /home/ubuntu/.npm/_npx; do
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
    if grep -q "weixin runtime hardening patch" "$TARGET"; then
        echo "[patch] weixin-agent-sdk hardening already applied: $TARGET"
        continue
    fi

    echo "[patch] Patching weixin-agent-sdk hardening: $TARGET ..."
    TARGET="$TARGET" node --input-type=module <<'NODE'
import fs from 'node:fs';
const target = process.env.TARGET;
let src = fs.readFileSync(target, 'utf8');
const marker = 'weixin runtime hardening patch';
if (src.includes(marker)) process.exit(0);

const startBefore = `async function processOneMessage(full, deps) {\n\tconst receivedAt = Date.now();\n\tconst textBody = extractTextBody(full.item_list);`;
const startAfter = `async function processOneMessage(full, deps) {\n\tconst receivedAt = Date.now();\n\t// weixin runtime hardening patch: stable request logging for debugging wrong/late replies.\n\tconst requestId = String(full.msg_id ?? full.message_id ?? full.client_id ?? full.create_time_ms ?? receivedAt) + "-" + crypto.randomBytes(3).toString("hex");\n\tconst requestStartedAt = Date.now();\n\tconst textBody = extractTextBody(full.item_list);\n\tconst itemTypes = full.item_list?.map((item) => item.type).join(",") ?? "none";\n\tlogger.info(\`[weixin-msg] start requestId=\${requestId} from=\${full.from_user_id ?? ""} types=\${itemTypes} text=\${JSON.stringify(textBody.slice(0, 120))}\`);`;
if (!src.includes(startBefore)) throw new Error('processOneMessage start block not found');
src = src.replace(startBefore, startAfter);

const successBefore = `\t\t} else if (response.text) await sendMessageWeixin({\n\t\t\tto,\n\t\t\ttext: markdownToPlainText(response.text),\n\t\t\topts: {\n\t\t\t\tbaseUrl: deps.baseUrl,\n\t\t\t\ttoken: deps.token,\n\t\t\t\tcontextToken\n\t\t\t}\n\t\t});\n\t} catch (err) {`;
const successAfter = `\t\t} else if (response.text) await sendMessageWeixin({\n\t\t\tto,\n\t\t\ttext: markdownToPlainText(response.text),\n\t\t\topts: {\n\t\t\t\tbaseUrl: deps.baseUrl,\n\t\t\t\ttoken: deps.token,\n\t\t\t\tcontextToken\n\t\t\t}\n\t\t});\n\t\tlogger.info(\`[weixin-msg] done requestId=\${requestId} durationMs=\${Date.now() - requestStartedAt} response=\${JSON.stringify((response.text ?? "").slice(0, 120))} media=\${response.media?.type ?? "none"}\`);\n\t} catch (err) {`;
if (!src.includes(successBefore)) throw new Error('processOneMessage success block not found');
src = src.replace(successBefore, successAfter);

const errorBefore = `\t} catch (err) {\n\t\tlogger.error(\`processOneMessage: agent or send failed: \${err instanceof Error ? err.stack ?? err.message : JSON.stringify(err)}\`);`;
const errorAfter = `\t} catch (err) {\n\t\tlogger.error(\`[weixin-msg] error requestId=\${requestId} durationMs=\${Date.now() - requestStartedAt} error=\${err instanceof Error ? err.message : JSON.stringify(err)}\`);\n\t\tlogger.error(\`processOneMessage: agent or send failed: \${err instanceof Error ? err.stack ?? err.message : JSON.stringify(err)}\`);`;
if (!src.includes(errorBefore)) throw new Error('processOneMessage error block not found');
src = src.replace(errorBefore, errorAfter);

const videoBefore = `\tif (mime.startsWith("video/")) {\n\t\tlogger.info(\`[weixin] sendWeixinMediaFile: uploading video filePath=\${filePath} to=\${to}\`);\n\t\tconst uploaded = await uploadVideoToWeixin({\n\t\t\tfilePath,\n\t\t\ttoUserId: to,\n\t\t\topts: uploadOpts,\n\t\t\tcdnBaseUrl\n\t\t});\n\t\tlogger.info(\`[weixin] sendWeixinMediaFile: video upload done filekey=\${uploaded.filekey} size=\${uploaded.fileSize}\`);\n\t\treturn sendVideoMessageWeixin({\n\t\t\tto,\n\t\t\ttext,\n\t\t\tuploaded,\n\t\t\topts\n\t\t});\n\t}\n\tif (mime.startsWith("image/")) {`;
const videoAfter = `\tif (mime.startsWith("video/")) {\n\t\tlogger.info(\`[weixin] sendWeixinMediaFile: uploading video filePath=\${filePath} to=\${to}\`);\n\t\ttry {\n\t\t\tconst uploaded = await uploadVideoToWeixin({\n\t\t\t\tfilePath,\n\t\t\t\ttoUserId: to,\n\t\t\t\topts: uploadOpts,\n\t\t\t\tcdnBaseUrl\n\t\t\t});\n\t\t\tlogger.info(\`[weixin] sendWeixinMediaFile: video upload done filekey=\${uploaded.filekey} size=\${uploaded.fileSize}\`);\n\t\t\treturn sendVideoMessageWeixin({\n\t\t\t\tto,\n\t\t\t\ttext,\n\t\t\t\tuploaded,\n\t\t\t\topts\n\t\t\t});\n\t\t} catch (err) {\n\t\t\tlogger.error(\`[weixin] sendWeixinMediaFile: video upload failed, falling back to file attachment filePath=\${filePath} err=\${String(err)}\`);\n\t\t\tconst fileName = path.basename(filePath);\n\t\t\tconst uploaded = await uploadFileAttachmentToWeixin({\n\t\t\t\tfilePath,\n\t\t\t\tfileName,\n\t\t\t\ttoUserId: to,\n\t\t\t\topts: uploadOpts,\n\t\t\t\tcdnBaseUrl\n\t\t\t});\n\t\t\tlogger.info(\`[weixin] sendWeixinMediaFile: video fallback file upload done filekey=\${uploaded.filekey} size=\${uploaded.fileSize}\`);\n\t\t\treturn sendFileMessageWeixin({\n\t\t\t\tto,\n\t\t\t\ttext,\n\t\t\t\tfileName,\n\t\t\t\tuploaded,\n\t\t\t\topts\n\t\t\t});\n\t\t}\n\t}\n\tif (mime.startsWith("image/")) {`;
if (!src.includes(videoBefore)) throw new Error('video send block not found');
src = src.replace(videoBefore, videoAfter);

fs.writeFileSync(target, src, 'utf8');
NODE
    PATCHED=$((PATCHED + 1))
done

if [ "$PATCHED" -eq 0 ]; then
    echo "[patch] no new weixin-agent-sdk bundles patched"
else
    echo "[patch] patched $PATCHED weixin-agent-sdk bundle(s)"
fi
