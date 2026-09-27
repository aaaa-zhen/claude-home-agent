#!/bin/bash
# room-timelapse.sh — 把某天的房间事件帧拼成延时小视频("你家的一天")。
# 用法: bash scripts/room-timelapse.sh [YYYY-MM-DD]   # 默认今天
# 输出: tmp/room-timelapse-<date>.mp4 (路径打印到 stdout,可直接 [send_file:] 发微信)
set -euo pipefail

ROOT="/Users/zhen/home-agent/weixin-agent"
DATE="${1:-$(date '+%Y-%m-%d')}"
DAY_DIR="$ROOT/media/room-log/$DATE"
OUT="$ROOT/tmp/room-timelapse-$DATE.mp4"

[ -d "$DAY_DIR" ] || { echo "没有 $DATE 的事件帧" >&2; exit 1; }
COUNT=$(ls "$DAY_DIR"/*.jpg 2>/dev/null | wc -l | tr -d ' ')
[ "$COUNT" -ge 3 ] || { echo "$DATE 只有 $COUNT 帧,不够拼视频" >&2; exit 1; }

# 每帧上叠一行时间戳(取自文件名 HH-MM-SS),4fps
ffmpeg -y -loglevel error -framerate 4 -pattern_type glob -i "$DAY_DIR/*.jpg" \
  -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" \
  -c:v libx264 -pix_fmt yuv420p -preset veryfast "$OUT"
echo "$OUT"
