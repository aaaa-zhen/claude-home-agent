#!/bin/bash
# Conservative tmp cleanup for generated media, cookies, logs and old backups.
set -euo pipefail
TMP_DIR="/home/ubuntu/weixin-agent/tmp"
[ -d "$TMP_DIR" ] || exit 0

find "$TMP_DIR" -maxdepth 1 -type f \( -name '*.jpg' -o -name '*.jpeg' -o -name '*.png' -o -name '*.webp' -o -name '*.gif' -o -name '*.mp4' -o -name '*.mov' -o -name '*.webm' -o -name '*.avi' -o -name '*.mkv' \) -mtime +7 -delete
find "$TMP_DIR" -maxdepth 1 -type f -name '*cookies*.txt' -mtime +1 -delete
find "$TMP_DIR" -maxdepth 1 -type f -name '*.log' -mtime +30 -delete
find "$TMP_DIR" -maxdepth 1 -type d -name 'optimize-backup-*' -mtime +14 -exec rm -rf {} +
find "$TMP_DIR/memory-backups" -type f -name 'memory-*.tar.gz' -mtime +30 -delete 2>/dev/null || true
