#!/bin/bash
# 兼容 shim：真实脚本已迁至 tools/travel/bus_query.sh
# 保留此入口是因为 CLAUDE.md / memory/skills / scripts/train-watch.mjs 仍按旧路径调用。
exec "$(cd "$(dirname "$0")" && pwd)/tools/travel/bus_query.sh" "$@"
