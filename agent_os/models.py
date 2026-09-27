from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_BLOCKED = "blocked"
TASK_STATUS_DONE = "done"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"

RUN_STATUS_CREATED = "created"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUS_ARCHIVED = "archived"

FOLDER_STATE_CREATED = "created"
FOLDER_STATE_PLANNED = "planned"
FOLDER_STATE_RUNNING = "running"
FOLDER_STATE_BLOCKED = "blocked"
FOLDER_STATE_SUSPENDED = "suspended"
FOLDER_STATE_COMPLETED = "completed"
FOLDER_STATE_COMPRESSED = "compressed"
FOLDER_STATE_ARCHIVED = "archived"

RESPONSE_STATUS_CREATED = "created"
RESPONSE_STATUS_SENT = "sent"

WORKER_CONTROL = "control"
WORKER_AGENT = "agent"
WORKER_JOB = "job"


PRIORITY_URGENT = 100
PRIORITY_HOME_CONTROL = 90
PRIORITY_HOME_STATUS = 80
PRIORITY_SHORT_QA = 60
PRIORITY_FILE_MEDIA = 40
PRIORITY_LONG_JOB = 20
PRIORITY_BACKGROUND = 10


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Classification:
    intent: str
    worker_type: str
    priority: int
    preferred_role: str = "general"
    target_agent_id: str | None = None


@dataclass(frozen=True)
class ClaimedTask:
    task_id: str
    folder_id: str
    run_id: str
    goal: str
    user_id: str
    channel: str
    intent: str
    worker_type: str
    preferred_role: str
    target_agent_id: str | None
    assigned_agent_id: str | None
    assigned_agent_role: str | None
    assigned_backend: str | None
    assigned_session_id: str | None
    priority: int
    context: dict[str, Any]
