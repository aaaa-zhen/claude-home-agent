"""Pressure-aware lifecycle manager for the live WeChat Claude session.

The assistant identity is durable; individual model sessions are bounded.
Under context pressure, /compact preserves continuity. Daily maintenance and
the maximum session age create a fresh session after checkpointing at an idle
boundary. Repeated hard-pressure compact failures also fall back to rotation.
SESSION_COMPACT_MODE=0 restores rotation for pressure events as well.

Agent OS Gateway remains out of the live message path.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows has no fcntl; lock-based holds are POSIX-only
    fcntl = None


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [session-mgr] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent  # this file lives in services/
MEMORY_DIR = ROOT / "memory"
TMP_DIR = ROOT / "tmp"
LOCKS_DIR = TMP_DIR / "locks"
RUNTIME_ENV_FILE = ROOT / "runtime" / "session-manager.env"
STATE_FILE = MEMORY_DIR / "session-state.json"
RESTART_LOG = MEMORY_DIR / "session-restarts.log"
CHECKPOINT_FILE = MEMORY_DIR / "session-checkpoint.json"
CHECKPOINT_ARCHIVE = MEMORY_DIR / "session-checkpoints"

IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"
LAUNCHD_WEIXIN_OUT_LOG = Path(
    os.getenv(
        "WEIXIN_AGENT_OUT_LOG",
        "/Users/zhen/home-agent/_migration/logs/com.zhen.weixin-agent.out.log",
    )
)
CLAUDE_PROJECT_DIR = Path(
    os.getenv(
        "WEIXIN_CLAUDE_PROJECT_DIR",
        "/Users/zhen/home-agent/.claude-agent/projects/-Users-zhen-home-agent-weixin-agent",
    )
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


DAILY_RESET_HOUR = _env_int("SESSION_DAILY_RESET_HOUR", 4)
DAILY_RESET_IDLE_MINUTES = _env_int("SESSION_DAILY_RESET_IDLE_MINUTES", 120)
MAX_SESSION_AGE_HOURS = _env_int("SESSION_MAX_AGE_HOURS", 24)
RECENT_ACTIVITY_MINUTES = _env_int("SESSION_RECENT_ACTIVITY_MINUTES", 30)
CHECK_INTERVAL = _env_int("SESSION_CHECK_INTERVAL_SECONDS", 60)

# Claude Code reports a 200k context window for the live model. Ratios are
# configurable so a future model switch does not require a code change.
CONTEXT_WINDOW_TOKENS = _env_int("SESSION_CONTEXT_WINDOW_TOKENS", 200_000)
SOFT_CONTEXT_RATIO = _env_float("SESSION_SOFT_CONTEXT_RATIO", 0.50)
HARD_CONTEXT_RATIO = _env_float("SESSION_HARD_CONTEXT_RATIO", 0.70)
SOFT_TURN_LIMIT = _env_int("SESSION_SOFT_TURN_LIMIT", 45)
HARD_TURN_LIMIT = _env_int("SESSION_HARD_TURN_LIMIT", 65)
SOFT_PRESSURE_IDLE_MINUTES = _env_int("SESSION_SOFT_PRESSURE_IDLE_MINUTES", 10)
HARD_PRESSURE_IDLE_MINUTES = _env_int("SESSION_HARD_PRESSURE_IDLE_MINUTES", 3)
MIN_PRESSURE_SESSION_AGE_MINUTES = _env_int("SESSION_MIN_PRESSURE_AGE_MINUTES", 45)
CHECKPOINT_ARCHIVE_LIMIT = _env_int("SESSION_CHECKPOINT_ARCHIVE_LIMIT", 30)

# Compact-in-place: at pressure, /compact is sent through the chat bridge
# socket instead of killing the process. The bridge serializes it behind any
# in-flight WeChat turn and its reply only returns to the socket client, so
# nothing leaks to WeChat. Verified 2026-08-31 end to end (weixin-acp →
# claude-agent-acp → Claude Code 2.1.183): the transcript gains a
# system/compact_boundary record with preTokens/postTokens metadata.
CHAT_SOCKET = ROOT / "runtime" / "acp-chat.sock"
COMPACT_MODE_DEFAULT = os.getenv("SESSION_COMPACT_MODE", "1")
COMPACT_TIMEOUT_SECONDS = _env_int("SESSION_COMPACT_TIMEOUT_SECONDS", 600)
# A compact leaves a summary behind; if that summary alone still clears the
# soft threshold, an interval+growth gate is the only thing standing between
# us and a compact loop, so both must pass before compacting again.
COMPACT_MIN_INTERVAL_MINUTES = _env_int("SESSION_COMPACT_MIN_INTERVAL_MINUTES", 15)
COMPACT_MIN_GROWTH_TOKENS = _env_int("SESSION_COMPACT_MIN_GROWTH_TOKENS", 20_000)
COMPACT_FAIL_ROTATE_THRESHOLD = _env_int("SESSION_COMPACT_FAIL_ROTATE_THRESHOLD", 2)
COMPACT_FOCUS = os.getenv(
    "SESSION_COMPACT_FOCUS",
    "保留:未完成的任务和承诺、用户最近提到的人/物/话题及其指代关系、"
    "进行中的对话主题、重要的时间/数字/事实、用户的语气和偏好",
)

SESSION_ID_RE = re.compile(
    r"(?:session created:\s*|\(session=)([0-9a-f]{8}-[0-9a-f-]{27,36})",
    re.IGNORECASE,
)


def parse_timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def iso_local(timestamp: float | None = None) -> str:
    value = time.time() if timestamp is None else timestamp
    return datetime.fromtimestamp(value).isoformat(timespec="seconds")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    """Atomically persist state so a restart cannot leave partial JSON."""
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        temp = STATE_FILE.with_name(f"{STATE_FILE.name}.{os.getpid()}.tmp")
        temp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, STATE_FILE)
    except OSError as exc:
        log.error("Failed to save state: %s", exc)


def read_log_tail(path: Path = LAUNCHD_WEIXIN_OUT_LOG, max_bytes: int = 524_288) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""


def active_session_id_from_text(text: str) -> str:
    matches = list(SESSION_ID_RE.finditer(text or ""))
    return matches[-1].group(1) if matches else ""


def get_active_session_id() -> str:
    return active_session_id_from_text(read_log_tail())


def is_acp_turn_busy() -> bool:
    """True when the latest prompt has no corresponding final response yet."""
    tail = read_log_tail(max_bytes=262_144)
    last_prompt = tail.rfind("[acp] prompt:")
    if last_prompt < 0:
        return False
    terminal_markers = (
        tail.rfind("[acp] response:"),
        tail.rfind("[acp] error:"),
        tail.rfind("[acp] subprocess exited"),
    )
    return max(terminal_markers) < last_prompt


_DEAD_SESSION_SKIPS_LOGGED: set[str] = set()


def is_acp_session_live() -> bool:
    """True when the bridge has a claude-agent-acp child, i.e. a session that can be compacted.

    After a bridge restart the child is spawned lazily by the first prompt. Sending
    /compact then would create a brand-new empty session and fail with
    "No messages to compact" (2026-09-20).
    """
    if IS_WINDOWS:
        return True
    try:
        result = subprocess.run(
            ["pgrep", "-f", "node_modules/.bin/claude-agent-acp"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("ACP liveness probe failed: %s", exc)
        return True
    return any(line.strip().isdigit() for line in result.stdout.splitlines())


def session_transcript_path(session_id: str) -> Path:
    return CLAUDE_PROJECT_DIR / f"{session_id}.jsonl"


def read_session_metrics(session_id: str, transcript: Path | None = None) -> dict:
    """Read token pressure and turn counts without reading message content."""
    path = transcript or session_transcript_path(session_id)
    metrics = {
        "session_id": session_id,
        "transcript": str(path),
        "transcript_bytes": 0,
        "records": 0,
        "user_turns": 0,
        "total_user_turns": 0,
        "compact_count": 0,
        "last_compact_at": "",
        "last_compact_post_tokens": 0,
        "context_tokens": 0,
        "context_window_tokens": CONTEXT_WINDOW_TOKENS,
        "pressure_ratio": 0.0,
        "model": "",
        "started_at": "",
        "last_record_at": "",
        "last_user_at": "",
        "last_work_at": "",
    }
    try:
        metrics["transcript_bytes"] = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for raw in handle:
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                metrics["records"] += 1
                timestamp = item.get("timestamp") or ""
                if timestamp and not metrics["started_at"]:
                    metrics["started_at"] = timestamp
                if timestamp:
                    metrics["last_record_at"] = timestamp

                if item.get("type") == "queue-operation" and item.get("operation") == "enqueue":
                    metrics["user_turns"] += 1
                    metrics["total_user_turns"] += 1
                    if timestamp:
                        metrics["last_user_at"] = timestamp
                        metrics["last_work_at"] = timestamp
                # Only real conversation work counts as activity; system or
                # bookkeeping records must not (last_record_at tracks those
                # for debugging only).
                elif item.get("type") == "assistant" and timestamp:
                    metrics["last_work_at"] = timestamp
                elif item.get("type") == "system" and item.get("subtype") == "compact_boundary":
                    compact_meta = item.get("compactMetadata") or {}
                    metrics["compact_count"] += 1
                    if timestamp:
                        metrics["last_compact_at"] = timestamp
                    post_tokens = int(compact_meta.get("postTokens") or 0)
                    metrics["last_compact_post_tokens"] = post_tokens
                    if post_tokens > 0:
                        # No usage record exists between the boundary and the
                        # next real turn, so without this the pre-compact
                        # number would keep reporting pressure that is gone.
                        metrics["context_tokens"] = post_tokens
                    # Pressure counts turns since the last compact, not
                    # lifetime; total_user_turns keeps the lifetime figure.
                    metrics["user_turns"] = 0

                message = item.get("message") or {}
                usage = message.get("usage") or {}
                context_tokens = sum(
                    int(usage.get(key) or 0)
                    for key in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
                # Synthetic bookkeeping messages report zero usage; ignore them.
                if context_tokens > 0:
                    metrics["context_tokens"] = context_tokens
                    model = message.get("model")
                    if model and model != "<synthetic>":
                        metrics["model"] = model
    except OSError:
        pass

    window = max(1, int(metrics["context_window_tokens"]))
    metrics["pressure_ratio"] = round(metrics["context_tokens"] / window, 4)
    return metrics


def current_session_metrics() -> dict:
    session_id = get_active_session_id()
    return read_session_metrics(session_id) if session_id else {}


def get_last_launchd_prompt_activity() -> float:
    """Fallback for platforms/runs where a Claude transcript is unavailable."""
    tail = read_log_tail(max_bytes=131_072)
    if "[acp] prompt:" not in tail:
        return 0.0
    try:
        return LAUNCHD_WEIXIN_OUT_LOG.stat().st_mtime
    except OSError:
        return 0.0


def load_runtime_env(path: Path = RUNTIME_ENV_FILE) -> dict:
    """Read the gitignored runtime override file on every call: launchctl
    kickstart does not reload a LaunchAgent's EnvironmentVariables, so this
    file is the only way to flip behavior without editing the plist."""
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def idle_use_last_record() -> bool:
    """Rollback switch only; the file being absent means the new behavior."""
    return load_runtime_env().get("SESSION_IDLE_USE_LAST_RECORD", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def compact_mode_enabled() -> bool:
    """Compact-in-place is the default; SESSION_COMPACT_MODE=0 in the runtime
    env file (or process env) restores kill-and-restart rotation."""
    value = load_runtime_env().get("SESSION_COMPACT_MODE", COMPACT_MODE_DEFAULT)
    return value.strip().lower() not in {"0", "false", "no", "off"}


def get_last_journal_prompt_activity(metrics: dict | None = None) -> float:
    if metrics:
        # last_record_at is deliberately excluded here: any transcript line
        # (system, attachment, bookkeeping) refreshes it, which kept idle
        # pinned near zero. It stays in metrics as a debug field only.
        candidates = [
            parse_timestamp(metrics.get("last_user_at")),
            parse_timestamp(metrics.get("last_work_at")),
        ]
        if idle_use_last_record():
            candidates.append(parse_timestamp(metrics.get("last_record_at")))
        transcript_ts = max(candidates)
        if transcript_ts:
            return transcript_ts
    if IS_WINDOWS:
        return 0.0
    if IS_MACOS:
        return get_last_launchd_prompt_activity()
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
    except (OSError, subprocess.SubprocessError):
        return 0.0
    latest = 0.0
    for line in result.stdout.splitlines():
        if "[acp] prompt:" not in line:
            continue
        try:
            latest = max(
                latest,
                datetime.strptime(line.split(maxsplit=1)[0], "%Y-%m-%dT%H:%M:%S%z").timestamp(),
            )
        except (ValueError, IndexError):
            continue
    return latest


def get_last_activity(state: dict | None = None, metrics: dict | None = None) -> float:
    state = load_state() if state is None else state
    candidates = [parse_timestamp(state.get("last_activity"))]
    candidates.append(get_last_journal_prompt_activity(metrics))
    return max(candidates)


def get_last_activity_marker(state: dict | None = None, metrics: dict | None = None) -> str:
    timestamp = get_last_activity(state, metrics)
    return iso_local(timestamp) if timestamp else "never"


def sync_runtime_state(state: dict, metrics: dict) -> dict:
    """Persist exact transcript activity and pressure metrics."""
    runtime_ts = get_last_journal_prompt_activity(metrics)
    state_ts = parse_timestamp(state.get("last_activity"))
    if runtime_ts > state_ts + 0.5:
        state["last_activity"] = iso_local(runtime_ts)
    if metrics:
        public_metrics = {key: value for key, value in metrics.items() if key != "transcript"}
        public_metrics["checked_at"] = iso_local()
        state["session_metrics"] = public_metrics
        pressure = session_pressure(metrics)
        state["rotation_pending"] = pressure["level"] != "normal"
        state["rotation_pending_reason"] = ", ".join(pressure["reasons"])
    save_state(state)
    return state


def session_pressure(metrics: dict) -> dict:
    ratio = float(metrics.get("pressure_ratio") or 0.0)
    turns = int(metrics.get("user_turns") or 0)
    reasons: list[str] = []
    level = "normal"
    if ratio >= HARD_CONTEXT_RATIO:
        reasons.append(f"context={ratio:.0%}>={HARD_CONTEXT_RATIO:.0%}")
        level = "hard"
    if turns >= HARD_TURN_LIMIT:
        reasons.append(f"turns={turns}>={HARD_TURN_LIMIT}")
        level = "hard"
    if level == "normal" and ratio >= SOFT_CONTEXT_RATIO:
        reasons.append(f"context={ratio:.0%}>={SOFT_CONTEXT_RATIO:.0%}")
        level = "soft"
    if level == "normal" and turns >= SOFT_TURN_LIMIT:
        reasons.append(f"turns={turns}>={SOFT_TURN_LIMIT}")
        level = "soft"
    return {"level": level, "reasons": reasons}


def has_held_lock(locks_dir: Path = LOCKS_DIR) -> bool:
    """True only while some process actually holds a lock under tmp/locks.

    A lock file merely existing is not a hold: flock dies with its holder, so
    a stale file left behind by a crashed job must never defer rotation.
    """
    if fcntl is None:
        return False
    for lock_path in sorted(locks_dir.glob("*.lock")):
        try:
            with lock_path.open("rb") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return True
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            continue
    return False


def has_active_background_task() -> bool:
    """True while a tracked background job should defer a soft rotation."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "[y]tdl.sh"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if any(line.strip().isdigit() for line in result.stdout.splitlines()):
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    return has_held_lock()


def evaluate_reset(state: dict, metrics: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    now_ts = now.timestamp()
    compact_mode = compact_mode_enabled()
    last_activity = get_last_activity(state, metrics)
    idle_minutes = (now_ts - last_activity) / 60 if last_activity else 999.0
    pressure = session_pressure(metrics) if metrics else {"level": "normal", "reasons": []}
    session_id = metrics.get("session_id", "") if metrics else ""
    session_started = parse_timestamp(metrics.get("started_at")) if metrics else 0.0
    session_age_minutes = (now_ts - session_started) / 60 if session_started else 999.0
    already_rotated = bool(session_id and state.get("last_rotated_session_id") == session_id)

    user_ts = parse_timestamp(metrics.get("last_user_at")) if metrics else 0.0
    work_ts = parse_timestamp(metrics.get("last_work_at")) if metrics else 0.0
    idle_from = "work_record" if work_ts > user_ts else "user"

    last_compact_ts = parse_timestamp(metrics.get("last_compact_at")) if metrics else 0.0
    minutes_since_compact = (now_ts - last_compact_ts) / 60 if last_compact_ts else 999999.0
    context_tokens = int(metrics.get("context_tokens") or 0) if metrics else 0
    if last_compact_ts:
        grown_tokens = max(0, context_tokens - int(metrics.get("last_compact_post_tokens") or 0))
    else:
        grown_tokens = context_tokens
    compact_allowed = not last_compact_ts or (
        minutes_since_compact >= COMPACT_MIN_INTERVAL_MINUTES
        and grown_tokens >= COMPACT_MIN_GROWTH_TOKENS
    )

    reason = None
    if not already_rotated and session_age_minutes >= MIN_PRESSURE_SESSION_AGE_MINUTES:
        if pressure["level"] == "hard" and idle_minutes >= HARD_PRESSURE_IDLE_MINUTES:
            reason = "context_pressure_hard"
        elif pressure["level"] == "soft" and idle_minutes >= SOFT_PRESSURE_IDLE_MINUTES:
            # Soft pressure only: a live background job (ytdl.sh, or a held
            # flock under tmp/locks) postpones rotation. Hard pressure never
            # waits on background work.
            if has_active_background_task():
                idle_from = "bgtask_hold"
            else:
                reason = "context_pressure"
    if compact_mode and reason is not None and not compact_allowed:
        # The last compact already absorbed this pressure (its summary alone
        # may clear the threshold); wait for real growth before compacting.
        reason = None

    if reason is None and not already_rotated and idle_minutes >= RECENT_ACTIVITY_MINUTES:
        today = now.date().isoformat()
        if (
            now.hour >= DAILY_RESET_HOUR
            and state.get("last_daily_reset_date") != today
            and state.get("last_daily_compact_attempt_date") != today
            and idle_minutes >= DAILY_RESET_IDLE_MINUTES
        ):
            reason = "daily_reset"
        else:
            # Measure the real session lifetime. A recent /compact or
            # checkpoint cannot reset the age of the underlying session.
            age_start = session_started or parse_timestamp(state.get("last_reset"))
            if age_start and (now_ts - age_start) / 3600 >= MAX_SESSION_AGE_HOURS:
                reason = "age_reset"
        if reason in {"daily_reset", "age_reset"} and has_active_background_task():
            reason = None
            idle_from = "bgtask_hold"

    return {
        "reason": reason,
        "idle_minutes": round(idle_minutes, 1),
        "idle_from": idle_from,
        "session_age_minutes": round(session_age_minutes, 1),
        "pressure": pressure,
        "already_rotated": already_rotated,
        "compact_mode": compact_mode,
        "compact_allowed": compact_allowed,
        "action": ("none" if reason is None else "rotate"
                   if reason in {"daily_reset", "age_reset"} or not compact_mode else "compact"),
        "grown_tokens_since_compact": grown_tokens,
        "minutes_since_compact": round(min(minutes_since_compact, 999999.0), 1),
    }


def find_weixin_pids() -> list[int]:
    pids: list[int] = []
    try:
        if IS_WINDOWS:
            result = subprocess.run(
                [
                    "powershell",
                    "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
                    "Where-Object { $_.CommandLine -like '*weixin-acp*' } | "
                    "Select-Object -ExpandProperty ProcessId",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        else:
            result = subprocess.run(
                ["pgrep", "-f", "weixin-acp"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        for line in result.stdout.splitlines():
            if line.strip().isdigit():
                pids.append(int(line.strip()))
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("Find PIDs failed: %s", exc)
    return pids


def kill_weixin() -> bool:
    pids = find_weixin_pids()
    if not pids:
        log.info("No weixin-acp process found to kill")
        return False
    killed = False
    for pid in pids:
        try:
            command = ["taskkill", "/PID", str(pid), "/T", "/F"] if IS_WINDOWS else ["kill", "-TERM", str(pid)]
            subprocess.run(command, capture_output=True, timeout=10, check=False)
            killed = True
            log.info("Rotated weixin-acp PID %s", pid)
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("Kill PID %s failed: %s", pid, exc)
    return killed


def log_restart(reason: str) -> None:
    try:
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        with RESTART_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {reason}\n")
        lines = RESTART_LOG.read_text(encoding="utf-8").splitlines()
        if len(lines) > 100:
            RESTART_LOG.write_text("\n".join(lines[-100:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def prepare_checkpoint(reason: str, metrics: dict, idle_minutes: float) -> Path | None:
    script = ROOT / "scripts" / "build-session-checkpoint.mjs"
    if not script.exists():
        log.error("Checkpoint builder is missing")
        return None
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    pending = TMP_DIR / f"session-checkpoint-pending-{os.getpid()}.json"
    command = [
        "/opt/homebrew/bin/node" if Path("/opt/homebrew/bin/node").exists() else "node",
        str(script),
        "--reason",
        reason,
        "--session-id",
        metrics.get("session_id", ""),
        "--context-tokens",
        str(metrics.get("context_tokens", 0)),
        "--context-window",
        str(metrics.get("context_window_tokens", CONTEXT_WINDOW_TOKENS)),
        "--turns",
        str(metrics.get("user_turns", 0)),
        "--idle-minutes",
        str(int(idle_minutes)),
        "--output",
        str(pending),
    ]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            log.error("Checkpoint builder failed: %s", (result.stderr or result.stdout)[-500:])
            pending.unlink(missing_ok=True)
            return None
        json.loads(pending.read_text(encoding="utf-8"))
        return pending
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.error("Checkpoint preparation failed: %s", exc)
        pending.unlink(missing_ok=True)
        return None


def commit_checkpoint(pending: Path) -> dict:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    os.replace(pending, CHECKPOINT_FILE)
    checkpoint = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    CHECKPOINT_ARCHIVE.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_id = re.sub(r"[^0-9a-f-]", "", checkpoint.get("session_id", ""))[:36] or "unknown"
    shutil.copy2(CHECKPOINT_FILE, CHECKPOINT_ARCHIVE / f"{timestamp}-{session_id}.json")
    archives = sorted(CHECKPOINT_ARCHIVE.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in archives[CHECKPOINT_ARCHIVE_LIMIT:]:
        stale.unlink(missing_ok=True)
    return checkpoint


def write_session_handoff(reason: str) -> None:
    script = ROOT / "scripts" / "write-session-handoff.mjs"
    if not script.exists():
        return
    node = "/opt/homebrew/bin/node" if Path("/opt/homebrew/bin/node").exists() else "node"
    try:
        subprocess.run(
            [node, str(script), "--reason", reason, "--source", "session-manager"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("Failed to write session handoff: %s", exc)


def start_memory_distill(reason: str) -> None:
    """Run durable-memory curation after rotation, never delaying restart."""
    script = ROOT / "scripts" / "memory-distill.sh"
    if not script.exists():
        return
    try:
        subprocess.Popen(
            ["bash", str(script), reason],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        log.error("Failed to start memory distill: %s", exc)


def rotate_session(state: dict, metrics: dict, decision: dict, *, dry_run: bool = False) -> bool:
    reason = decision.get("reason")
    if not reason:
        return False
    if is_acp_turn_busy():
        log.info("Rotation deferred: an ACP turn is still running")
        return False

    baseline_activity = get_last_activity(state, metrics)
    log.info(
        "Preparing %s rotation: session=%s context=%s/%s turns=%s idle=%smin",
        reason,
        metrics.get("session_id", "unknown"),
        metrics.get("context_tokens", 0),
        metrics.get("context_window_tokens", CONTEXT_WINDOW_TOKENS),
        metrics.get("user_turns", 0),
        decision.get("idle_minutes"),
    )
    pending = prepare_checkpoint(reason, metrics, decision.get("idle_minutes", 0))
    if pending is None:
        return False

    # The checkpoint model call takes a few seconds. If the user spoke during
    # that window, abandon this rotation rather than interrupting the new turn.
    latest_state = load_state()
    latest_metrics = current_session_metrics()
    latest_activity = get_last_activity(latest_state, latest_metrics)
    session_changed = bool(
        metrics.get("session_id")
        and latest_metrics.get("session_id")
        and metrics.get("session_id") != latest_metrics.get("session_id")
    )
    if is_acp_turn_busy() or latest_activity > baseline_activity + 0.5 or session_changed:
        pending.unlink(missing_ok=True)
        log.info("Rotation deferred: new activity arrived while checkpointing")
        return False

    if dry_run:
        pending.unlink(missing_ok=True)
        log.info("Dry-run: checkpoint validated; session was not rotated")
        return True

    checkpoint = commit_checkpoint(pending)
    handoff_reason = (
        f"{reason}, idle={int(decision.get('idle_minutes', 0))}min, "
        f"context={metrics.get('context_tokens', 0)}/{metrics.get('context_window_tokens', CONTEXT_WINDOW_TOKENS)}, "
        f"turns={metrics.get('user_turns', 0)}"
    )
    write_session_handoff(handoff_reason)
    log_restart(handoff_reason)

    if not kill_weixin():
        return False

    now = datetime.now()
    next_state = load_state()
    next_state.update(
        {
            "last_reset": now.isoformat(),
            "last_rotation_reason": reason,
            "last_rotated_session_id": metrics.get("session_id", ""),
            "last_activity_at_reset": iso_local(baseline_activity) if baseline_activity else "never",
            "rotation_pending": False,
            "rotation_pending_reason": "",
            "last_checkpoint_generated_at": checkpoint.get("generated_at"),
        }
    )
    if reason == "daily_reset":
        next_state["last_daily_reset_date"] = now.date().isoformat()
    save_state(next_state)

    if reason in {"daily_reset", "age_reset"}:
        start_memory_distill(reason)
    log.info("Session rotated; launchd will restart the live agent")
    return True


def send_compact_command(timeout: int = COMPACT_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Send /compact into the live session through the chat bridge socket."""
    request = json.dumps({"text": f"/compact {COMPACT_FOCUS}".strip()}, ensure_ascii=False)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(CHAT_SOCKET))
            client.sendall((request + "\n").encode("utf-8"))
            chunks: list[bytes] = []
            while True:
                try:
                    chunk = client.recv(65536)
                except socket.timeout:
                    return False, "compact timed out waiting for the bridge"
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError as exc:
        return False, f"bridge socket error: {exc}"
    raw = b"".join(chunks).decode("utf-8", errors="ignore").strip()
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        return False, f"bridge returned invalid JSON: {raw[:200]}"
    if payload.get("error"):
        detail = str(payload.get("detail") or "").strip()
        error = str(payload["error"])
        return False, f"{error}: {detail}" if detail and detail != error else error
    return True, str(payload.get("text") or "")


def compact_session(state: dict, metrics: dict, decision: dict, *, dry_run: bool = False) -> bool:
    """Compact the live session in place instead of rotating it.

    /compact keeps the session id, so conversation continuity no longer
    depends on the checkpoint/handoff bootstrap; those files are still
    written first as the archive of what the lossy summary may drop.
    """
    reason = decision.get("reason")
    if not reason:
        return False

    # Daily maintenance on a session that barely grew since the last compact:
    # mark the day done (memory distill still runs) without spending a compact
    # on an already-small context.
    if reason == "daily_reset" and not decision.get("compact_allowed", True):
        if dry_run:
            log.info("Dry-run: daily maintenance would be marked done without compacting")
            return True
        next_state = load_state()
        next_state["last_daily_reset_date"] = datetime.now().date().isoformat()
        save_state(next_state)
        start_memory_distill(reason)
        log.info("Daily maintenance: context barely grew since the last compact; skipped /compact")
        return True

    if is_acp_turn_busy():
        log.info("Compact deferred: an ACP turn is still running")
        return False

    if not is_acp_session_live():
        session_id = str(metrics.get("session_id") or "")
        if session_id not in _DEAD_SESSION_SKIPS_LOGGED:
            _DEAD_SESSION_SKIPS_LOGGED.add(session_id)
            log.info("Compact skipped: no live ACP session; /compact would only open an empty one (session=%s)", session_id or "unknown")
        return False

    before_compact_count = int(metrics.get("compact_count") or 0)
    baseline_activity = get_last_activity(state, metrics)
    log.info(
        "Preparing %s compact: session=%s context=%s/%s turns=%s idle=%smin",
        reason,
        metrics.get("session_id", "unknown"),
        metrics.get("context_tokens", 0),
        metrics.get("context_window_tokens", CONTEXT_WINDOW_TOKENS),
        metrics.get("user_turns", 0),
        decision.get("idle_minutes"),
    )
    pending = prepare_checkpoint(reason, metrics, decision.get("idle_minutes", 0))
    if pending is None:
        return False

    # Same guard as rotation: if the user spoke while the checkpoint model
    # call ran, do not wedge a minutes-long compact in front of their turn.
    latest_state = load_state()
    latest_metrics = current_session_metrics()
    latest_activity = get_last_activity(latest_state, latest_metrics)
    session_changed = bool(
        metrics.get("session_id")
        and latest_metrics.get("session_id")
        and metrics.get("session_id") != latest_metrics.get("session_id")
    )
    if is_acp_turn_busy() or latest_activity > baseline_activity + 0.5 or session_changed:
        pending.unlink(missing_ok=True)
        log.info("Compact deferred: new activity arrived while checkpointing")
        return False

    if dry_run:
        pending.unlink(missing_ok=True)
        log.info("Dry-run: checkpoint validated; /compact was not sent")
        return True

    checkpoint = commit_checkpoint(pending)
    audit = (
        f"compact:{reason}, idle={int(decision.get('idle_minutes', 0))}min, "
        f"context={metrics.get('context_tokens', 0)}/{metrics.get('context_window_tokens', CONTEXT_WINDOW_TOKENS)}, "
        f"turns={metrics.get('user_turns', 0)}"
    )
    write_session_handoff(audit)  # keeps the reboot-recovery handoff fresh too
    log_restart(audit)

    if reason == "daily_reset":
        # A failed/no-op daily compact must not re-enter the conversation every
        # two hours. Real context pressure still triggers independently.
        attempt_state = load_state()
        attempt_state["last_daily_compact_attempt_date"] = datetime.now().date().isoformat()
        save_state(attempt_state)
    ok, response = send_compact_command()

    # The boundary record lands when the bridge answers, but give the
    # transcript writer a moment before declaring failure.
    compacted = False
    after_metrics: dict = {}
    for _ in range(3):
        after_metrics = current_session_metrics()
        if int(after_metrics.get("compact_count") or 0) > before_compact_count:
            compacted = True
            break
        time.sleep(5)

    now = datetime.now()
    next_state = load_state()
    if compacted:
        next_state.update(
            {
                "last_reset": now.isoformat(),
                "last_compact_at": now.isoformat(timespec="seconds"),
                "last_compact_reason": reason,
                "compact_fail_count": 0,
                "rotation_pending": False,
                "rotation_pending_reason": "",
                "last_checkpoint_generated_at": checkpoint.get("generated_at"),
            }
        )
        if reason == "daily_reset":
            next_state["last_daily_reset_date"] = now.date().isoformat()
        save_state(next_state)
        if reason == "daily_reset":
            start_memory_distill(reason)
        log.info(
            "Session compacted in place: %s -> %s tokens (session %s kept)",
            metrics.get("context_tokens", 0),
            after_metrics.get("last_compact_post_tokens", 0),
            metrics.get("session_id", "unknown"),
        )
        return True

    if "Not enough messages" in response or "No messages to compact" in response:
        # Nothing worth compacting is not a failure; mark daily done so the
        # check does not re-fire all morning.
        if reason == "daily_reset":
            next_state["last_daily_reset_date"] = now.date().isoformat()
            save_state(next_state)
            start_memory_distill(reason)
        log.info("Compact skipped: %s", response.strip()[:120])
        return True

    failures = int(next_state.get("compact_fail_count") or 0) + 1
    next_state["compact_fail_count"] = failures
    save_state(next_state)
    detail = response if not ok else "bridge replied but no compact boundary appeared in the transcript"
    log.error("Compact failed (consecutive=%s): %s", failures, detail[:300])
    if (
        decision.get("pressure", {}).get("level") == "hard"
        and failures >= COMPACT_FAIL_ROTATE_THRESHOLD
    ):
        log.warning("Falling back to rotation after %s failed compacts under hard pressure", failures)
        return rotate_session(load_state(), current_session_metrics(), decision, dry_run=False)
    return False


def status_payload() -> dict:
    state = load_state()
    metrics = current_session_metrics()
    decision = evaluate_reset(state, metrics)
    return {
        "ok": True,
        "gateway_enabled": False,
        "compact_mode": decision.get("compact_mode", False),
        "idle_from": decision.get("idle_from"),
        "busy": is_acp_turn_busy(),
        "metrics": {key: value for key, value in metrics.items() if key != "transcript"},
        "pressure": session_pressure(metrics) if metrics else {"level": "unknown", "reasons": []},
        "decision": decision,
        "config": {
            "context_window_tokens": CONTEXT_WINDOW_TOKENS,
            "soft_context_ratio": SOFT_CONTEXT_RATIO,
            "hard_context_ratio": HARD_CONTEXT_RATIO,
            "soft_turn_limit": SOFT_TURN_LIMIT,
            "hard_turn_limit": HARD_TURN_LIMIT,
            "soft_pressure_idle_minutes": SOFT_PRESSURE_IDLE_MINUTES,
            "hard_pressure_idle_minutes": HARD_PRESSURE_IDLE_MINUTES,
            "compact_min_interval_minutes": COMPACT_MIN_INTERVAL_MINUTES,
            "compact_min_growth_tokens": COMPACT_MIN_GROWTH_TOKENS,
            "compact_fail_rotate_threshold": COMPACT_FAIL_ROTATE_THRESHOLD,
        },
    }


def daemon(*, once: bool = False, dry_run: bool = False) -> int:
    log.info(
        "Session Manager 2.0 started (platform=%s, soft=%s%%, hard=%s%%)",
        platform.system(),
        int(SOFT_CONTEXT_RATIO * 100),
        int(HARD_CONTEXT_RATIO * 100),
    )
    state = load_state()
    if not state.get("last_reset"):
        state["last_reset"] = datetime.now().isoformat()
        save_state(state)

    while True:
        try:
            state = load_state()
            metrics = current_session_metrics()
            state = sync_runtime_state(state, metrics)
            decision = evaluate_reset(state, metrics)
            if decision.get("reason"):
                if decision.get("action") == "compact":
                    compact_session(state, metrics, decision, dry_run=dry_run)
                else:
                    rotate_session(state, metrics, decision, dry_run=dry_run)
        except Exception as exc:  # keep the supervisor alive on malformed runtime state
            log.exception("Session manager iteration failed: %s", exc)
        if once:
            return 0
        time.sleep(CHECK_INTERVAL)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the live WeChat Claude session lifecycle")
    subparsers = parser.add_subparsers(dest="command")
    status_parser = subparsers.add_parser("status", help="show current token pressure and rotation state")
    status_parser.add_argument("--json", action="store_true")
    check_parser = subparsers.add_parser("check", help="run one lifecycle check")
    check_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "status":
        payload = status_payload()
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            metrics = payload["metrics"]
            print(
                f"session={metrics.get('session_id', 'unknown')} "
                f"mode={'compact' if payload['compact_mode'] else 'rotate'} "
                f"context={metrics.get('context_tokens', 0)}/{metrics.get('context_window_tokens', CONTEXT_WINDOW_TOKENS)} "
                f"pressure={payload['pressure']['level']} turns={metrics.get('user_turns', 0)} "
                f"compacts={metrics.get('compact_count', 0)} "
                f"busy={payload['busy']} decision={payload['decision'].get('reason') or 'none'}"
            )
        return 0
    if args.command == "check":
        return daemon(once=True, dry_run=args.dry_run)
    return daemon()


if __name__ == "__main__":
    sys.exit(main())
