#!/bin/bash
set -u
ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"
PASS=0
WARN=0
FAIL=0
ok() { echo "[OK] $*"; PASS=$((PASS+1)); }
warn() { echo "[WARN] $*"; WARN=$((WARN+1)); }
fail() { echo "[FAIL] $*"; FAIL=$((FAIL+1)); }
check_service() {
  local svc="$1"
  if systemctl is-active --quiet "$svc"; then ok "$svc active"; else fail "$svc not active"; fi
  if systemctl is-enabled --quiet "$svc"; then ok "$svc enabled"; else warn "$svc not enabled"; fi
}

echo "== services =="
check_service weixin-agent
check_service weixin-monitor
check_service weixin-session-manager

echo ""
echo "== node runtime =="
if [ -x "$ROOT/node_modules/.bin/weixin-acp" ]; then ok "local weixin-acp installed"; else fail "local weixin-acp missing; run npm install"; fi
node_version=$(node -v 2>/dev/null || true)
if [ -n "$node_version" ]; then
  ok "node $node_version"
  major=${node_version#v}; major=${major%%.*}
  if [ "$major" -lt 22 ]; then warn "weixin-acp 0.6.0 declares node >=22; current is $node_version"; fi
else
  fail "node missing"
fi

echo ""
echo "== weixin push state =="
if [ -f /home/ubuntu/.openclaw/openclaw-weixin/accounts.json ]; then ok "weixin accounts.json exists"; else fail "weixin accounts.json missing; run weixin-acp login"; fi
account_count=$(find /home/ubuntu/.openclaw/openclaw-weixin/accounts -maxdepth 1 -name '*-im-bot.json' 2>/dev/null | wc -l | tr -d ' ')
if [ "${account_count:-0}" -gt 0 ]; then ok "weixin account token file count=$account_count"; else fail "weixin account token file missing"; fi
if [ "${1:-}" = "--send" ]; then
  if /usr/bin/node "$ROOT/weixin-send.mjs" --text "healthcheck: weixin-send ok $(date '+%F %T')" >/tmp/weixin-healthcheck-send.out 2>&1; then ok "weixin-send test message sent"; else fail "weixin-send test failed: $(cat /tmp/weixin-healthcheck-send.out)"; fi
else
  ok "weixin-send not exercised; pass --send to send a test message"
fi

echo ""
echo "== cron =="
cron=$(crontab -l 2>/dev/null || true)
reading_count=$(printf '%s\n' "$cron" | grep -c 'reading-digest.mjs' || true)
if [ "$reading_count" -eq 4 ]; then ok "reading digest cron count=4"; else fail "reading digest cron count=$reading_count"; fi
if printf '%s\n' "$cron" | grep -q 'geofence_reminder.py check --quiet'; then ok "geofence cron installed"; else fail "geofence cron missing"; fi
if printf '%s\n' "$cron" | grep -q 'backup-memory.sh'; then ok "memory backup cron installed"; else warn "memory backup cron missing"; fi

echo ""
echo "== local APIs =="

echo ""
echo "== home assistant =="
if python3 - <<'PYHA' >/tmp/ha-healthcheck.out 2>&1
import os
from pathlib import Path
import requests
for raw in Path('.env').read_text().splitlines():
    s = raw.strip()
    if not s or s.startswith('#') or '=' not in s:
        continue
    k, v = s.split('=', 1)
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
url = os.environ['HA_URL'].rstrip('/')
token = os.environ['HA_TOKEN']
r = requests.get(f'{url}/states/climate.gree', headers={'Authorization': 'Bearer ' + token}, timeout=10)
r.raise_for_status()
data = r.json()
print(data.get('entity_id'), data.get('state'))
PYHA
then
  ok "HA API reachable: $(cat /tmp/ha-healthcheck.out)"
else
  fail "HA API failed: $(cat /tmp/ha-healthcheck.out)"
fi

echo ""
echo "== MCP =="
if timeout 20 bash -c 'printf "%s\n" "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2024-11-05\",\"capabilities\":{},\"clientInfo\":{\"name\":\"healthcheck\",\"version\":\"1\"}}}" "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\",\"params\":{}}" | /home/ubuntu/weixin-agent/venv/bin/python /home/ubuntu/weixin-agent/didi_mcp_proxy.py' >/tmp/didi-healthcheck.out 2>&1; then
  if grep -q 'taxi_estimate' /tmp/didi-healthcheck.out; then ok "Didi MCP tools/list ok"; else fail "Didi MCP missing taxi_estimate"; fi
else
  fail "Didi MCP check failed: $(tail -5 /tmp/didi-healthcheck.out)"
fi

echo ""
echo "== resources =="
mem_pct=$(free | awk '/Mem:/ {printf "%d", $3*100/$2}')
disk_pct=$(df / | awk 'NR==2 {gsub("%", "", $5); print $5}')
if [ "$mem_pct" -lt 85 ]; then ok "memory usage ${mem_pct}%"; else warn "memory usage ${mem_pct}%"; fi
if [ "$disk_pct" -lt 85 ]; then ok "disk usage ${disk_pct}%"; else warn "disk usage ${disk_pct}%"; fi

echo ""
echo "summary: ok=$PASS warn=$WARN fail=$FAIL"
if [ "$FAIL" -gt 0 ]; then exit 1; fi
