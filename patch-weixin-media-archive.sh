#!/bin/bash
# Persist inbound WeChat media so the agent can retrieve/send it later.
set -euo pipefail

targets=(
  "/home/ubuntu/weixin-agent/node_modules/weixin-agent-sdk/dist/index.mjs"
  "/home/ubuntu/.npm-global/lib/node_modules/weixin-acp/node_modules/weixin-agent-sdk/dist/index.mjs"
  /home/ubuntu/.npm/_npx/*/node_modules/weixin-agent-sdk/dist/index.mjs
)

patched=0
for target in "${targets[@]}"; do
  [ -f "$target" ] || continue

  if grep -q "MEDIA_ARCHIVE_DIR" "$target" && grep -q "getExtensionFromFilenameOrContent" "$target"; then
    echo "[patch] inbound media archive already applied: $target"
    continue
  fi

  python3 - "$target" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()

mime_helper_old = """function getMimeFromFilename(filename) {
\treturn EXTENSION_TO_MIME[path.extname(filename).toLowerCase()] ?? "application/octet-stream";
}
/** Get file extension from MIME type. Returns ".bin" for unknown types. */"""
mime_helper_new = """function getMimeFromFilename(filename) {
\treturn EXTENSION_TO_MIME[path.extname(filename).toLowerCase()] ?? "application/octet-stream";
}
function getMimeFromMagicBytes(filename) {
\tlet fd;
\ttry {
\t\tfd = fs.openSync(filename, "r");
\t\tconst buffer = Buffer.alloc(16);
\t\tconst bytesRead = fs.readSync(fd, buffer, 0, buffer.length, 0);
\t\tconst sig = buffer.subarray(0, bytesRead);
\t\tif (sig[0] === 0xff && sig[1] === 0xd8 && sig[2] === 0xff) return "image/jpeg";
\t\tif (sig[0] === 0x89 && sig[1] === 0x50 && sig[2] === 0x4e && sig[3] === 0x47) return "image/png";
\t\tif (sig.subarray(0, 3).toString("ascii") === "GIF") return "image/gif";
\t\tif (sig.subarray(0, 4).toString("ascii") === "RIFF" && sig.subarray(8, 12).toString("ascii") === "WEBP") return "image/webp";
\t\tif (sig[0] === 0x42 && sig[1] === 0x4d) return "image/bmp";
\t\tif (sig.subarray(4, 8).toString("ascii") === "ftyp") {
\t\t\tconst brand = sig.subarray(8, 12).toString("ascii");
\t\t\tif (brand === "heic" || brand === "heix" || brand === "hevc" || brand === "hevx" || brand === "mif1" || brand === "msf1") return "image/heic";
\t\t\treturn "video/mp4";
\t\t}
\t} catch {}
\tfinally {
\t\tif (fd !== void 0) {
\t\t\ttry {
\t\t\t\tfs.closeSync(fd);
\t\t\t} catch {}
\t\t}
\t}
}
function getMimeFromFilenameOrContent(filename) {
\tconst mime = getMimeFromFilename(filename);
\tif (mime !== "application/octet-stream") return mime;
\treturn getMimeFromMagicBytes(filename) ?? mime;
}
function getExtensionFromFilenameOrContent(filename, mimeType) {
\tconst filenameExt = path.extname(filename).toLowerCase();
\tif (EXTENSION_TO_MIME[filenameExt]) return filenameExt;
\tconst sniffedMime = getMimeFromMagicBytes(filename);
\tif (sniffedMime) {
\t\tconst sniffedExt = getExtensionFromMime(sniffedMime);
\t\tif (sniffedExt !== ".bin") return sniffedExt;
\t}
\tif (mimeType) {
\t\tconst mimeExt = getExtensionFromMime(mimeType);
\t\tif (mimeExt !== ".bin") return mimeExt;
\t}
\treturn filenameExt || ".bin";
}
/** Get file extension from MIME type. Returns ".bin" for unknown types. */"""

const_old = """const MEDIA_TEMP_DIR = "/tmp/weixin-agent/media";
/** Save a buffer to a temporary file, returning the file path. */"""
const_new = """const MEDIA_TEMP_DIR = "/tmp/weixin-agent/media";
const MEDIA_ARCHIVE_DIR = path.join(resolveWeixinStateDir(), "media-archive");
const MEDIA_ARCHIVE_INDEX_PATH = path.join(MEDIA_ARCHIVE_DIR, "index.jsonl");
const MEMORY_RECENT_CONTEXT_PATH = process.env.WEIXIN_AGENT_RECENT_CONTEXT_PATH?.trim() || path.join(process.cwd(), "memory", "recent-context.md");
/** Save a buffer to a temporary file, returning the file path. */"""

helper_anchor = """\tawait fs$1.writeFile(filePath, buffer);
\treturn { path: filePath };
}
/** Extract raw text from item_list (for slash command detection). */"""
helper_new = """\tawait fs$1.writeFile(filePath, buffer);
\treturn { path: filePath };
}
function safeTextForMemory(text) {
\treturn String(text ?? "").replace(/\\s+/g, " ").trim().slice(0, 160);
}
async function archiveInboundMedia(media, full, textBody, requestId) {
\tif (!media?.filePath) return media;
\tconst now = new Date();
\tconst dateDir = now.toISOString().slice(0, 10);
\tconst conversationId = full.from_user_id ?? "";
\tconst dir = path.join(MEDIA_ARCHIVE_DIR, dateDir);
\tawait fs$1.mkdir(dir, { recursive: true });
\tconst ext = getExtensionFromFilenameOrContent(media.filePath, media.mimeType);
\tconst name = `${now.toISOString().replace(/[:.]/g, "-")}-${media.type}-${crypto.randomBytes(4).toString("hex")}${ext}`;
\tconst archivedPath = path.join(dir, name);
\tawait fs$1.copyFile(media.filePath, archivedPath);
\tconst record = {
\t\tsavedAt: now.toISOString(),
\t\trequestId,
\t\tconversationId,
\t\tmessageId: full.msg_id ?? full.message_id ?? full.client_id ?? null,
\t\ttype: media.type,
\t\tmimeType: media.mimeType,
\t\tfilePath: archivedPath,
\t\toriginalFilePath: media.filePath,
\t\ttext: safeTextForMemory(textBody)
\t};
\tawait fs$1.mkdir(path.dirname(MEDIA_ARCHIVE_INDEX_PATH), { recursive: true });
\tawait fs$1.appendFile(MEDIA_ARCHIVE_INDEX_PATH, `${JSON.stringify(record)}\\n`, "utf-8");
\ttry {
\t\tawait fs$1.mkdir(path.dirname(MEMORY_RECENT_CONTEXT_PATH), { recursive: true });
\t\tconst label = media.type === "image" ? "图片" : media.type;
\t\tconst noteText = record.text ? `；随图文字：${record.text}` : "";
\t\tawait fs$1.appendFile(
\t\t\tMEMORY_RECENT_CONTEXT_PATH,
\t\t\t`[${record.savedAt}] [weixin-media] 用户发送了一张${label}，已保存到 ${archivedPath}${noteText}。如果用户之后要求发回这张图/之前那张图/Picture A，可用 [send_file:${archivedPath}] 发送。\\n`,
\t\t\t"utf-8"
\t\t);
\t} catch (err) {
\t\tlogger.warn(`[weixin-media] memory append failed: ${String(err)}`);
\t}
\tlogger.info(`[weixin-media] archived ${media.type} requestId=${requestId} path=${archivedPath}`);
\treturn {
\t\t...media,
\t\tfilePath: archivedPath,
\t\tarchivedFrom: media.filePath
\t};
}
/** Extract raw text from item_list (for slash command detection). */"""

media_old = """\t\telse if (downloaded.decryptedVoicePath) media = {
\t\t\ttype: "audio",
\t\t\tfilePath: downloaded.decryptedVoicePath,
\t\t\tmimeType: downloaded.voiceMediaType ?? "audio/wav"
\t\t};
\t} catch (err) {"""
media_new = """\t\telse if (downloaded.decryptedVoicePath) media = {
\t\t\ttype: "audio",
\t\t\tfilePath: downloaded.decryptedVoicePath,
\t\t\tmimeType: downloaded.voiceMediaType ?? "audio/wav"
\t\t};
\t\tif (media) media = await archiveInboundMedia(media, full, textBody, requestId);
\t} catch (err) {"""

request_old = """\tconst request = {
\t\tconversationId: full.from_user_id ?? "",
\t\ttext: bodyFromItemList(full.item_list),
\t\tmedia
\t};"""
request_new = """\tconst mediaMemoryNote = media ? `\\n\\n[系统记录：用户刚发送的${media.type === "image" ? "图片" : media.type}已永久保存到 VPS：${media.filePath}。如果用户以后要求找回、处理或发回这张图，请使用这个路径；需要发回原图时回复 [send_file:${media.filePath}]。]` : "";
\tconst request = {
\t\tconversationId: full.from_user_id ?? "",
\t\ttext: `${bodyFromItemList(full.item_list)}${mediaMemoryNote}`,
\t\tmedia
\t};"""

if "getMimeFromMagicBytes" not in text:
    if mime_helper_old not in text:
        raise SystemExit(f"mime helper anchor not found in {path}")
    text = text.replace(mime_helper_old, mime_helper_new, 1)

text = text.replace("const mime = getMimeFromFilename(filePath);", "const mime = getMimeFromFilenameOrContent(filePath);", 1)

if "MEDIA_ARCHIVE_DIR" not in text:
    for old, new, label in [
        (const_old, const_new, "constants"),
        (helper_anchor, helper_new, "archive helper"),
        (media_old, media_new, "media hook"),
        (request_old, request_new, "request note"),
    ]:
        if old not in text:
            raise SystemExit(f"{label} anchor not found in {path}")
        text = text.replace(old, new, 1)
else:
    text = text.replace(
        'let ext = path.extname(media.filePath);\n\tif (!ext || ext === ".bin") ext = getExtensionFromMime(media.mimeType);',
        'const ext = getExtensionFromFilenameOrContent(media.filePath, media.mimeType);',
        1
    )

path.write_text(text)
PY

  echo "[patch] inbound media archive applied: $target"
  patched=1
done

if [ "$patched" = 0 ]; then
  echo "[patch] no new inbound media archive bundles patched"
fi
