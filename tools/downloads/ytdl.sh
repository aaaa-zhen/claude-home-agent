#!/bin/bash
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
YTDL=/opt/homebrew/bin/yt-dlp
NODE=/opt/homebrew/bin/node
PYTHON="$ROOT/venv/bin/python"

cd "$ROOT"
source "$ROOT/tools/net/proxy.env"
mkdir -p "$ROOT/tmp/locks"

if [ "$#" -ne 1 ]; then
  echo "usage: $0 YOUTUBE_URL" >&2
  exit 64
fi

URL="$1"
LOCK_FILE="$ROOT/tmp/locks/ytdl.lock"
LOG_FILE="$ROOT/tmp/ytdl.log"
RESULT_FILE="$ROOT/tmp/ytdl-result-$$.txt"
OUTPUT_TEMPLATE="$ROOT/tmp/ytdl-%(id)s.%(ext)s"
trap 'rm -f "$RESULT_FILE"' EXIT
: > "$RESULT_FILE"

"$PYTHON" "$ROOT/tools/downloads/with-lock.py" "$LOCK_FILE" \
  "$YTDL" \
  --no-playlist \
  --cookies-from-browser safari \
  --proxy "$PROXY_URL" \
  -f "bv*[height<=720]+ba/b[height<=720]/b" \
  --merge-output-format mp4 \
  --print-to-file "after_move:filepath" "$RESULT_FILE" \
  -o "$OUTPUT_TEMPLATE" \
  "$URL" >> "$LOG_FILE" 2>&1
download_rc=$?

case "$download_rc" in
  2)
    exit 2
    ;;
  0)
    IFS= read -r output_file < "$RESULT_FILE"
    "$NODE" "$ROOT/weixin-send-file.mjs" \
      --file "$output_file" \
      --text "YouTube 下载完成" >> "$LOG_FILE" 2>&1
    exit $?
    ;;
  *)
    "$NODE" "$ROOT/weixin-send.mjs" \
      --text "YouTube 下载失败（退出码 $download_rc）" >> "$LOG_FILE" 2>&1
    exit "$download_rc"
    ;;
esac
