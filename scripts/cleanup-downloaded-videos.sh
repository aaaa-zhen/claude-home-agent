#!/bin/bash
# Weekly cleanup for disposable downloaded/outbound videos.
# Keeps inbound WeChat media and generated source assets untouched.
set -euo pipefail

ROOT="/Users/zhen/home-agent/weixin-agent"
# 3 天:视频发给用户之后就没用了,7 天会让 tmp/ 常年压着 60-70MB 的死文件。
RETENTION_DAYS="${VIDEO_RETENTION_DAYS:-3}"
DRY_RUN=0

usage() {
  echo "Usage: $0 [--dry-run] [--days N]"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --days)
      [ "$#" -ge 2 ] || { usage >&2; exit 2; }
      RETENTION_DAYS="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

case "$RETENTION_DAYS" in
  ''|*[!0-9]*)
    echo "retention days must be a non-negative integer" >&2
    exit 2
    ;;
esac

# tmp/ is disposable. media/outbox/ only holds delivery copies; durable inbound
# media and generated source assets live elsewhere and are intentionally excluded.
TARGETS=(
  "$ROOT/tmp"
  "$ROOT/media/outbox"
)

count=0
bytes=0

for target in "${TARGETS[@]}"; do
  [ -d "$target" ] || continue
  while IFS= read -r -d '' file; do
    size=$(stat -f '%z' "$file" 2>/dev/null || echo 0)
    count=$((count + 1))
    bytes=$((bytes + size))
    if [ "$DRY_RUN" -eq 1 ]; then
      printf '[dry-run] would remove %s\n' "$file"
    else
      rm -f -- "$file"
      printf 'removed %s\n' "$file"
    fi
  done < <(
    find "$target" -type f \
      \( -iname '*.mp4' -o -iname '*.mov' -o -iname '*.mkv' -o -iname '*.webm' -o -iname '*.m4v' -o -iname '*.avi' \) \
      -mtime "+$RETENTION_DAYS" -print0
  )
done

megabytes=$(awk -v bytes="$bytes" 'BEGIN { printf "%.1f", bytes / 1024 / 1024 }')
action="removed"
[ "$DRY_RUN" -eq 1 ] && action="would remove"
printf '[%s] video cleanup: %s %d file(s), %s MB; retention=%s days\n' \
  "$(date '+%Y-%m-%d %H:%M:%S')" "$action" "$count" "$megabytes" "$RETENTION_DAYS"
