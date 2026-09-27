#!/bin/bash
# 实时公交查询，通过 HA 盒子代理（国内网络）
# Usage: ./bus_query.sh <站名> [城市] [方向：2=反向]
# Example:
#   ./bus_query.sh "白沙"
#   ./bus_query.sh "白沙" 珠海
#   ./bus_query.sh "白沙" 珠海 2

# 向上找仓库根（含 start.sh / package.json），本脚本可从 tools/travel/ 或根目录 shim 调用
ROOT=$(cd "$(dirname "$0")" && pwd)
while [ "$ROOT" != "/" ] && [ ! -f "$ROOT/start.sh" ] && [ ! -f "$ROOT/package.json" ]; do
  ROOT=$(dirname "$ROOT")
done
set -a
[ -f "$ROOT/.env" ] && . "$ROOT/.env"
set +a

HA_USER=${HA_SSH_USER:-root}
HA_PASS=${HA_SSH_PASS:?HA_SSH_PASS not set in .env}
HOSTNAME=${HA_SSH_HOST:-your-ssh-host.example.com}

STOP="$1"
CITY="${2:-珠海}"
DIR="${3:-}"

STOP_ENC=$(python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))" "$STOP")
CITY_ENC=$(python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))" "$CITY")

QUERY="city=${CITY_ENC}&stop=${STOP_ENC}"
if [ -n "$DIR" ]; then
  QUERY="${QUERY}&dir=${DIR}"
fi

cloudflared access tcp --hostname "$HOSTNAME" --url localhost:15003 &>/dev/null &
CF_PID=$!
sleep 3

ssh \
  -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
  -p 15003 "$HA_USER@localhost" \
  "curl -s 'http://localhost:5003/bus?${QUERY}'"

kill $CF_PID 2>/dev/null
