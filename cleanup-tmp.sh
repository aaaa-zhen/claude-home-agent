#!/bin/bash
# Conservative tmp cleanup for generated media, cookies, logs and old backups.
set -euo pipefail
ROOT="/Users/zhen/home-agent/weixin-agent"
TMP_DIR="$ROOT/tmp"
[ -d "$TMP_DIR" ] || exit 0

find "$TMP_DIR" -maxdepth 1 -type f \( -name '*.jpg' -o -name '*.jpeg' -o -name '*.png' -o -name '*.webp' -o -name '*.gif' -o -name '*.mp4' -o -name '*.mov' -o -name '*.webm' -o -name '*.avi' -o -name '*.mkv' \) -mtime +7 -delete
find "$TMP_DIR" -maxdepth 1 -type f -name '*cookies*.txt' -mtime +1 -delete
find "$TMP_DIR" -maxdepth 1 -type f \( -name '*chrome-history-copy*' -o -name '*apple-calendar-copy*' -o -name '*.sqlite' -o -name '*.sqlite-*' -o -name '*.db' -o -name '*.db-*' \) -mtime +1 -delete
find "$TMP_DIR" -maxdepth 1 -type f -name 'slim-archive-*.tar.gz' -mtime +7 -delete
find "$TMP_DIR" -maxdepth 1 -type f -name '*.log' -mtime +30 -delete
# 按体积兜底:*.log +30天 的规则治不了"还在写、但已经涨到几十 MB"的日志
# (tmp/fastchat-cf.log 就这么长到 29MB)。超过 20MB 的截断成最后 2000 行。
while IFS= read -r big; do
  tail -n 2000 "$big" > "$big.trim" 2>/dev/null && mv -f "$big.trim" "$big"
  echo "[$(date '+%F %T')] truncated oversized log: $big"
done < <(find "$TMP_DIR" -maxdepth 1 -type f -name '*.log' -size +20M)
# 下载器留下的临时目录(tmp/x-dl-*、tmp/xdl)是 maxdepth 1 扫不到的
find "$TMP_DIR" -maxdepth 1 -type d \( -name 'x-dl-*' -o -name 'xdl' \) -mtime +3 -exec rm -rf {} + 2>/dev/null || true
# 摄像头相关:snap/look 抓的高清帧 + 延时摄影成片,发完微信就没用了,留 7 天
find "$TMP_DIR/camera-snaps" -type f -name '*.jpg' -mtime +7 -delete 2>/dev/null || true
find "$TMP_DIR" -maxdepth 1 -type f -name 'room-timelapse-*.mp4' -mtime +7 -delete
find "$TMP_DIR" -maxdepth 1 -type d -name 'optimize-backup-*' -mtime +14 -exec rm -rf {} +
find "$TMP_DIR/memory-backups" -type f -name 'memory-*.tar.gz' -mtime +30 -delete 2>/dev/null || true

# PR-05: maxdepth 1 以前漏掉的解包目录 / asar 文件 / 安装镜像
find "$TMP_DIR" -maxdepth 1 \( -name 'tb-asar' -o -name 'local-sniff' -o -name 'app.asar*' \) -mtime +3 -exec rm -rf {} + 2>/dev/null || true
find "$TMP_DIR" -maxdepth 1 -type f -name '*.dmg' -mtime +3 -delete

# PR-05: tmp/locks 下 mtime>1d 且无人打开、无人持有 fcntl LOCK_EX 的文件才删
LOCKS_DIR="$TMP_DIR/locks"
if [ -d "$LOCKS_DIR" ]; then
  "$ROOT/venv/bin/python" - "$LOCKS_DIR" <<'PY'
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
now = time.time()
cutoff = 24 * 3600
for p in sorted(root.iterdir()):
    if not p.is_file():
        continue
    try:
        if now - p.stat().st_mtime <= cutoff:
            continue
    except FileNotFoundError:
        continue
    r = subprocess.run(
        ["lsof", str(p)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if r.returncode == 0:
        print(f"skip open lock: {p}")
        continue
    fd = -1
    try:
        fd = os.open(p, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"skip held lock: {p}")
            continue
        os.unlink(p)
        print(f"removed stale lock: {p}")
    except OSError as exc:
        print(f"skip unreadable lock: {p}: {exc}")
    finally:
        if fd >= 0:
            os.close(fd)
sys.exit(0)
PY
fi

# PR-05 兜底: inject.log >1MB 留最后 2000 行（PR-02 logLine 已截断，这里防漏）
INJECT_LOG="$ROOT/runtime/prompt-inject/inject.log"
if [ -f "$INJECT_LOG" ]; then
  inj_size=$(stat -f%z "$INJECT_LOG")
  if [ "$inj_size" -gt 1048576 ]; then
    tail -n 2000 "$INJECT_LOG" > "$INJECT_LOG.trim" && mv -f "$INJECT_LOG.trim" "$INJECT_LOG"
    echo "[$(date '+%F %T')] truncated oversized inject.log ($inj_size bytes)"
  fi
fi
