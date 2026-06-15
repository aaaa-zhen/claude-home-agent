#!/bin/bash
# 在 HA 盒子（国内网络）上执行命令
# 用法：./ha_run.sh "python3 script.py"
#       echo "script" | ./ha_run.sh "python3 -"

set -e

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
set -a
[ -f "$SCRIPT_DIR/.env" ] && . "$SCRIPT_DIR/.env"
set +a

HA_USER=${HA_SSH_USER:-root}
HA_PASS=${HA_SSH_PASS:?HA_SSH_PASS not set in .env}

# 优先使用反向隧道（HA 盒子主动建立，监听在 localhost:2222）
# 如需重建：在 HA Terminal 执行：
#   ssh -o StrictHostKeyChecking=no -f -N -R 2222:localhost:22 -i ~/.ssh/ided25519 ubuntu@YOUR_VPS_IP
if nc -z 127.0.0.1 2222 2>/dev/null; then
    sshpass -p "$HA_PASS" ssh \
        -o StrictHostKeyChecking=no \
        -o LogLevel=ERROR \
        -p 2222 "$HA_USER@127.0.0.1" "$@"
else
    echo "[ha_run] 反向隧道不可用（localhost:2222 未监听）" >&2
    echo "[ha_run] 请在 HA Terminal 执行：ssh -o StrictHostKeyChecking=no -f -N -R 2222:localhost:22 -i ~/.ssh/ided25519 ubuntu@YOUR_VPS_IP" >&2
    exit 1
fi
