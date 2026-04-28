#!/bin/bash
cd "$(dirname "$0")"
source venv/bin/activate
echo "Monitor started at $(date)"
while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting monitor.py..."
    python monitor.py || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] monitor.py exited, restarting in 10s..."
    sleep 10
done
