#!/bin/bash
# Back up personal memory safely. Default: local compressed snapshots.
set -euo pipefail
cd /Users/zhen/home-agent/weixin-agent || exit 1

BACKUP_ROOT="${MEMORY_BACKUP_DIR:-/Users/zhen/home-agent/weixin-agent/tmp/memory-backups}"
MODE="${MEMORY_BACKUP_MODE:-local}"
STAMP="$(date '+%Y%m%d-%H%M%S')"
mkdir -p "$BACKUP_ROOT"

if [ ! -d memory ]; then
    echo "memory directory not found"
    exit 1
fi

/opt/homebrew/bin/node scripts/backup-conversation-context.mjs "$BACKUP_ROOT/context-$STAMP.sqlite3"
find "$BACKUP_ROOT" -type f -name 'context-*.sqlite3' -mtime +30 -delete

tar --exclude='*.lock' -czf "$BACKUP_ROOT/memory-$STAMP.tar.gz" memory
find "$BACKUP_ROOT" -type f -name 'memory-*.tar.gz' -mtime +30 -delete

echo "[$(date '+%Y-%m-%d %H:%M:%S')] local backup: $BACKUP_ROOT/memory-$STAMP.tar.gz"

# 每日顺带把 CLAUDE.md/memory 提交进私有版本库(.git-private,永不外推)
bash scripts/private-commit.sh "daily snapshot $(date '+%Y-%m-%d')" || true

if [ "$MODE" = "git" ]; then
    if git status --porcelain -- memory | grep -q .; then
        git add -f memory/
        git commit -m "auto-backup: memory $(date '+%Y-%m-%d %H:%M')" --no-gpg-sign
        git push origin HEAD
    fi
fi
