#!/bin/bash
# 导出 SDK 内部的 sendWeixinMediaFile,供 weixin-send-file.mjs(后台 worker 发文件)复用。
# 不导出会导致后台任务的 [send_file:] 只能发文本(发出路径而非文件)。
set -euo pipefail

targets=(
  "/Users/zhen/home-agent/weixin-agent/node_modules/weixin-agent-sdk/dist/index.mjs"
  "/opt/homebrew/lib/node_modules/weixin-acp/node_modules/weixin-agent-sdk/dist/index.mjs"
  /Users/zhen/.npm/_npx/*/node_modules/weixin-agent-sdk/dist/index.mjs
)

for target in "${targets[@]}"; do
  [ -f "$target" ] || continue
  if grep -q "sendWeixinMediaFile, start" "$target"; then
    echo "[patch] media export already applied: $target"; continue
  fi
  if grep -q "export { isLoggedIn, login, logout, start }" "$target"; then
    /usr/bin/sed -i '' 's/export { isLoggedIn, login, logout, start }/export { isLoggedIn, login, logout, sendWeixinMediaFile, start }/' "$target"
    echo "[patch] media export applied: $target"
  else
    echo "[patch] media export anchor not found (skipped): $target"
  fi
done
