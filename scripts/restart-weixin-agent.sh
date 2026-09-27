#!/bin/bash
# Restart the WeChat agent from a detached helper process.
# The caller should send its user-facing reply before invoking this script.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
DELAY_SECONDS=2
REASON="manual"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --delay)
      DELAY_SECONDS="${2:-2}"
      shift 2
      ;;
    --reason)
      REASON="${2:-manual}"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

LOG="$ROOT/tmp/restart-weixin-agent.log"
RESTART_LOG="$ROOT/memory/session-restarts.log"
mkdir -p "$ROOT/tmp" "$ROOT/memory"

ts() {
  date '+%Y-%m-%d %H:%M:%S'
}

echo "[$(ts)] scheduled delay=${DELAY_SECONDS}s reason=${REASON}" >> "$LOG"
NODE_BIN=$(command -v node || true)
if [ -z "$NODE_BIN" ] && [ -x /opt/homebrew/bin/node ]; then
  NODE_BIN=/opt/homebrew/bin/node
fi
if [ -n "$NODE_BIN" ]; then
  "$NODE_BIN" "$ROOT/scripts/write-session-handoff.mjs" --reason "$REASON" --source self-restart >> "$LOG" 2>&1 || true
fi
sleep "$DELAY_SECONDS"

UID_NUM=$(id -u)
echo "[$(ts)] self-restart requested: ${REASON}" >> "$RESTART_LOG"

launchctl kickstart -k "gui/${UID_NUM}/com.zhen.weixin-session-manager" >> "$LOG" 2>&1 || true
launchctl kickstart -k "gui/${UID_NUM}/com.zhen.weixin-agent" >> "$LOG" 2>&1
echo "[$(ts)] restart command sent" >> "$LOG"
