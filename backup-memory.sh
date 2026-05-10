#!/bin/bash
# Back up personal memory safely. Default: local compressed snapshots.
set -euo pipefail
cd /home/ubuntu/weixin-agent || exit 1

BACKUP_ROOT="${MEMORY_BACKUP_DIR:-/home/ubuntu/weixin-agent/tmp/memory-backups}"
MODE="${MEMORY_BACKUP_MODE:-local}"
STAMP="$(date '+%Y%m%d-%H%M%S')"
mkdir -p "$BACKUP_ROOT"

if [ ! -d memory ]; then
    echo "memory directory not found"
    exit 1
fi

tar --exclude='*.lock' -czf "$BACKUP_ROOT/memory-$STAMP.tar.gz" memory
find "$BACKUP_ROOT" -type f -name 'memory-*.tar.gz' -mtime +30 -delete

echo "[$(date '+%Y-%m-%d %H:%M:%S')] local backup: $BACKUP_ROOT/memory-$STAMP.tar.gz"

if [ "$MODE" = "git" ]; then
    if git status --porcelain -- memory | grep -q .; then
        git add -f memory/
        git commit -m "auto-backup: memory $(date '+%Y-%m-%d %H:%M')" --no-gpg-sign
        git push origin HEAD
    fi
fi
