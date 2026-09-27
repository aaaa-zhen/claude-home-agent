#!/bin/bash
# private-commit.sh — 把隐私文件(CLAUDE.md / AGENTS.md / memory/)提交到独立的私有 git 仓库。
# 私有仓库的 git-dir 在 .git-private/,与公开仓库(.git,远端是公开 GitHub)完全隔离,
# 永远不要给 .git-private 配公开远端。
# 用法: bash scripts/private-commit.sh ["提交说明"]
set -euo pipefail

ROOT="/Users/zhen/home-agent/weixin-agent"
G=(git --git-dir="$ROOT/.git-private" --work-tree="$ROOT")
cd "$ROOT"

if [ ! -d "$ROOT/.git-private" ]; then
  git --git-dir="$ROOT/.git-private" --work-tree="$ROOT" init -q -b main
fi

# 主仓库 .gitignore 对这些文件生效且优先级高,必须 -f 显式白名单加入。
# 只跟踪有语义的记忆文件,不跟踪日志/锁/状态 json。
"${G[@]}" add -f CLAUDE.md HEARTBEAT.md memory/*.md 2>/dev/null; [ -d memory/daily ] && "${G[@]}" add -f memory/daily 2>/dev/null || true
[ -d memory/skills ] && "${G[@]}" add -f memory/skills 2>/dev/null || true
[ -f memory/geofences.json ] && "${G[@]}" add -f memory/geofences.json 2>/dev/null || true
[ -f AGENTS.md ] && "${G[@]}" add -f AGENTS.md 2>/dev/null || true

if "${G[@]}" diff --cached --quiet 2>/dev/null; then
  echo "private repo: nothing to commit"
  exit 0
fi

MSG="${1:-snapshot $(date '+%Y-%m-%d %H:%M')}"
"${G[@]}" commit -q -m "$MSG"
"${G[@]}" log --oneline -1
