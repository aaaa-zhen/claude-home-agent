#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
APP="$ROOT/runtime/apple-bridge/AppleBridge.app"
BIN="$APP/Contents/MacOS/AppleBridge"
SOURCE="$ROOT/apple_bridge/Sources/AppleBridge/main.swift"
INFO="$ROOT/apple_bridge/Info.plist"
LOCK_DIR="${TMPDIR:-/tmp}/home-agent-apple-bridge.lock"
LOCK_HELD=0
RESULT=""
ERROR_FILE=""

cleanup() {
  if [ -n "$RESULT" ]; then rm -f "$RESULT"; fi
  if [ -n "$ERROR_FILE" ]; then rm -f "$ERROR_FILE"; fi
  if [ "$LOCK_HELD" -eq 1 ]; then
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

# LaunchServices can intermittently fail when multiple callers open the same
# helper app at once. Serialize the whole build/launch/read cycle so every
# invocation gets its own complete result.
deadline=$((SECONDS + 30))
while ! mkdir "$LOCK_DIR" 2>/dev/null; do
  if [ -f "$LOCK_DIR/pid" ]; then
    owner_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || true)
    if [ -n "$owner_pid" ] && ! kill -0 "$owner_pid" 2>/dev/null; then
      stale_lock="${LOCK_DIR}.stale.$$"
      if mv "$LOCK_DIR" "$stale_lock" 2>/dev/null; then
        rm -rf "$stale_lock"
      fi
      continue
    fi
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo '{"ok":false,"error":"Apple Bridge is busy; timed out waiting for the launch lock"}'
    exit 1
  fi
  sleep 0.1
done
printf '%s\n' "$$" >"$LOCK_DIR/pid"
LOCK_HELD=1

if [ ! -x "$BIN" ] || [ "$SOURCE" -nt "$BIN" ] || [ "$INFO" -nt "$BIN" ]; then
  "$ROOT/apple_bridge/build.sh" >/dev/null
fi

RESULT=$(mktemp /tmp/home-agent-apple-bridge.XXXXXX)
ERROR_FILE=$(mktemp /tmp/home-agent-apple-bridge-error.XXXXXX)

attempt=1
while [ "$attempt" -le 3 ]; do
  : >"$RESULT"
  : >"$ERROR_FILE"
  /usr/bin/open -W -n "$APP" --args "$@" --result-file "$RESULT" \
    >/dev/null 2>"$ERROR_FILE" || true
  if [ -s "$RESULT" ]; then break; fi
  sleep "0.$((attempt * 2))"
  attempt=$((attempt + 1))
done

if [ ! -s "$RESULT" ]; then
  detail=$(tail -c 2000 "$ERROR_FILE" | tr '\n' ' ')
  node -e '
    const detail = process.argv[1].trim();
    const suffix = detail ? `: ${detail}` : "";
    console.log(JSON.stringify({
      ok: false,
      error: `Apple Bridge app produced no result after 3 attempts${suffix}`,
    }));
  ' "$detail"
  exit 1
fi
cat "$RESULT"
echo
node -e 'const j=JSON.parse(require("fs").readFileSync(process.argv[1], "utf8")); if (!j.ok) process.exit(1)' "$RESULT"
