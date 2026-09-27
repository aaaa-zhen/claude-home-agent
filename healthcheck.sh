#!/bin/bash
set -u

# 非交互 shell(SSH/cron)不加载 .zshrc,PATH 里没有 Homebrew 路径,
# 会导致裸 node/其他 brew 工具探测失败而误报。这里补上。
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"

ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"

PASS=0
WARN=0
FAIL=0
UID_NUM=$(id -u)

ok() { echo "[OK] $*"; PASS=$((PASS+1)); }
warn() { echo "[WARN] $*"; WARN=$((WARN+1)); }
fail() { echo "[FAIL] $*"; FAIL=$((FAIL+1)); }

# 给可能调用模型或外部服务的检查加进程组级硬超时。
# 子命令自己的 timeout 若失效,也不能让整轮 healthcheck 永久挂住。
run_with_timeout() {
  local seconds="$1"
  shift
  /opt/homebrew/bin/python3 - "$seconds" "$@" <<'PYTIMEOUT'
import os
import signal
import subprocess
import sys

timeout = float(sys.argv[1])
command = sys.argv[2:]
process = subprocess.Popen(command, start_new_session=True)
try:
    returncode = process.wait(timeout=timeout)
except subprocess.TimeoutExpired:
    print(f"hard timeout after {timeout:g}s: {' '.join(command)}", file=sys.stderr)
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    returncode = 124
raise SystemExit(returncode)
PYTIMEOUT
}

check_launch_agent() {
  local label="$1"
  local domain="gui/${UID_NUM}/${label}"
  local out
  if ! out=$(launchctl print "$domain" 2>/dev/null); then
    fail "$label not loaded"
    return
  fi
  if printf '%s\n' "$out" | grep -q 'state = running'; then
    local pid
    pid=$(printf '%s\n' "$out" | awk '/pid =/ {print $3; exit}')
    ok "$label running pid=${pid:-unknown}"
  else
    fail "$label loaded but not running"
  fi
}

section() {
  echo ""
  echo "== $1 =="
}

section "services"
# 2026-07-03: 清单对齐现役服务(v1 agent-os workers 已退役,v2-worker 已停用)
check_launch_agent com.zhen.weixin-agent
check_launch_agent com.zhen.weixin-monitor
check_launch_agent com.zhen.weixin-session-manager
check_launch_agent com.zhen.chelaile-bus
check_launch_agent com.zhen.api-server
check_launch_agent com.zhen.cloudflared
check_launch_agent com.zhen.caffeinate

# 间隔作业：两次 tick 之间 PID 经常是 `-`、state=not running，只查 loaded。
if launchctl print "gui/${UID_NUM}/com.zhen.weixin-reply-watch" >/dev/null 2>&1; then
  ok "reply-watch LaunchAgent loaded"
else
  fail "reply-watch LaunchAgent 未加载"
fi

# 摄像头服务按需启用。未加载表示用户主动停用,不应让日常体检报警；
# 一旦加载,仍严格检查进程和抓帧新鲜度,避免“看似启动、实际断流”。
room_recorder_domain="gui/${UID_NUM}/com.zhen.room-recorder"
room_digest_domain="gui/${UID_NUM}/com.zhen.room-digest"
if launchctl print "$room_recorder_domain" >/dev/null 2>&1; then
  check_launch_agent com.zhen.room-recorder
  rr_state="$ROOT/media/room-log/.state.json"
  if [ -f "$rr_state" ]; then
    rr_age=$(( $(date '+%s') - $(stat -f %m "$rr_state") ))
    if [ "$rr_age" -gt 300 ]; then
      fail "room-recorder .state.json ${rr_age}s 没更新 — 抓帧可能一直失败(摄像头掉线/换IP?)"
    else
      ok "room-recorder 最近 ${rr_age}s 内抓过帧"
    fi
  else
    warn "room-recorder 从未写过 state(刚装?)"
  fi

  # 摘要是 StartInterval 型,只要求 loaded,不要求持续 running。
  if launchctl print "$room_digest_domain" >/dev/null 2>&1; then
    ok "room-digest LaunchAgent loaded (每小时增量标注)"
  else
    fail "room-digest LaunchAgent 未加载 — 事件帧不会有文字标注"
  fi
else
  ok "room-recorder disabled (按需启用)"
  if launchctl print "$room_digest_domain" >/dev/null 2>&1; then
    warn "room-digest 仍加载,但 room-recorder 已停用"
  else
    ok "room-digest disabled with room-recorder"
  fi
fi

section "chat reply"
if chat_health=$(node "$ROOT/scripts/chat-health.mjs" 2>/dev/null); then
  ok "$chat_health"
else
  fail "${chat_health:-chat reply check unavailable}"
fi

section "brain lifeline"
# Claude API 必须经 Clash 代理可达(CN 直连被 403);探测失败=助理大脑离线
if curl -fsS --max-time 8 -x http://127.0.0.1:7897 -o /dev/null https://api.anthropic.com/v1/models 2>/dev/null || \
   [ "$(curl -s --max-time 8 -x http://127.0.0.1:7897 -o /dev/null -w '%{http_code}' https://api.anthropic.com/v1/models 2>/dev/null)" != "000" ]; then
  ok "claude API reachable via clash proxy"
else
  fail "claude API NOT reachable via clash proxy (Clash down? network?)"
fi

# 心跳存活自检 —— 谁来监控监控者。
# 2026-07-03 ~ 07-26:心跳连续失败 755 次,整整 23 天无人察觉,因为这套体检查了 56 项
# 唯独没查心跳自己还活着没有。心跳一死,待办提醒/新闻主动开口全部静默消失。
# 心跳 2026-07-26 从 cron 迁到 launchd:cron 进程拿不到登录钥匙串里的 OAuth 凭证
# (keychain rc=44 / "Not logged in"),launchd gui/501 域可以。别再挪回 cron。
# StartCalendarInterval 是 08:00-23:30 每 30 分钟。print 成功即 loaded，不要看 PID。
if launchctl print "gui/${UID_NUM}/com.zhen.weixin-heartbeat" >/dev/null 2>&1; then
  ok "heartbeat LaunchAgent loaded"
else
  fail "heartbeat LaunchAgent 未加载 — 主动提醒能力离线"
fi

hb_log="$ROOT/tmp/heartbeat.log"
hb_hour=$(date '+%-H')
hb_minute=$(date '+%-M')
if [ ! -f "$hb_log" ]; then
  fail "heartbeat log missing (心跳从未跑过?)"
elif [ "$hb_hour" -ge 8 ] && [ "$hb_hour" -le 23 ]; then
  # 心跳是 08:00-23:30 每 30 分钟。08:00 首轮与体检可能并发,给 15 分钟启动宽限；
  # 08:30 起就应已有当天成功记录,不再让上午故障静默到 11 点。
  if [ "$hb_hour" -eq 8 ] && [ "$hb_minute" -lt 15 ]; then
    ok "heartbeat startup grace period (当前 ${hb_hour}:$(printf '%02d' "$hb_minute"))"
  else
    hb_last=$(grep -E '\] (OK|SPOKE:)' "$hb_log" 2>/dev/null | tail -1 | sed -n 's/^\[\([0-9-]* [0-9:]*\)\].*/\1/p')
    if [ -z "$hb_last" ]; then
      hb_fails=$(grep -c 'heartbeat error' "$hb_log" 2>/dev/null) || hb_fails=0
      fail "heartbeat 从无成功记录(累计失败 ${hb_fails} 次) — 主动提醒能力完全离线"
    else
      hb_epoch=$(date -j -f '%Y-%m-%d %H:%M:%S' "$hb_last" '+%s' 2>/dev/null || echo 0)
      hb_age=$(( ($(date '+%s') - hb_epoch) / 60 ))
      if [ "$hb_epoch" -eq 0 ]; then
        warn "heartbeat 时间戳解析失败: $hb_last"
      elif [ "$hb_age" -gt 180 ]; then
        fail "heartbeat 已 ${hb_age} 分钟没有成功记录(最后一次 $hb_last) — 主动提醒能力离线"
      else
        ok "heartbeat alive (最后成功 ${hb_age} 分钟前)"
      fi
    fi
  fi
else
  ok "heartbeat check skipped (当前 ${hb_hour} 点,心跳 launchd 只在 08:00-23:30 跑)"
fi

section "node runtime"
if [ -x "$ROOT/node_modules/.bin/weixin-acp" ]; then ok "local weixin-acp installed"; else fail "local weixin-acp missing; run npm install"; fi
if [ -x "$ROOT/node_modules/.bin/claude" ]; then ok "local claude installed"; else fail "local claude missing; run npm install"; fi
if [ -x "$ROOT/node_modules/.bin/claude" ]; then ok "optional local claude code installed"; else warn "optional local claude code missing; run npm install to enable Claude Code tool"; fi

# 20 个 patch-*.sh 每次启动 monkey-patch node_modules。它们"找不到目标就 skip"不炸启动,
# 但那意味着依赖升级后功能会静默消失。start.sh 把结果写进 tmp/patch-status.log,这里读它。
patch_log="$ROOT/tmp/patch-status.log"
if [ ! -f "$patch_log" ]; then
  ok "patch status not yet recorded (weixin-agent 下次重启后生成)"
else
  # 注意:grep -c 计数为 0 时退出码是 1,别用 `|| echo 0` 兜底(会拼成 "0\n0")
  skipped=$(grep -c 'skipping' "$patch_log" 2>/dev/null) || skipped=0
  total=$(grep -c '^--- ' "$patch_log" 2>/dev/null) || total=0
  if [ "$skipped" -eq 0 ]; then
    ok "all $total node_modules patches applied"
  else
    warn "$skipped 个 patch 没打上(依赖可能升级过): $(grep 'skipping' "$patch_log" | head -3 | tr '\n' ';')"
  fi
fi
node_version=$(node -v 2>/dev/null || true)
if [ -n "$node_version" ]; then
  ok "node $node_version"
  major=${node_version#v}; major=${major%%.*}
  if [ "$major" -lt 22 ]; then warn "weixin-acp 0.6.0 declares node >=22; current is $node_version"; fi
else
  fail "node missing"
fi

section "weixin push state"
if [ -f /Users/zhen/.openclaw/openclaw-weixin/accounts.json ]; then ok "weixin accounts.json exists"; else fail "weixin accounts.json missing; run weixin-acp login"; fi
account_count=$(find /Users/zhen/.openclaw/openclaw-weixin/accounts -maxdepth 1 -name '*-im-bot.json' 2>/dev/null | wc -l | tr -d ' ')
if [ "${account_count:-0}" -gt 0 ]; then ok "weixin account token file count=$account_count"; else fail "weixin account token file missing"; fi
if [ "${1:-}" = "--send" ]; then
  if /opt/homebrew/bin/node "$ROOT/weixin-send.mjs" --text "healthcheck: weixin-send ok $(date '+%F %T')" >/tmp/weixin-healthcheck-send.out 2>&1; then
    ok "weixin-send test message sent"
  else
    fail "weixin-send test failed: $(cat /tmp/weixin-healthcheck-send.out)"
  fi
else
  ok "weixin-send not exercised; pass --send to send a test message"
fi

section "claude runtime"
if "$ROOT/venv/bin/python" "$ROOT/core/claude_task.py" doctor >/tmp/claude-task-doctor.json 2>/tmp/claude-task-doctor.err && grep -q '"ok": true' /tmp/claude-task-doctor.json; then
  ok "optional claude code tool callable"
else
  warn "optional claude code tool unavailable: $(cat /tmp/claude-task-doctor.json /tmp/claude-task-doctor.err 2>/dev/null)"
fi

section "cron"
cron=$(crontab -l 2>/dev/null || true)
reading_count=$(printf '%s\n' "$cron" | grep -c 'reading-digest.mjs' || true)
if [ "$reading_count" -eq 0 ]; then ok "reading digest cron disabled as intended"; else warn "reading digest cron count=$reading_count (2026-07-03 起应为 0)"; fi
if printf '%s\n' "$cron" | grep -q 'geofence_reminder.py check --quiet'; then ok "geofence cron installed"; else fail "geofence cron missing"; fi
if printf '%s\n' "$cron" | grep -q 'backup-memory.sh'; then ok "memory backup cron installed"; else warn "memory backup cron missing"; fi
if printf '%s\n' "$cron" | grep -q 'scripts/memory-optimize.mjs'; then ok "memory optimize cron installed"; else warn "memory optimize cron missing"; fi
if printf '%s\n' "$cron" | grep -q '^NO_PROXY=.*192\.168\.3\.6'; then ok "cron NO_PROXY includes HA local IP"; else warn "cron NO_PROXY does not include 192.168.1.100"; fi

section "local APIs"
if curl -fsS --max-time 5 http://127.0.0.1:8080/api/status >/tmp/chelaile-healthcheck.json; then ok "chelaile API status ok"; else fail "chelaile API status failed"; fi
preview_status=$(curl -sS --max-time 5 -o /tmp/preview-healthcheck.out -w '%{http_code}' http://127.0.0.1:8081/preview/__missing__ 2>/tmp/preview-healthcheck.err || true)
if [ "$preview_status" = "404" ]; then ok "preview API route reachable"; else fail "preview API route failed: status=$preview_status $(cat /tmp/preview-healthcheck.err)"; fi
remote_connect_url=$("$ROOT/venv/bin/python" "$ROOT/core/remote_hosts.py" connect-url --base-url http://127.0.0.1:8081 2>/tmp/remote-connect-url-healthcheck.err || true)
if [ -n "$remote_connect_url" ] && curl -fsS --max-time 5 "$remote_connect_url" >/tmp/remote-connect-healthcheck.html 2>/tmp/remote-connect-healthcheck.err && grep -q 'Home Agent Remote Connect' /tmp/remote-connect-healthcheck.html; then
  ok "remote connect page reachable"
else
  fail "remote connect page failed: $(cat /tmp/remote-connect-url-healthcheck.err /tmp/remote-connect-healthcheck.err 2>/dev/null)"
fi
remote_connector_url=${remote_connect_url/\/remote\/connect/\/remote\/connector.py}
if [ -n "$remote_connector_url" ] && curl -fsS --max-time 5 "$remote_connector_url" >/tmp/home-agent-connector-healthcheck.py 2>/tmp/remote-connector-healthcheck.err && "$ROOT/venv/bin/python" -m py_compile /tmp/home-agent-connector-healthcheck.py >/tmp/remote-connector-pycompile.out 2>&1; then
  ok "remote connector script downloads and compiles"
else
  fail "remote connector script failed: $(cat /tmp/remote-connector-healthcheck.err /tmp/remote-connector-pycompile.out 2>/dev/null)"
fi
listener=$(lsof -nP -iTCP:8080 -sTCP:LISTEN 2>/dev/null || true)
if printf '%s\n' "$listener" | grep -q '127.0.0.1:8080'; then
  ok "chelaile listens on 127.0.0.1:8080"
elif printf '%s\n' "$listener" | grep -q ':8080'; then
  fail "chelaile 8080 listener is not loopback-only"
else
  fail "chelaile 8080 listener not found"
fi

section "home assistant"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
if [ -n "${HA_URL:-}" ] && [ -n "${HA_TOKEN:-}" ]; then
  if curl -fsS --max-time 10 --noproxy 192.168.1.100 "$HA_URL/states/climate.gree" -H "Authorization: Bearer $HA_TOKEN" >/tmp/ha-healthcheck.json 2>/tmp/ha-healthcheck.err; then
    ha_state=$(node -e 'const fs=require("fs"); const j=JSON.parse(fs.readFileSync("/tmp/ha-healthcheck.json","utf8")); console.log(`${j.entity_id} ${j.state}`)')
    ok "HA API reachable: $ha_state"
  else
    fail "HA API failed: $(cat /tmp/ha-healthcheck.err)"
  fi
else
  fail "HA_URL or HA_TOKEN missing"
fi
if node --check "$ROOT/scripts/ha-fast-status.mjs" >/tmp/ha-fast-status-check.out 2>&1 && node "$ROOT/scripts/ha-fast-status.mjs" --json >/tmp/ha-fast-status.json 2>/tmp/ha-fast-status.err; then
  fast_ms=$(node -e 'const fs=require("fs"); const j=JSON.parse(fs.readFileSync("/tmp/ha-fast-status.json","utf8")); if (!j.ok) process.exit(1); console.log(j.durationMs)')
  ok "HA fast status runs in ${fast_ms}ms"
else
  fail "HA fast status failed: $(cat /tmp/ha-fast-status-check.out /tmp/ha-fast-status.err 2>/dev/null)"
fi

section "media and screenshot"
script_check_failed=0
: > /tmp/startup-script-healthcheck.out
for script in "$ROOT"/patch-*.sh "$ROOT"/start.sh "$ROOT"/backup-memory.sh "$ROOT"/cleanup-tmp.sh "$ROOT"/scripts/restart-weixin-agent.sh; do
  [ -e "$script" ] || continue
  if ! bash -n "$script" >>/tmp/startup-script-healthcheck.out 2>&1; then
    script_check_failed=1
  fi
done
if [ "$script_check_failed" -eq 0 ]; then ok "startup and patch scripts parse"; else fail "startup script check failed: $(cat /tmp/startup-script-healthcheck.out)"; fi
if node --check "$ROOT/scripts/memory-cleanup.mjs" >/tmp/memory-cleanup-check.out 2>&1 && node "$ROOT/scripts/memory-cleanup.mjs" --dry-run >/tmp/memory-cleanup-healthcheck.json 2>/tmp/memory-cleanup-healthcheck.err; then
  ok "memory cleanup dry-run ok"
else
  fail "memory cleanup failed: $(cat /tmp/memory-cleanup-check.out /tmp/memory-cleanup-healthcheck.err 2>/dev/null)"
fi
if "$ROOT/venv/bin/python" "$ROOT/services/session-manager.py" status --json >/tmp/session-manager-healthcheck.json 2>/tmp/session-manager-healthcheck.err \
  && node -e 'const j=JSON.parse(require("fs").readFileSync("/tmp/session-manager-healthcheck.json","utf8")); if(!j.ok || !j.metrics.session_id || !Number.isFinite(j.metrics.context_tokens) || j.gateway_enabled !== false) process.exit(1)' \
  && node --check "$ROOT/scripts/build-session-checkpoint.mjs" >/tmp/session-checkpoint-check.out 2>&1 \
  && node "$ROOT/scripts/build-session-checkpoint.mjs" --reason healthcheck --session-id healthcheck --no-model --output /tmp/session-checkpoint-healthcheck.json >/tmp/session-checkpoint-healthcheck.out 2>/tmp/session-checkpoint-healthcheck.err \
  && node -e 'const j=JSON.parse(require("fs").readFileSync("/tmp/session-checkpoint-healthcheck.json","utf8")); if(j.schema_version !== 1 || !j.current_topic || !Array.isArray(j.recent_turns)) process.exit(1)'; then
  ok "pressure-aware session manager and checkpoint builder ok"
else
  fail "session manager v2 failed: $(cat /tmp/session-manager-healthcheck.err /tmp/session-checkpoint-check.out /tmp/session-checkpoint-healthcheck.err 2>/dev/null)"
fi
if node --check "$ROOT/scripts/memory-optimize.mjs" >/tmp/memory-optimize-check.out 2>&1 && node "$ROOT/scripts/memory-optimize.mjs" daily --dry-run >/tmp/memory-optimize-healthcheck.json 2>/tmp/memory-optimize-healthcheck.err; then
  ok "memory optimize dry-run ok"
else
  fail "memory optimize failed: $(cat /tmp/memory-optimize-check.out /tmp/memory-optimize-healthcheck.err 2>/dev/null)"
fi
if node --check "$ROOT/scripts/write-session-handoff.mjs" >/tmp/session-handoff-check.out 2>&1 && node "$ROOT/scripts/write-session-handoff.mjs" --reason healthcheck --source healthcheck --dry-run >/tmp/session-handoff-healthcheck.json 2>/tmp/session-handoff-healthcheck.err; then
  ok "session handoff dry-run ok"
else
  fail "session handoff failed: $(cat /tmp/session-handoff-check.out /tmp/session-handoff-healthcheck.err 2>/dev/null)"
fi
if node --check "$ROOT/scripts/memory-audit.mjs" >/tmp/memory-audit-check.out 2>&1 && node "$ROOT/scripts/memory-audit.mjs" >/tmp/memory-audit-healthcheck.json 2>/tmp/memory-audit-healthcheck.err; then
  ok "memory audit ok"
else
  fail "memory audit failed: $(cat /tmp/memory-audit-check.out /tmp/memory-audit-healthcheck.json /tmp/memory-audit-healthcheck.err 2>/dev/null)"
fi
if node --check "$ROOT/scripts/publish-preview.mjs" >/tmp/publish-preview-check.out 2>&1 && node --check "$ROOT/scripts/preview-admin.mjs" >/tmp/preview-admin-check.out 2>&1 && node "$ROOT/scripts/preview-admin.mjs" list --json >/tmp/preview-admin-healthcheck.json 2>/tmp/preview-admin-healthcheck.err; then
  ok "preview publish tools ok"
else
  fail "preview publish tools failed: $(cat /tmp/publish-preview-check.out /tmp/preview-admin-check.out /tmp/preview-admin-healthcheck.err 2>/dev/null)"
fi
if bash -n "$ROOT/scripts/cleanup-downloaded-videos.sh" && "$ROOT/scripts/cleanup-downloaded-videos.sh" --dry-run >/tmp/video-cleanup-healthcheck.out 2>&1; then
  ok "downloaded video cleanup dry-run ok"
else
  fail "downloaded video cleanup failed: $(cat /tmp/video-cleanup-healthcheck.out 2>/dev/null)"
fi
if printf '%s\n' "$cron" | grep -q 'scripts/cleanup-downloaded-videos.sh'; then
  ok "weekly downloaded video cleanup cron installed"
else
  fail "weekly downloaded video cleanup cron missing"
fi
if bash -n "$ROOT/apple_bridge/build.sh" "$ROOT/scripts/apple-bridge.sh" "$ROOT/tests/test-apple-bridge.sh" \
  && "$ROOT/tests/test-apple-bridge.sh" >/tmp/apple-bridge-healthcheck.out 2>/tmp/apple-bridge-healthcheck.err; then
  ok "Apple Bridge build and safe previews ok"
else
  fail "Apple Bridge failed: $(cat /tmp/apple-bridge-healthcheck.out /tmp/apple-bridge-healthcheck.err 2>/dev/null)"
fi
if node --check "$ROOT/scripts/browser-bridge-lib.mjs" \
  && node --check "$ROOT/scripts/browser-bridge.mjs" \
  && node --check "$ROOT/scripts/browser.mjs" \
  && node --check "$ROOT/scripts/agent-browser-open.mjs" \
  && node --check "$ROOT/scripts/agent-browser-preply.mjs" \
  && node --test "$ROOT/tests/test_browser_bridge.mjs" >/tmp/browser-bridge-test.out 2>/tmp/browser-bridge-test.err \
  && node "$ROOT/scripts/browser-bridge.mjs" doctor >/tmp/browser-bridge-doctor.json 2>/tmp/browser-bridge-doctor.err \
  && node -e 'const j=JSON.parse(require("fs").readFileSync("/tmp/browser-bridge-doctor.json","utf8")); if(!j.ok || !j.data.chrome_available || !j.data.profile_ready || !j.data.endpoint_loopback_only || j.data.cookies_exported || j.data.arbitrary_script_exposed) process.exit(1)'; then
  ok "Browser Bridge profile and safety checks ok"
else
  fail "Browser Bridge failed: $(cat /tmp/browser-bridge-test.out /tmp/browser-bridge-test.err /tmp/browser-bridge-doctor.err 2>/dev/null)"
fi
if [ -f "$ROOT/node_modules/weixin-agent-sdk/dist/index.mjs" ] && grep -q 'weixin message queue patch' "$ROOT/node_modules/weixin-agent-sdk/dist/index.mjs" && grep -q 'enqueueAgentChat' "$ROOT/node_modules/weixin-agent-sdk/dist/index.mjs"; then
  ok "weixin message queue patch installed"
else
  fail "weixin message queue patch missing"
fi
if [ -f "$ROOT/node_modules/weixin-agent-sdk/dist/index.mjs" ] && ! grep -qE 'maybeHandleFastHome(Control|Status)' "$ROOT/node_modules/weixin-agent-sdk/dist/index.mjs"; then
  ok "HA fast home shortcuts disabled"
else
  fail "HA fast home shortcuts still installed"
fi
if [ -f "$ROOT/node_modules/weixin-agent-sdk/dist/index.mjs" ] && ! grep -q 'maybeHandleAgentOSControl' "$ROOT/node_modules/weixin-agent-sdk/dist/index.mjs"; then
  ok "direct Agent OS home-control gateway disabled"
else
  fail "direct Agent OS home-control gateway still installed"
fi
if [ -f "$ROOT/node_modules/weixin-agent-sdk/dist/index.mjs" ] && grep -q 'weixin agent os generic gateway patch' "$ROOT/node_modules/weixin-agent-sdk/dist/index.mjs"; then
  ok "optional Agent OS generic gateway patch installed"
else
  fail "optional Agent OS generic gateway patch missing"
fi
if [ -f "$ROOT/node_modules/weixin-agent-sdk/dist/index.mjs" ] && grep -q 'weixin-agent-os-send-file' "$ROOT/node_modules/weixin-agent-sdk/dist/index.mjs"; then
  ok "Agent OS gateway send_file patch installed"
else
  fail "Agent OS gateway send_file patch missing"
fi
if node --check "$ROOT/scripts/ha-fast-control.mjs" >/tmp/ha-fast-control-check.out 2>&1; then
  ok "HA fast control script parses"
else
  fail "HA fast control script check failed: $(cat /tmp/ha-fast-control-check.out 2>/dev/null)"
fi
if bash -n "$ROOT/patches/patch-weixin-media-vault.sh" && node --check "$ROOT/scripts/media-search.mjs" >/tmp/media-search-check.out 2>&1; then ok "media vault scripts parse"; else fail "media vault script check failed: $(cat /tmp/media-search-check.out)"; fi
if node "$ROOT/scripts/media-search.mjs" --type image --limit 1 >/tmp/media-search.out 2>&1; then ok "media search runs"; else fail "media search failed: $(cat /tmp/media-search.out)"; fi
if bash -n "$ROOT/scripts/restart-weixin-agent.sh" && bash -n "$ROOT/patches/patch-weixin-self-restart.sh"; then ok "self restart scripts parse"; else fail "self restart script check failed"; fi
shot="/tmp/weixin-agent-healthcheck-screen.png"
rm -f "$shot"
if /usr/sbin/screencapture -x "$shot" >/tmp/screencapture-healthcheck.out 2>&1 && [ -s "$shot" ]; then
  dims=$(sips -g pixelWidth -g pixelHeight "$shot" 2>/dev/null | awk '/pixel/ {printf "%s%s", sep $2, $3; sep="x"}')
  ok "screencapture ok ${dims:-unknown-size}"
else
  warn "screencapture unavailable (locked screen is normal): $(cat /tmp/screencapture-healthcheck.out)"
fi

section "python tools"
if "$ROOT/venv/bin/python" -m py_compile "$ROOT/ha_mcp_server.py" "$ROOT/services/monitor.py" "$ROOT/services/session-manager.py" "$ROOT/geofence_reminder.py" "$ROOT/services/api-server.py" "$ROOT/core/app_notify.py" "$ROOT/core/remote_hosts.py" "$ROOT/core/local_file_tool.py" "$ROOT/core/claude_task.py" "$ROOT/core/codex_task.py" "$ROOT"/agent_os/*.py >/tmp/pycompile-healthcheck.out 2>&1; then
  ok "core python files compile"
else
  fail "python compile failed: $(cat /tmp/pycompile-healthcheck.out)"
fi
agent_os_test_db=$(mktemp "/tmp/agent-os-healthcheck.XXXXXX")
if "$ROOT/venv/bin/python" -m agent_os --db "$agent_os_test_db" init --json >/tmp/agent-os-init-healthcheck.json 2>/tmp/agent-os-init-healthcheck.err \
  && "$ROOT/venv/bin/python" -m agent_os --db "$agent_os_test_db" classify "把客厅灯关掉" --json >/tmp/agent-os-classify-healthcheck.json 2>/tmp/agent-os-classify-healthcheck.err \
  && run_with_timeout 135 "$ROOT/venv/bin/python" -m agent_os --db "$agent_os_test_db" gateway "家里什么设备开着呢" --inline --wait --json --timeout 120 >/tmp/agent-os-gateway-healthcheck.json 2>/tmp/agent-os-gateway-healthcheck.err \
  && node - <<'NODECHECK' >/tmp/agent-os-gateway-assert.out 2>&1
const fs = require("fs");
const data = JSON.parse(fs.readFileSync("/tmp/agent-os-gateway-healthcheck.json", "utf8"));
if (!data.ok || !data.task?.folder_id || !data.response?.response_id) process.exit(1);
if (data.response.folder_id !== data.task.folder_id) process.exit(1);
if (data.task.worker_type !== 'control') process.exit(1);
NODECHECK
then
  ok "agent os gateway/folder/agent-response ok"
else
  fail "agent os check failed: $(cat /tmp/agent-os-init-healthcheck.err /tmp/agent-os-classify-healthcheck.err /tmp/agent-os-gateway-healthcheck.err /tmp/agent-os-gateway-assert.out 2>/dev/null)"
fi
rm -f "$agent_os_test_db" "$agent_os_test_db-shm" "$agent_os_test_db-wal"
if "$ROOT/venv/bin/python" "$ROOT/core/remote_hosts.py" list --json >/tmp/remote-hosts-healthcheck.json 2>/tmp/remote-hosts-healthcheck.err; then
  ok "remote host registry readable"
else
  fail "remote host registry failed: $(cat /tmp/remote-hosts-healthcheck.err)"
fi
if ! grep -qE '"connectorToken"|"password"|"stdout"|"stderr"|"command"' /tmp/remote-hosts-healthcheck.json; then
  ok "remote host registry list is redacted"
else
  fail "remote host registry list leaks sensitive fields"
fi
if "$ROOT/venv/bin/python" - <<'PYMOD' >/tmp/pymod-healthcheck.out 2>&1
import httpx
import dotenv
import mcp
import paramiko
print("imports ok")
PYMOD
then
  ok "python MCP dependencies import"
else
  fail "python MCP dependency import failed: $(cat /tmp/pymod-healthcheck.out)"
fi

section "prompt inject hook"
if /opt/homebrew/bin/node --check "$ROOT/scripts/prompt-inject.mjs"; then ok "prompt-inject.mjs parses"; else fail "prompt-inject.mjs syntax"; fi
if python3 - "$ROOT/.claude/settings.json" <<'HOOKJSON'
import json, sys
from pathlib import Path
s = json.loads(Path(sys.argv[1]).read_text())
hooks = s.get("hooks", {}).get("UserPromptSubmit", [])
try:
    cmd = hooks[0]["hooks"][0]["command"]
except Exception:
    raise SystemExit("missing UserPromptSubmit command")
if "scripts/prompt-inject.mjs" not in cmd:
    raise SystemExit("hook command does not point at prompt-inject.mjs")
print("ok")
HOOKJSON
then ok "UserPromptSubmit hook registered"; else fail "UserPromptSubmit hook missing"; fi
eat=$(/opt/homebrew/bin/node "$ROOT/scripts/prompt-inject.mjs" --probe "今晚吃啥好呢" 2>/dev/null || true)
gate=$(/opt/homebrew/bin/node "$ROOT/scripts/prompt-inject.mjs" --probe "周末去澳门哪个口岸人少" 2>/dev/null || true)
mkt=$(/opt/homebrew/bin/node "$ROOT/scripts/prompt-inject.mjs" --probe "今天大盘怎么样" 2>/dev/null || true)
bootstrap_probe=$(/opt/homebrew/bin/node "$ROOT/scripts/prompt-inject.mjs" --probe-bootstrap "你蒸馏下这个教程文字发给我，我记下来" 2>/dev/null || true)
if printf '%s' "$eat" | grep -qE 'skill:|sniff:'; then fail "prompt-inject too chatty on 今晚吃啥"; else ok "prompt-inject stays quiet on small talk"; fi
if printf '%s' "$gate" | grep -q "macau-border"; then ok "prompt-inject routes 澳门口岸 to macau-border"; else fail "prompt-inject missed 澳门口岸"; fi
if printf '%s' "$mkt" | grep -q "stock-quote"; then ok "prompt-inject routes 大盘 to stock-quote"; else fail "prompt-inject missed 大盘"; fi
# 2026-09-13 起对话库可用时不再附 recent-context 非心跳流水，只认注入块的首尾标记。
if printf '%s' "$bootstrap_probe" | grep -q "强制已读，不是后台提示" \
  && printf '%s' "$bootstrap_probe" | grep -q "强制已读结束" \
  && ! printf '%s' "$bootstrap_probe" | grep -q "不相关当没看见"; then
  ok "prompt-inject bootstrap context is mandatory"
else
  fail "prompt-inject bootstrap context contract"
fi

# 用指代句验证“接着聊刚才心跳”的能力,避免把测试绑死在某条历史新闻关键词上。
# 2026-09-13 起光有“刚才/那条”不够，要带“推送/提醒/新闻”这类话题证据才会附上候选。
# 没有 4 小时内的主动消息时没有可跟进对象,属于正常状态,不报故障。
followup=$(/opt/homebrew/bin/node "$ROOT/scripts/prompt-inject.mjs" --probe "你刚主动推送的那条详细说说" 2>/dev/null || true)
proactive_path="$ROOT/runtime/prompt-inject/last-proactive.json"
if [ -f "$proactive_path" ]; then
  proactive_age=$(( $(date '+%s') - $(stat -f %m "$proactive_path") ))
else
  proactive_age=999999
fi
if [ "$proactive_age" -le 14400 ]; then
  if printf '%s' "$followup" | grep -q "proactive-candidate:"; then
    ok "prompt-inject attaches recent heartbeat on follow-up"
  else
    fail "prompt-inject missed recent heartbeat follow-up"
  fi
else
  ok "prompt-inject follow-up probe skipped (no recent proactive message)"
fi

unrelated=$(/opt/homebrew/bin/node "$ROOT/scripts/prompt-inject.mjs" --probe "紫色河马编号QZXV731" 2>/dev/null || true)
if printf '%s' "$unrelated" | grep -q "proactive:"; then fail "prompt-inject mixed heartbeat into unrelated chat"; else ok "prompt-inject keeps heartbeat off unrelated chats"; fi

section "proxy and live config"
# 死端口 7890 只该出现在即兴 shell。指令/技能/脚本树里出现则是事故复发。
# 不扫 memory/ 流水和 checkpoint：8-29 事故记录里有 7890，扫了今天会误红。
dead_proxy_roots=()
[ -f "$ROOT/CLAUDE.md" ] && dead_proxy_roots+=("$ROOT/CLAUDE.md")
[ -d "$ROOT/memory/skills" ] && dead_proxy_roots+=("$ROOT/memory/skills")
[ -d "$ROOT/skills" ] && dead_proxy_roots+=("$ROOT/skills")
[ -d "$ROOT/scripts" ] && dead_proxy_roots+=("$ROOT/scripts")
[ -d "$ROOT/tools" ] && dead_proxy_roots+=("$ROOT/tools")
dead_proxy_hits=""
if [ "${#dead_proxy_roots[@]}" -gt 0 ]; then
  dead_proxy_hits=$(grep -RIl --exclude-dir node_modules --exclude-dir .git '127.0.0.1:7890' "${dead_proxy_roots[@]}" 2>/dev/null || true)
fi
if [ -n "$dead_proxy_hits" ]; then
  dead_proxy_rel=$(printf '%s\n' "$dead_proxy_hits" | sed "s|^$ROOT/||" | paste -sd ' ' -)
  fail "dead proxy 127.0.0.1:7890 in tree: $dead_proxy_rel"
else
  ok "no 127.0.0.1:7890 in CLAUDE.md/memory/skills/scripts/tools"
fi

inject_log="$ROOT/runtime/prompt-inject/inject.log"
if [ -f "$inject_log" ]; then
  inject_bytes=$(stat -f %z "$inject_log")
  if [ "$inject_bytes" -gt 1048576 ]; then
    warn "inject.log ${inject_bytes} bytes >1MB"
  else
    ok "inject.log ${inject_bytes} bytes"
  fi
else
  ok "inject.log not present"
fi

# 只判断 didi args 指向的脚本在不在。不要把 settings 内容或 key 打到日志。
didi_rc=0
didi_out=$(python3 - "$ROOT/.claude/settings.json" <<'DIDIJSON' 2>/tmp/didi-mcp-path.err
import json
import os
import sys
from pathlib import Path

settings = Path(sys.argv[1])
if not settings.is_file():
    print("missing-settings")
    raise SystemExit(2)
try:
    data = json.loads(settings.read_text())
except Exception:
    print("invalid-json")
    raise SystemExit(2)
didi = (data.get("mcpServers") or {}).get("didi-mcp") or {}
args = didi.get("args") or []
paths = []
for arg in args:
    if not isinstance(arg, str):
        continue
    if arg.endswith(".py") or arg.endswith(".mjs") or "didi" in arg.lower():
        paths.append(arg)
if not paths:
    print("missing-arg")
    raise SystemExit(2)
missing = [os.path.basename(p) for p in paths if not os.path.isfile(p)]
if missing:
    print("missing-file")
    raise SystemExit(1)
print("ok")
DIDIJSON
) || didi_rc=$?
if [ "$didi_rc" -eq 0 ]; then
  ok "didi-mcp args path exists"
else
  fail "didi-mcp args path missing"
fi

section "resources"
disk_pct=$(df "$ROOT" | awk 'NR==2 {gsub("%", "", $5); print $5}')
if [ "${disk_pct:-100}" -lt 85 ]; then ok "disk usage ${disk_pct}%"; else warn "disk usage ${disk_pct}%"; fi
page_size=$(vm_stat | awk '/page size of/ {print $8}')
free_pages=$(vm_stat | awk '/Pages free/ {gsub("\\.","",$3); print $3}')
inactive_pages=$(vm_stat | awk '/Pages inactive/ {gsub("\\.","",$3); print $3}')
if [ -n "${page_size:-}" ] && [ -n "${free_pages:-}" ]; then
  available_mb=$(( (free_pages + ${inactive_pages:-0}) * page_size / 1024 / 1024 ))
  ok "memory available approx ${available_mb}MB"
else
  warn "memory stats unavailable"
fi

echo ""
echo "summary: ok=$PASS warn=$WARN fail=$FAIL"
if [ "$FAIL" -gt 0 ]; then exit 1; fi
