#!/bin/bash
# Query 12306 train tickets via HA box proxy
# Usage: ./train_query.sh <from> <to> <date>
# Example: ./train_query.sh 珠海 郑州 2026-04-30

FROM=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$1'))")
TO=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$2'))")
DATE="$3"

ssh -o ProxyCommand="cloudflared access ssh --hostname your-ssh-host.example.com" \
  -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
  root@your-ssh-host.example.com \
  "curl -s 'http://localhost:5001/query?from=${FROM}&to=${TO}&date=${DATE}'"
