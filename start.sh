#!/bin/bash
# weixin-agent 主服务守护脚本 (Linux)
set -e

cd "$(dirname "$0")"

# 初始模型：从参数或 model.txt 或默认 sonnet
if [ -n "$1" ]; then
    echo "$1" > model.txt
fi
[ -f model.txt ] || echo "sonnet" > model.txt

echo "========================================"
echo "  weixin-acp 守护脚本 (Linux)"
echo "  Ctrl+C 停止服务"
echo "========================================"

COUNT=0
FAST_CRASH_COUNT=0

while true; do
    COUNT=$((COUNT + 1))
    CLAUDE_MODEL=$(cat model.txt | tr -d '[:space:]')
    export CLAUDE_MODEL

    echo ""
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${COUNT} 次启动 (模型: ${CLAUDE_MODEL})..."
    date '+%Y-%m-%d %H:%M:%S' > session-start.txt

    # Apply send_file patch if needed
    bash ./patch-send-file.sh

    START_TIME=$(date +%s)
    npx weixin-acp claude-code || true
    END_TIME=$(date +%s)
    DURATION=$((END_TIME - START_TIME))

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 进程退出 (运行了 ${DURATION}s)"

    # Crash backoff: if exited within 30s, count as fast crash
    if [ "$DURATION" -lt 30 ]; then
        FAST_CRASH_COUNT=$((FAST_CRASH_COUNT + 1))
        if [ "$FAST_CRASH_COUNT" -ge 3 ]; then
            WAIT=$((FAST_CRASH_COUNT * 10))
            [ "$WAIT" -gt 120 ] && WAIT=120
            echo "[WARNING] 连续快速退出 ${FAST_CRASH_COUNT} 次，等待 ${WAIT}s..."
            sleep "$WAIT"
        else
            sleep 5
        fi
    else
        FAST_CRASH_COUNT=0
        sleep 5
    fi
done
