#!/bin/bash
# 快递价格查询，通过 HA 盒子代理（国内网络）
# Usage: ./kuaidi100_query.sh <寄件地址> <收件地址> [重量kg] [快递公司代码]
# Example: ./kuaidi100_query.sh "广东珠海" "北京" 1 shunfeng
#          ./kuaidi100_query.sh "广东珠海" "北京" 1   # 查所有快递公司

FROM=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$1'))")
TO=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$2'))")
WEIGHT="${3:-1}"
MAN="${4:-}"

QUERY="from=${FROM}&to=${TO}&weight=${WEIGHT}"
if [ -n "$MAN" ]; then
  QUERY="${QUERY}&man=${MAN}"
fi

cloudflared access tcp --hostname ssh.mafuzhenhome.xyz --url localhost:15002 &>/dev/null &
CF_PID=$!
sleep 3

sshpass -p '123456' ssh \
  -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
  -p 15002 root@localhost \
  "curl -s 'http://localhost:5002/price?${QUERY}'"

kill $CF_PID 2>/dev/null
