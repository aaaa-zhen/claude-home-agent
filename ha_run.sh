#!/bin/bash
# ha_run.sh — 在 HA 盒子(国内网络)上执行命令。
# 用法: bash ha_run.sh "python3 script.py"
# 2026-07-03 重写:旧版依赖已退役腾讯云 VPS 的反向隧道(localhost:2222),是死代码;
# 现在与 train_query.sh 同路径:cloudflared access SSH + ~/.ssh/id_ed25519 密钥认证。
set -euo pipefail

HA_USER=${HA_SSH_USER:-root}
HOSTNAME=${HA_SSH_HOST:-your-ssh-host.example.com}

exec ssh \
    -o ProxyCommand="cloudflared access ssh --hostname ${HOSTNAME}" \
    -o StrictHostKeyChecking=no \
    -o LogLevel=ERROR \
    -o ConnectTimeout=20 \
    "${HA_USER}@${HOSTNAME}" "$@"
