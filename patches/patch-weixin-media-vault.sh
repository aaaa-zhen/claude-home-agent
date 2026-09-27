#!/bin/bash
# Upgrade inbound WeChat media archive into a project-local media vault.
set -euo pipefail

targets=(
  "/Users/zhen/home-agent/weixin-agent/node_modules/weixin-agent-sdk/dist/index.mjs"
  "/opt/homebrew/lib/node_modules/weixin-acp/node_modules/weixin-agent-sdk/dist/index.mjs"
  /Users/zhen/.npm/_npx/*/node_modules/weixin-agent-sdk/dist/index.mjs
)

patched=0
for target in "${targets[@]}"; do
  [ -f "$target" ] || continue

  if grep -q "MEDIA_ARCHIVE_SCHEMA_VERSION = 2" "$target" && grep -q "mediaBucketFor" "$target"; then
    echo "[patch] media vault already applied: $target"
    continue
  fi

  python3 - "$target" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()

if "archiveInboundMedia" not in text:
    raise SystemExit(f"inbound media archive patch must run before media vault patch: {path}")

constants_pattern = re.compile(
    r'const MEDIA_TEMP_DIR = "/tmp/weixin-agent/media";\n'
    r'(?:const MEDIA_ARCHIVE_SCHEMA_VERSION = 2;\n)?'
    r'(?:const MEDIA_ROOT_DIR = [^\n]+;\n)?'
    r'const MEDIA_ARCHIVE_DIR = [^\n]+;\n'
    r'const MEDIA_ARCHIVE_INDEX_PATH = [^\n]+;\n'
    r'const MEMORY_RECENT_CONTEXT_PATH = [^\n]+;\n'
)
constants_new = '''const MEDIA_TEMP_DIR = "/tmp/weixin-agent/media";
const MEDIA_ARCHIVE_SCHEMA_VERSION = 2;
const MEDIA_ROOT_DIR = process.env.WEIXIN_AGENT_MEDIA_DIR?.trim() || path.join(process.cwd(), "media");
const MEDIA_ARCHIVE_DIR = path.join(MEDIA_ROOT_DIR, "inbox");
const MEDIA_ARCHIVE_INDEX_PATH = path.join(MEDIA_ROOT_DIR, "index.jsonl");
const MEMORY_RECENT_CONTEXT_PATH = process.env.WEIXIN_AGENT_RECENT_CONTEXT_PATH?.trim() || path.join(process.cwd(), "memory", "recent-context.md");
'''

text, count = constants_pattern.subn(lambda _: constants_new, text, count=1)
if count != 1:
    raise SystemExit(f"media constants block not found in {path}")

helper_pattern = re.compile(
    r'function safeTextForMemory\(text\) \{[\s\S]*?\n\}\n/\*\* Extract raw text from item_list \(for slash command detection\)\. \*/',
    re.MULTILINE,
)
helper_new = '''function safeTextForMemory(text) {
\treturn String(text ?? "").replace(/\\s+/g, " ").trim().slice(0, 160);
}
function localDateDir(now) {
\tconst offsetMs = -now.getTimezoneOffset() * 6e4;
\treturn new Date(now.getTime() + offsetMs).toISOString().slice(0, 10);
}
function mediaBucketFor(media) {
\tconst type = String(media?.type ?? "").toLowerCase();
\tconst mime = String(media?.mimeType ?? "").toLowerCase();
\tif (type === "image" || mime.startsWith("image/")) return "images";
\tif (type === "video" || mime.startsWith("video/")) return "videos";
\tif (type === "audio" || mime.startsWith("audio/")) return "audio";
\treturn "files";
}
function mediaLabelFor(bucket) {
\tif (bucket === "images") return "图片";
\tif (bucket === "videos") return "视频";
\tif (bucket === "audio") return "语音";
\treturn "文件";
}
async function hashFileSha256(filePath) {
\treturn await new Promise((resolve) => {
\t\tconst hash = crypto.createHash("sha256");
\t\tconst stream = fs.createReadStream(filePath);
\t\tstream.on("data", (chunk) => hash.update(chunk));
\t\tstream.on("error", () => resolve(null));
\t\tstream.on("end", () => resolve(hash.digest("hex")));
\t});
}
async function fileMetadata(filePath) {
\ttry {
\t\tconst stat = await fs$1.stat(filePath);
\t\treturn {
\t\t\tsizeBytes: stat.size,
\t\t\tsha256: await hashFileSha256(filePath)
\t\t};
\t} catch {
\t\treturn {
\t\t\tsizeBytes: null,
\t\t\tsha256: null
\t\t};
\t}
}
async function archiveInboundMedia(media, full, textBody, requestId) {
\tif (!media?.filePath) return media;
\tconst now = new Date();
\tconst dateDir = localDateDir(now);
\tconst conversationId = full.from_user_id ?? "";
\tconst bucket = mediaBucketFor(media);
\tconst dir = path.join(MEDIA_ARCHIVE_DIR, bucket, dateDir);
\tawait fs$1.mkdir(dir, { recursive: true });
\tconst ext = getExtensionFromFilenameOrContent(media.filePath, media.mimeType);
\tconst id = `${dateDir}-${bucket}-${crypto.randomBytes(6).toString("hex")}`;
\tconst name = `${now.toISOString().replace(/[:.]/g, "-")}-${media.type}-${crypto.randomBytes(4).toString("hex")}${ext}`;
\tconst archivedPath = path.join(dir, name);
\tawait fs$1.copyFile(media.filePath, archivedPath);
\tconst meta = await fileMetadata(archivedPath);
\tconst actualMimeType = getMimeFromMagicBytes(archivedPath) ?? media.mimeType;
\tconst caption = safeTextForMemory(textBody);
\tconst record = {
\t\tid,
\t\tschemaVersion: MEDIA_ARCHIVE_SCHEMA_VERSION,
\t\tsavedAt: now.toISOString(),
\t\tdirection: "inbound",
\t\tsource: "weixin",
\t\trequestId,
\t\tconversationId,
\t\tmessageId: full.msg_id ?? full.message_id ?? full.client_id ?? null,
\t\ttype: media.type,
\t\tbucket,
\t\tmimeType: actualMimeType,
\t\tfilePath: archivedPath,
\t\tmediaUri: `media://${id}`,
\t\toriginalFilePath: media.filePath,
\t\tsizeBytes: meta.sizeBytes,
\t\tsha256: meta.sha256,
\t\ttext: caption,
\t\tcaption,
\t\ttags: []
\t};
\tawait fs$1.mkdir(path.dirname(MEDIA_ARCHIVE_INDEX_PATH), { recursive: true });
\tawait fs$1.appendFile(MEDIA_ARCHIVE_INDEX_PATH, `${JSON.stringify(record)}\\n`, "utf-8");
\ttry {
\t\tawait fs$1.mkdir(path.dirname(MEMORY_RECENT_CONTEXT_PATH), { recursive: true });
\t\tconst label = mediaLabelFor(bucket);
\t\tconst noteText = record.text ? `；随附文字：${record.text}` : "";
\t\tawait fs$1.appendFile(
\t\t\tMEMORY_RECENT_CONTEXT_PATH,
\t\t\t`[${record.savedAt}] [weixin-media] 收到${label}，归档为 ${record.mediaUri}，路径 ${archivedPath}${noteText}。需要发回原件时回复 [send_file:${archivedPath}]。\\n`,
\t\t\t"utf-8"
\t\t);
\t} catch (err) {
\t\tlogger.warn(`[weixin-media] memory append failed: ${String(err)}`);
\t}
\tlogger.info(`[weixin-media] archived ${media.type} requestId=${requestId} id=${id} path=${archivedPath}`);
\treturn {
\t\t...media,
\t\tfilePath: archivedPath,
\t\tarchivedFrom: media.filePath,
\t\tmediaId: id,
\t\tmediaUri: record.mediaUri,
\t\tbucket
\t};
}
/** Extract raw text from item_list (for slash command detection). */'''

text, count = helper_pattern.subn(lambda _: helper_new, text, count=1)
if count != 1:
    raise SystemExit(f"media archive helper block not found in {path}")

note_pattern = re.compile(r'const mediaMemoryNote = media \? `[\s\S]*?` : "";', re.MULTILINE)
note_new = '''const mediaKind = media?.bucket === "images" ? "图片" : media?.bucket === "videos" ? "视频" : media?.bucket === "audio" ? "语音" : media?.type;
\tconst mediaMemoryNote = media ? `\\n\\n[系统记录：用户刚发送的${mediaKind}已保存到本地媒体库：${media.filePath}${media.mediaUri ? `（${media.mediaUri}）` : ""}。如果用户以后要求找回、处理或发回这个附件，请使用这个路径；需要发回原件时回复 [send_file:${media.filePath}]。]` : "";'''

text, count = note_pattern.subn(lambda _: note_new, text, count=1)
if count != 1:
    raise SystemExit(f"media memory note block not found in {path}")

path.write_text(text)
PY

  echo "[patch] media vault applied: $target"
  patched=1
done

if [ "$patched" = 0 ]; then
  echo "[patch] no new media vault bundles patched"
fi
