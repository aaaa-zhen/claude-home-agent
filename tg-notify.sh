#!/bin/bash
# Telegram channel notification wrapper
# Usage: tg-notify.sh <channel> "message"
# Channels: english, podcast, home, reminder, env
#
# Runs in background, never blocks the caller.
#
# Daily group topic thread IDs:
#   english  → 8
#   home     → 9
#   reminder → 10
#   env      → 11
#   podcast  → 12

CHANNEL="$1"
MSG="$2"

if [ -z "$CHANNEL" ] || [ -z "$MSG" ]; then
  echo "Usage: tg-notify.sh <channel> <message>" >&2
  exit 1
fi

# Forward to Daily group, routed to the matching topic
case "$CHANNEL" in
  english)  THREAD=8  ;;
  home)     THREAD=9  ;;
  reminder) THREAD=10 ;;
  env)      THREAD=11 ;;
  podcast)  THREAD=12 ;;
  *)        THREAD="" ;;
esac

if [ -n "$THREAD" ]; then
  nohup /usr/bin/node /home/ubuntu/weixin-agent/telegram-send.mjs \
    --channel daily --thread-id "$THREAD" --text "$MSG" \
    >> /home/ubuntu/weixin-agent/logs/tg-notify.log 2>&1 &
else
  nohup /usr/bin/node /home/ubuntu/weixin-agent/telegram-send.mjs \
    --channel daily --text "$MSG" \
    >> /home/ubuntu/weixin-agent/logs/tg-notify.log 2>&1 &
fi
