#!/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
BRIDGE="$ROOT/scripts/apple-bridge.sh"
TMP_DIR=$(mktemp -d /tmp/home-agent-apple-bridge.XXXXXX)
trap 'rm -rf "$TMP_DIR"' EXIT

assert_json() {
  local file="$1"
  local expression="$2"
  node -e "const j=JSON.parse(require('fs').readFileSync(process.argv[1], 'utf8')); if (!($expression)) process.exit(1)" "$file"
}

"$BRIDGE" doctor >"$TMP_DIR/doctor.json"
assert_json "$TMP_DIR/doctor.json" 'j.ok && j.action === "doctor" && j.data.delete_operations_exposed === false'

"$BRIDGE" calendar create \
  --title "Preview" \
  --start "2026-07-12T09:00:00+08:00" \
  --end "2026-07-12T10:00:00+08:00" >"$TMP_DIR/calendar.json"
assert_json "$TMP_DIR/calendar.json" 'j.ok && j.dry_run === true && j.action === "calendar.create"'

"$BRIDGE" reminders create \
  --title "Preview" \
  --due "2026-07-12T09:00:00+08:00" >"$TMP_DIR/reminder.json"
assert_json "$TMP_DIR/reminder.json" 'j.ok && j.dry_run === true && j.action === "reminders.create"'

"$BRIDGE" notes create \
  --title "Preview" \
  --body "No write" >"$TMP_DIR/note.json"
assert_json "$TMP_DIR/note.json" 'j.ok && j.dry_run === true && j.action === "notes.create"'

# Concurrent callers previously raced inside LaunchServices, leaving one or
# more invocations with an empty result. The wrapper must queue them safely.
pids=()
for index in 1 2 3 4 5 6; do
  "$BRIDGE" doctor >"$TMP_DIR/concurrent-$index.json" &
  pids+=("$!")
done
for pid in "${pids[@]}"; do
  wait "$pid"
done
for index in 1 2 3 4 5 6; do
  assert_json "$TMP_DIR/concurrent-$index.json" 'j.ok && j.action === "doctor"'
done

echo "Apple Bridge safe preview tests passed"
