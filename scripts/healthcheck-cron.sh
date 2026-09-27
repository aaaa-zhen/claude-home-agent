#!/bin/bash
# healthcheck-cron.sh — 定时体检 + 核心故障状态变化报警。
# 辅助功能和单次网络抖动只记日志；只有影响主链路的故障才发微信。
set -uo pipefail

ROOT="/Users/zhen/home-agent/weixin-agent"
STATE="$ROOT/tmp/healthcheck-last-fails.txt"
ALERT_STATE="$ROOT/tmp/healthcheck-last-alert-fails.txt"
CLAUDE_STREAK_STATE="$ROOT/tmp/healthcheck-claude-api-fail-streak"
WARN_STATE="$ROOT/tmp/healthcheck-last-warns.txt"
LOG="$ROOT/tmp/healthcheck-cron.log"
NODE=/opt/homebrew/bin/node
cd "$ROOT"
mkdir -p tmp

OUT="$(bash ./healthcheck.sh 2>/dev/null)"
FAILS="$(printf '%s\n' "$OUT" | grep '^\[FAIL\]' | sort || true)"
printf '%s' "$FAILS" > "$STATE"

ts="$(date '+%Y-%m-%d %H:%M:%S')"
summary="$(printf '%s\n' "$OUT" | tail -1)"
echo "[$ts] $summary" >> "$LOG"

# WARN 和所有原始 FAIL 仍落盘供排查,但不再直接推送。
WARNS="$(printf '%s\n' "$OUT" | grep '^\[WARN\]' | sort || true)"
printf '%s' "$WARNS" > "$WARN_STATE"

# 立即推送的范围只保留主链路：微信主进程、防休眠、心跳、核心运行时、
# 微信登录态和消息队列。其余工具/桥接/摄像头/提示注入故障仅记日志。
CORE_FAILS="$(printf '%s\n' "$FAILS" | grep -E '^\[FAIL\] (com\.zhen\.weixin-agent|com\.zhen\.caffeinate|heartbeat |chat |local weixin-acp missing|local claude missing|node missing|weixin accounts\.json missing|weixin account token file missing|weixin-send test failed|weixin message queue patch missing)' || true)"

# Claude API 线路偶尔会抖一下。连续 3 轮都不可达才升级为核心故障；
# 任何一次恢复都会立即清零,因此半小时内自愈的抖动不会打扰用户。
CLAUDE_FAIL="$(printf '%s\n' "$FAILS" | grep '^\[FAIL\] claude API NOT reachable via clash proxy' || true)"
claude_streak="$(cat "$CLAUDE_STREAK_STATE" 2>/dev/null || echo 0)"
case "$claude_streak" in
  ''|*[!0-9]*) claude_streak=0 ;;
esac
if [ -n "$CLAUDE_FAIL" ]; then
  claude_streak=$((claude_streak + 1))
else
  claude_streak=0
fi
printf '%s' "$claude_streak" > "$CLAUDE_STREAK_STATE"

ALERT_FAILS="$({
  printf '%s\n' "$CORE_FAILS"
  if [ "$claude_streak" -ge 3 ]; then printf '%s\n' "$CLAUDE_FAIL"; fi
} | sed '/^$/d' | sort)"
PREV_ALERTS="$(cat "$ALERT_STATE" 2>/dev/null || true)"

if [ "$ALERT_FAILS" = "$PREV_ALERTS" ]; then
  exit 0
fi

if [ -n "$ALERT_FAILS" ]; then
  MSG="🔴 Home Agent 核心故障：
$ALERT_FAILS"
else
  MSG="✅ Home Agent 核心故障已恢复。"
fi
if "$NODE" "$ROOT/weixin-send.mjs" --text "$MSG" >> "$LOG" 2>&1; then
  printf '%s' "$ALERT_FAILS" > "$ALERT_STATE"
else
  echo "[$ts] weixin-send failed or outcome unknown; alert acknowledgement unchanged" >> "$LOG"
fi
