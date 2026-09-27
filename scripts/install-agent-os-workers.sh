#!/bin/bash
# Install Agent OS workers as per-user launchd services.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LOG_DIR="/Users/zhen/home-agent/_migration/logs"
UID_NUM=$(id -u)

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

install_worker() {
  local worker="$1"
  local slot="${2:-1}"
  local suffix=""
  if [ "$slot" != "1" ]; then
    suffix="-${slot}"
  fi
  local label="com.zhen.agent-os-${worker}-worker${suffix}"
  local plist="$HOME/Library/LaunchAgents/${label}.plist"

  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key><array>
    <string>/Users/zhen/home-agent/_migration/run-with-env.sh</string>
    <string>${ROOT}</string>
    <string>${ROOT}/venv/bin/python</string>
    <string>-m</string>
    <string>agent_os</string>
    <string>work</string>
    <string>--worker</string>
    <string>${worker}</string>
    <string>--worker-id</string>
    <string>launchd-${worker}-${slot}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>EnvironmentVariables</key><dict>
    <key>TZ</key><string>Asia/Shanghai</string>
    <key>WEIXIN_AGENT_ROOT</key><string>${ROOT}</string>
    <key>PATH</key><string>${ROOT}/node_modules/.bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key><string>${LOG_DIR}/${label}.out.log</string>
  <key>StandardErrorPath</key><string>${LOG_DIR}/${label}.err.log</string>
</dict></plist>
PLIST

  launchctl bootout "gui/${UID_NUM}/${label}" >/dev/null 2>&1 || true
  sleep 0.5
  launchctl bootstrap "gui/${UID_NUM}" "$plist"
  launchctl kickstart -k "gui/${UID_NUM}/${label}"
  launchctl print "gui/${UID_NUM}/${label}" >/dev/null
  echo "installed ${label}"
}

if [ "$#" -gt 0 ]; then
  for spec in "$@"; do
    if [[ "$spec" == *:* ]]; then
      worker="${spec%%:*}"
      count="${spec##*:}"
    else
      worker="$spec"
      count=1
    fi
    for slot in $(seq 1 "$count"); do
      install_worker "$worker" "$slot"
    done
  done
else
  install_worker control 1
  for slot in $(seq 1 10); do
    install_worker agent "$slot"
  done
  install_worker job 1
fi
