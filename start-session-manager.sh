#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
echo "Session manager started at $(date)"
while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting session-manager.py..."
    python session-manager.py || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] session-manager.py exited, restarting in 10s..."
    sleep 10
done
