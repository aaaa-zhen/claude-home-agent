#!/bin/bash
# heartbeat.sh — 心跳:每 30 分钟静默问一次大脑"有需要提醒用户的吗"。
# OpenClaw HEARTBEAT_OK 契约:回复 HEARTBEAT_OK → 吞掉不打扰;否则把回复原样推微信。
# 日志: tmp/heartbeat.log
set -uo pipefail

ROOT="/Users/zhen/home-agent/weixin-agent"
LOG="$ROOT/tmp/heartbeat.log"
CLAUDE_BIN="$ROOT/node_modules/.bin/claude"
NODE=/opt/homebrew/bin/node
mkdir -p "$ROOT/tmp"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

export CLAUDE_CONFIG_DIR="/Users/zhen/home-agent/.claude-agent"
export PATH="/opt/homebrew/bin:$PATH"
export TZ="Asia/Shanghai"
export HTTP_PROXY="http://127.0.0.1:7897"  http_proxy="http://127.0.0.1:7897"
export HTTPS_PROXY="http://127.0.0.1:7897" https_proxy="http://127.0.0.1:7897"
export NO_PROXY="192.168.1.100,localhost,127.0.0.1,.weixin.qq.com,ilinkai.weixin.qq.com,.example.com,.amap.com,.gtimg.cn,.qq.com"
export no_proxy="$NO_PROXY"

[ -x "$CLAUDE_BIN" ] || { echo "[$(ts)] skip: no claude" >> "$LOG"; exit 0; }

# Retry only explicit rejections. Ambiguous delivery is retained for inspection.
"$NODE" "$ROOT/scripts/heartbeat-notify.mjs" --retry >> "$LOG" 2>&1 || true

PROMPT="你是 Zhen 的微信家庭助手的心跳检查进程(用户看不到这次运行,除非你决定开口)。
现在时间:$(date '+%Y-%m-%d %H:%M %A')。工作目录 ${ROOT}。

严格按 ${ROOT}/HEARTBEAT.md 清单逐项检查(先读它)。

输出规则(二选一):
A. 没有值得打扰用户的事 → 只输出 HEARTBEAT_OK
B. 有 → 只输出要发给用户的那条微信消息本身(简短口语,像朋友随口提一句,不解释你是心跳)"

OUT="$(cd "$ROOT" && "$CLAUDE_BIN" -p "$PROMPT" \
  --model sonnet \
  --permission-mode bypassPermissions \
  --setting-sources user \
  2>>"$LOG")"
RC=$?

FIRST_LINE="$(printf '%s' "$OUT" | head -1 | tr -d '[:space:]')"
if [ $RC -ne 0 ] || [ -z "$OUT" ]; then
  # 失败时把 stdout 也留下 —— claude CLI 的报错(如 "Failed to authenticate")走的是 stdout,
  # 只重定向 stderr 会让失败完全静默(2026-07-03 起连续失败 23 天无人察觉就是这么来的)。
  echo "[$(ts)] heartbeat error rc=$RC stdout=$(printf '%s' "$OUT" | head -c 300 | tr '\n' ' ')" >> "$LOG"
elif [ "$FIRST_LINE" = "HEARTBEAT_OK" ] && [ ${#OUT} -lt 300 ]; then
  echo "[$(ts)] OK" >> "$LOG"
else
  printf '%s' "$OUT" | "$NODE" "$ROOT/scripts/heartbeat-notify.mjs" >> "$LOG" 2>&1
fi
exit 0
