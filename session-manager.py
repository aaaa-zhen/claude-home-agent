"""
Session Lifecycle Manager
- Resets weixin-acp session daily at 4am (when idle 30min+)
- Resets after 8 hours of inactivity
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

DAILY_RESET_HOUR = 4        # 4am
IDLE_RESET_HOURS = 8         # reset after 8h idle
IDLE_CHECK_MINUTES = 30      # don't reset if active within 30min
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
    """Read last_activity from session-state.json (updated by Claude on each message)."""
    try:
        state = load_state()
        ts = state.get("last_activity")
        if ts:
            return datetime.fromisoformat(ts).timestamp()
    except Exception:
        pass
    return 0


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

    # Daily reset: after 4am, different day from last reset, idle 30min+
    if (now.hour >= DAILY_RESET_HOUR and
            now.date() > last_reset_dt.date() and
            idle_minutes >= IDLE_CHECK_MINUTES):
        return "daily_reset"

    # Idle reset: 8 hours no activity
    hours_since_reset = (now - last_reset_dt).total_seconds() / 3600
    if hours_since_reset >= IDLE_RESET_HOURS and idle_minutes >= IDLE_CHECK_MINUTES:
        return "idle_reset"

    return None


def main():
    log.info(f"Session manager started (platform={platform.system()})")
    state = load_state()

    if not state.get("last_reset"):
        state["last_reset"] = datetime.now().isoformat()
        save_state(state)
        log.info("Initialized state")

    while True:
        try:
            reason = should_reset(state)
            if reason:
                log.info(f"Resetting session: {reason}")
                kill_weixin()
                log_restart(reason)
                state["last_reset"] = datetime.now().isoformat()
                save_state(state)
                log.info("Session killed, auto-restart will handle the rest")
        except Exception as e:
            log.error(f"Error: {e}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
