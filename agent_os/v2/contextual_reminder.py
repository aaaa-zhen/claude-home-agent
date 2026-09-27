"""Deterministic follow-up for contextual reminder intents.

The normal brain can handle explicit reminders, but short messages like
"你提醒我" or "English with Esma" after a reminder clarification need to bind
to the immediately previous schedule/event. This module keeps that path boring
and verifiable: read recent context, extract an event time, install one macOS
cron reminder.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MEM = Path(os.getenv("V2_MEMORY_DIR", str(ROOT / "memory")))
NODE = os.getenv("NODE_BIN", "/opt/homebrew/bin/node")
if not Path(NODE).exists():
    NODE = "node"

BARE_REMINDER_RE = re.compile(
    r"^\s*(?:你|帮我|麻烦你|到时候)?\s*"
    r"(?:提醒我|提醒一下我|提醒我一下|叫我|喊我|记得提醒我|别让我忘了)"
    r"\s*(?:吧|啊|呀|哈|哦|一下)?\s*$"
)
REMINDER_CLARIFICATION_RE = re.compile(
    r"(提醒你啥|提醒啥|提醒什么|要提醒啥事|几点提醒|什么时候提醒|我给你设)"
)
DATE_RE = re.compile(r"(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日?")
TIME_RANGE_RE = re.compile(
    r"(?P<h1>\d{1,2})(?:[:：](?P<m1>\d{2}))?\s*(?P<a1>[AaPp]\.?\s?[Mm]\.?)?\s*"
    r"(?:-|–|—|~|～|到|至)\s*"
    r"(?P<h2>\d{1,2})(?:[:：](?P<m2>\d{2}))?\s*(?P<a2>[AaPp]\.?\s?[Mm]\.?)?"
)
TIME_SINGLE_RE = re.compile(
    r"(?P<h>\d{1,2})(?:[:：](?P<m>\d{2}))\s*(?P<a>[AaPp]\.?\s?[Mm]\.?)?"
)


@dataclass(frozen=True)
class ReminderEvent:
    title: str
    start_at: datetime
    source: str


def is_bare_reminder(message: str) -> bool:
    return bool(BARE_REMINDER_RE.match(message or ""))


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


def _recent_asked_reminder_clarification(recent: str, limit_chars: int = 900) -> bool:
    return bool(REMINDER_CLARIFICATION_RE.search((recent or "")[-limit_chars:]))


def _message_points_to_event(message: str, event: ReminderEvent) -> bool:
    msg = _norm(message)
    title = _norm(event.title)
    if len(msg) < 4 or not title:
        return False
    if msg in title or title in msg:
        return True
    words = [w for w in re.findall(r"[A-Za-z0-9]{3,}", event.title.lower()) if w not in {"with", "the"}]
    return bool(words) and all(w in msg for w in words)


def _recent_context_tail(limit_lines: int = 80) -> str:
    path = MEM / "recent-context.md"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return ""
    return "\n".join(lines[-limit_lines:])


def _recent_messages_text(store: Any, session_id: str, limit: int = 12) -> str:
    try:
        rows = store.recent_messages(session_id, limit)
    except Exception:
        return ""
    lines: list[str] = []
    for row in rows:
        role = row.get("role", "")
        content = str(row.get("content", "")).strip()
        if content:
            lines.append(f"[{role}] {content}")
    return "\n".join(lines)


def _ampm(value: str | None) -> str | None:
    if not value:
        return None
    v = value.lower().replace(".", "").replace(" ", "")
    if v in {"am", "pm"}:
        return v
    return None


def _hour24(hour: int, ampm: str | None) -> int:
    if ampm == "am":
        return 0 if hour == 12 else hour
    if ampm == "pm":
        return hour if hour == 12 else hour + 12
    return hour


def _event_datetime(month: int, day: int, hour: int, minute: int, now: datetime) -> datetime | None:
    try:
        start = now.replace(month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    if start < now - timedelta(days=2):
        try:
            start = start.replace(year=start.year + 1)
        except ValueError:
            return None
    return start


def _extract_title(window: str) -> str:
    preply = re.search(r"\bEnglish\s+with\s+[A-Za-z][A-Za-z .'-]{0,40}", window, re.I)
    if preply:
        return re.sub(r"\s+", " ", preply.group(0)).strip()
    for line in reversed(window.splitlines()):
        cleaned = re.sub(r"^\[[^\]]+\]\s*(?:\[[^\]]+\]\s*)?", "", line).strip()
        if not cleaned:
            continue
        if "时间" in cleaned or DATE_RE.search(cleaned) or TIME_RANGE_RE.search(cleaned):
            cleaned = re.sub(r".*?(你约好了[:：]?)", "", cleaned).strip()
            return cleaned[:40] or "这件事"
    return "这件事"


def extract_event(text: str, *, now: datetime | None = None) -> ReminderEvent | None:
    """Extract the latest dated event from recent context."""
    now = now or datetime.now()
    matches = list(DATE_RE.finditer(text or ""))
    for date_match in reversed(matches):
        start_idx = max(0, date_match.start() - 260)
        end_idx = min(len(text), date_match.end() + 260)
        window = text[start_idx:end_idx]
        search_area = text[date_match.end():end_idx]
        month = int(date_match.group("month"))
        day = int(date_match.group("day"))
        for time_match in [*TIME_RANGE_RE.finditer(search_area), *TIME_SINGLE_RE.finditer(search_area)]:
            if "h1" in time_match.groupdict():
                hour = int(time_match.group("h1"))
                minute = int(time_match.group("m1") or "0")
                end_hour = int(time_match.group("h2"))
                end_minute = int(time_match.group("m2") or "0")
                if end_hour > 23 or end_minute > 59:
                    continue
                marker = _ampm(time_match.group("a1")) or _ampm(time_match.group("a2"))
            else:
                hour = int(time_match.group("h"))
                minute = int(time_match.group("m") or "0")
                marker = _ampm(time_match.group("a"))
            hour = _hour24(hour, marker)
            if hour > 23 or minute > 59:
                continue

            start_at = _event_datetime(month, day, hour, minute, now)
            if not start_at:
                continue
            return ReminderEvent(title=_extract_title(window), start_at=start_at, source=window.strip())
    return None


def _cron_line(*, reminder_id: str, remind_at: datetime, message: str) -> str:
    script = ROOT / "scripts" / "send-once-reminder.mjs"
    log_path = ROOT / "tmp" / "reminders.log"
    return (
        f"{remind_at.minute} {remind_at.hour} {remind_at.day} {remind_at.month} * "
        f"cd {shlex.quote(str(ROOT))} && {shlex.quote(NODE)} {shlex.quote(str(script))} "
        f"--id {reminder_id} --message {shlex.quote(message)} >> {shlex.quote(str(log_path))} 2>&1"
    )


def install_reminder(event: ReminderEvent, *, lead_minutes: int = 30) -> dict[str, Any]:
    remind_at = event.start_at - timedelta(minutes=lead_minutes)
    now = datetime.now()
    if remind_at <= now < event.start_at:
        remind_at = now + timedelta(minutes=1)
    if event.start_at <= now:
        return {"ok": False, "error": "event already passed"}

    digest = hashlib.sha1(f"{event.title}|{event.start_at.isoformat()}".encode("utf-8")).hexdigest()[:8]
    reminder_id = f"ctxrem-{event.start_at.strftime('%Y%m%d%H%M')}-{digest}"
    msg = f"提醒：{event.title} {event.start_at.strftime('%m月%d日 %H:%M')} 开始。"
    line = _cron_line(reminder_id=reminder_id, remind_at=remind_at, message=msg)

    if os.getenv("V2_CONTEXTUAL_REMINDER_DRY_RUN") == "1":
        return {"ok": True, "id": reminder_id, "line": line, "dry_run": True, "remind_at": remind_at}

    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False).stdout
    lines = [ln for ln in current.splitlines() if reminder_id not in ln]
    lines.append(line)
    updated = "\n".join(ln for ln in lines if ln.strip()) + "\n"
    subprocess.run(["crontab", "-"], input=updated, text=True, check=True)
    verify = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False).stdout
    if reminder_id not in verify:
        return {"ok": False, "error": "cron verify failed", "id": reminder_id}
    return {"ok": True, "id": reminder_id, "line": line, "remind_at": remind_at}


def try_handle(message: str, *, store: Any, session_id: str) -> dict[str, Any] | None:
    bare_reminder = is_bare_reminder(message)
    recent = "\n".join(
        part for part in [
            _recent_context_tail(),
            _recent_messages_text(store, session_id),
        ] if part.strip()
    )
    event = extract_event(recent)
    if not event:
        return None
    if not bare_reminder:
        if not (
            _recent_asked_reminder_clarification(recent)
            and _message_points_to_event(message, event)
        ):
            return None
    result = install_reminder(event)
    if not result.get("ok"):
        return None
    remind_at = result["remind_at"]
    return {
        "ok": True,
        "mode": "instant",
        "reply": (
            f"好，我给你设好了：{event.title}，"
            f"{event.start_at.strftime('%m月%d日 %H:%M')} 开始，"
            f"我会提前 30 分钟提醒你。"
        ),
        "task_id": None,
        "reminder_id": result.get("id"),
        "remind_at": remind_at.isoformat(),
    }
