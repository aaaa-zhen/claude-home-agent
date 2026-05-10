#!/bin/bash
# weixin-agent main service launcher. systemd owns restart/backoff.
set -euo pipefail

cd "$(dirname "$0")"
export PATH=/home/ubuntu/.npm-global/bin:$PATH
export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=256}"

if [ "${1:-}" != "" ]; then
    echo "$1" > model.txt
fi
[ -f model.txt ] || echo "sonnet" > model.txt

CLAUDE_MODEL=$(tr -d '[:space:]' < model.txt)
export CLAUDE_MODEL

echo "[$(date '+%Y-%m-%d %H:%M:%S')] starting weixin-acp (model=${CLAUDE_MODEL})"
date '+%Y-%m-%d %H:%M:%S' > session-start.txt

bash ./patch-send-file.sh
exec /home/ubuntu/.npm-global/bin/weixin-acp claude-code
