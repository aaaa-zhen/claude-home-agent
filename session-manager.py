"""
Session Lifecycle Manager
- Never resets if there was activity in the last 30 minutes
- Resets daily after 4am at most once, only when idle 2h+
- Resets after 2h of true inactivity, once per activity window
- Writes a lightweight session summary before reset
- start.sh / systemd auto-restarts after kill
- Cross-platform: works on both Windows and Linux
"""

import os
import sys
import time
import json
import logging
import subprocess
import platform
from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [session-mgr] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "memory", "session-state.json")
RESTART_LOG = os.path.join(SCRIPT_DIR, "memory", "session-restarts.log")
RECENT_CONTEXT = os.path.join(SCRIPT_DIR, "memory", "recent-context.md")
CONVERSATION_SUMMARY = os.path.join(SCRIPT_DIR, "memory", "conversation-summary.md")

DAILY_RESET_HOUR = 4        # 4am
IDLE_RESET_HOURS = 2         # reset after 2h true idle
RECENT_ACTIVITY_MINUTES = 30 # never reset if active within 30min
CHECK_INTERVAL = 60          # check every 60 seconds

IS_WINDOWS = platform.system() == "Windows"


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.error(f"Failed to save state: {e}")


def get_last_activity():
    """Read last_activity from state and weixin-agent journal prompt logs."""
    candidates = []
    try:
        state = load_state()
        ts = state.get("last_activity")
        if ts:
            candidates.append(datetime.fromisoformat(ts).timestamp())
    except Exception:
        pass

    journal_ts = get_last_journal_prompt_activity()
    if journal_ts:
        candidates.append(journal_ts)
    return max(candidates) if candidates else 0


def get_last_activity_marker():
    """Stable marker used to avoid repeated idle resets for the same idle window."""
    last_activity = get_last_activity()
    if last_activity:
        return datetime.fromtimestamp(last_activity).isoformat(timespec="seconds")
    try:
        state = load_state()
        ts = state.get("last_activity")
        if ts:
            return ts
    except Exception:
        pass
    return "never"


def get_last_journal_prompt_activity():
    """Return timestamp for the latest user prompt seen in systemd journal."""
    if IS_WINDOWS:
        return 0
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u",
                "weixin-agent",
                "--since",
                "15 minutes ago",
                "-o",
                "short-iso",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as e:
        log.error(f"Read journal activity failed: {e}")
        return 0

    latest = 0
    for line in result.stdout.splitlines():
        if "[acp] prompt:" not in line:
            continue
        ts = line.split(maxsplit=1)[0]
        try:
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")
            latest = max(latest, dt.timestamp())
        except Exception:
            continue
    return latest


def sync_journal_activity(state):
    """Persist journal-derived prompt activity into session-state.json."""
    journal_ts = get_last_journal_prompt_activity()
    if not journal_ts:
        return state

    state_ts = 0
    try:
        current = state.get("last_activity")
        if current:
            state_ts = datetime.fromisoformat(current).timestamp()
    except Exception:
        state_ts = 0

    if journal_ts > state_ts:
        state["last_activity"] = datetime.fromtimestamp(journal_ts).isoformat(timespec="seconds")
        save_state(state)
        log.info(f"Synced last_activity from journal: {state['last_activity']}")
    return state


def find_weixin_pids():
    """Find PIDs of weixin-acp node processes. Cross-platform."""
    pids = []
    try:
        if IS_WINDOWS:
            result = subprocess.run(
                ['powershell', '-Command',
                 "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
                 "Where-Object { $_.CommandLine -like '*weixin-acp*' } | "
                 "Select-Object -ExpandProperty ProcessId"],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))
        else:
            result = subprocess.run(
                ['pgrep', '-f', 'weixin-acp'],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.strip().split('\n'):
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))
    except Exception as e:
        log.error(f"Find PIDs failed: {e}")
    return pids


def is_weixin_running():
    """Check if weixin-acp process is running."""
    return len(find_weixin_pids()) > 0


def kill_weixin():
    """Kill all weixin-acp processes. Cross-platform."""
    pids = find_weixin_pids()
    if not pids:
        log.info("No weixin-acp process found to kill")
        return

    for pid in pids:
        try:
            if IS_WINDOWS:
                subprocess.run(
                    ['taskkill', '/PID', str(pid), '/T', '/F'],
                    capture_output=True, timeout=10
                )
            else:
                subprocess.run(
                    ['kill', '-TERM', str(pid)],
                    capture_output=True, timeout=10
                )
            log.info(f"Killed PID {pid}")
        except Exception as e:
            log.error(f"Kill PID {pid} failed: {e}")


def log_restart(reason):
    """Log restart event."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(RESTART_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {reason}\n")
        # Trim restart log (keep last 100 lines)
        with open(RESTART_LOG, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > 100:
            with open(RESTART_LOG, "w", encoding="utf-8") as f:
                f.writelines(lines[-100:])
    except Exception:
        pass


def _append_line(path, line):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except Exception as e:
        log.error(f"Failed to append {path}: {e}")


def _trim_recent_context(max_entries=10):
    try:
        with open(RECENT_CONTEXT, "r", encoding="utf-8") as f:
            lines = f.readlines()
        header = []
        entries = []
        for line in lines:
            if line.startswith("["):
                entries.append(line)
            else:
                header.append(line)
        with open(RECENT_CONTEXT, "w", encoding="utf-8") as f:
            f.writelines(header)
            f.writelines(entries[-max_entries:])
    except Exception as e:
        log.error(f"Failed to trim recent context: {e}")


def write_session_summary(reason, idle_minutes):
    """Persist reset metadata without polluting user-facing memory."""
    idle = "unknown" if idle_minutes is None else f"{int(idle_minutes)}min"
    last_activity = get_last_activity_marker()
    log_restart(f"{reason}, idle={idle}, last_activity={last_activity}")


def should_reset(state):
    """Determine if session should be reset."""
    now = datetime.now()
    last_reset = state.get("last_reset")

    if last_reset:
        last_reset_dt = datetime.fromisoformat(last_reset)
    else:
        last_reset_dt = now - timedelta(days=1)

    last_activity = get_last_activity()
    idle_minutes = (time.time() - last_activity) / 60 if last_activity > 0 else 999
    if idle_minutes < RECENT_ACTIVITY_MINUTES:
        return None, idle_minutes

    if idle_minutes < IDLE_RESET_HOURS * 60:
        return None, idle_minutes

    # Daily reset: after 4am, at most once per calendar day, only when truly idle.
    today = now.date().isoformat()
    if now.hour >= DAILY_RESET_HOUR and state.get("last_daily_reset_date") != today:
        return "daily_reset", idle_minutes

    # Idle reset: once per activity window, so an overnight idle period does not
    # restart the process every CHECK_INTERVAL.
    marker = get_last_activity_marker()
    hours_since_reset = (now - last_reset_dt).total_seconds() / 3600
    if (hours_since_reset >= IDLE_RESET_HOURS and
            state.get("last_activity_at_reset") != marker):
        return "idle_reset", idle_minutes

    return None, idle_minutes


def main():
    log.info(f"Session manager started (platform={platform.system()})")
    state = load_state()

    if not state.get("last_reset"):
        state["last_reset"] = datetime.now().isoformat()
        save_state(state)
        log.info("Initialized state")

    while True:
        try:
            state = load_state()
            state = sync_journal_activity(state)
            reason, idle_minutes = should_reset(state)
            if reason:
                log.info(f"Resetting session: {reason} (idle={int(idle_minutes)}min)")
                write_session_summary(reason, idle_minutes)
                kill_weixin()
                now = datetime.now()
                state["last_reset"] = now.isoformat()
                state["last_activity_at_reset"] = get_last_activity_marker()
                if reason == "daily_reset":
                    state["last_daily_reset_date"] = now.date().isoformat()
                save_state(state)
                log.info("Session killed, auto-restart will handle the rest")
        except Exception as e:
            log.error(f"Error: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
