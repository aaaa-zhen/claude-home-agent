#!/bin/bash
# weixin-agent main service launcher. launchd owns restart/backoff on this Mac.
set -euo pipefail

cd "$(dirname "$0")"
# Load .env so MCP subprocesses (e.g. Notion) inherit secrets without hardcoding
# them in the git-tracked .mcp.json. Only simple KEY=value lines; safe under set -u.
if [ -f .env ]; then set -a; . ./.env; set +a; fi
export PATH=/Users/zhen/home-agent/weixin-agent/node_modules/.bin:/opt/homebrew/bin:$PATH
export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=256}"
export CODEX_HOME=/Users/zhen/home-agent/.codex-weixin
export CODEX_SQLITE_HOME=/Users/zhen/home-agent/.codex-weixin
export WEIXIN_AGENT_MEDIA_DIR=/Users/zhen/home-agent/weixin-agent/media
export WEIXIN_AGENT_RECENT_CONTEXT_PATH=/Users/zhen/home-agent/weixin-agent/memory/recent-context.md
export WEIXIN_AGENT_ROOT=/Users/zhen/home-agent/weixin-agent
export WEIXIN_AGENT_PYTHON=/Users/zhen/home-agent/weixin-agent/venv/bin/python
# 2026-07-02 回归原架构:关闭 v2 网关,所有消息(文字+图片)直接进 claude-agent-acp 长命会话
# (CLAUDE.md + memory/ 就是全部记忆)。要复活 v2 就把这里改回 1 重启。
export WEIXIN_AGENT_OS_GATEWAY="${WEIXIN_AGENT_OS_GATEWAY:-0}"
export WEIXIN_AGENT_OS_GATEWAY_INLINE="${WEIXIN_AGENT_OS_GATEWAY_INLINE:-0}"
# Session-Agent OS v2 上岗:微信入口路由到 agent_os.v2(回退:改回 agent_os 重启即可)。
# 注意:上面 WEIXIN_AGENT_OS_GATEWAY=0,所以这一行**当前是惰性的** —— 只有把 gateway
# 打开才会生效。别被它误导成"v2 正在跑",实际主链路是下面那条 weixin-acp → claude-agent-acp。
export WEIXIN_AGENT_OS_MODULE="${WEIXIN_AGENT_OS_MODULE:-agent_os.v2}"
if [ "$WEIXIN_AGENT_OS_MODULE" = "agent_os.v2" ]; then
    export WEIXIN_AGENT_OS_GATEWAY_TIMEOUT_SECONDS="${WEIXIN_AGENT_OS_GATEWAY_TIMEOUT_SECONDS:-180}"
    export WEIXIN_AGENT_OS_GATEWAY_TIMEOUT_MS="${WEIXIN_AGENT_OS_GATEWAY_TIMEOUT_MS:-185000}"
else
    export WEIXIN_AGENT_OS_GATEWAY_TIMEOUT_SECONDS="${WEIXIN_AGENT_OS_GATEWAY_TIMEOUT_SECONDS:-7200}"
    export WEIXIN_AGENT_OS_GATEWAY_TIMEOUT_MS="${WEIXIN_AGENT_OS_GATEWAY_TIMEOUT_MS:-7205000}"
fi
# v2 自己按快慢判定并 ack,关掉 SDK 那段硬编码"已交给 job agent"预回复。
export WEIXIN_AGENT_OS_LONG_JOB_ACK="${WEIXIN_AGENT_OS_LONG_JOB_ACK:-0}"

# ACP backend = Claude (claude-agent-acp)。网关已关,这就是唯一主链路:
# 微信消息(文字+图片)全部进这个长命 Claude Code 会话,CLAUDE.md 是它的人设与规则。
# codex-acp retired per request.
# Model + bypassPermissions come from $CLAUDE_CONFIG_DIR/settings.json.
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-/Users/zhen/home-agent/.claude-agent}"
export CLAUDE_CODE_EXECUTABLE="${CLAUDE_CODE_EXECUTABLE:-/Users/zhen/home-agent/weixin-agent/node_modules/.bin/claude}"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] starting weixin-acp with Claude (claude-agent-acp)"
date '+%Y-%m-%d %H:%M:%S' > session-start.txt

# 补丁执行结果落盘 —— 这些脚本"找不到目标就 skip 并 exit 0",设计上不炸启动,
# 代价是依赖一升级(npm install / weixin-acp 更新)功能会静默消失,和心跳那次一个病。
# healthcheck 读这个文件,出现 skipping 就告警。
PATCH_LOG=/Users/zhen/home-agent/weixin-agent/tmp/patch-status.log
: > "$PATCH_LOG"
exec 3>&1
patch_run() { echo "--- $1" >>"$PATCH_LOG"; bash "./patches/$1" 2>&1 | tee -a "$PATCH_LOG" >&3; }

patch_run patch-send-file.sh
patch_run patch-claude-agent-acp.sh
patch_run patch-weixin-acp-audio.sh
patch_run patch-weixin-acp-session-retry.sh
patch_run patch-weixin-agent-sdk.sh
patch_run patch-weixin-response-media-guard.sh
patch_run patch-weixin-media-archive.sh
patch_run patch-weixin-turn-memory.sh
patch_run patch-weixin-media-vault.sh
patch_run patch-weixin-self-restart.sh
patch_run patch-weixin-disable-fast-home-shortcuts.sh
patch_run patch-weixin-interrupt-fast-home.sh
patch_run patch-weixin-disable-agent-os-direct-control.sh
patch_run patch-weixin-agent-os-gateway.sh
patch_run patch-weixin-agent-os-media-gateway.sh
patch_run patch-weixin-agent-os-send-file.sh
patch_run patch-weixin-export-media.sh
echo "--- patch-chat-reliability.mjs" >> "$PATCH_LOG"
node ./patches/patch-chat-reliability.mjs 2>&1 | tee -a "$PATCH_LOG"
echo "--- patch-secret-redaction.mjs" >> "$PATCH_LOG"
node ./patches/patch-secret-redaction.mjs 2>&1 | tee -a "$PATCH_LOG"
exec node ./scripts/weixin-acp-chat-bridge.mjs -- claude-agent-acp
