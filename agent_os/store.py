from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .models import (
    ClaimedTask,
    FOLDER_STATE_BLOCKED,
    FOLDER_STATE_COMPLETED,
    FOLDER_STATE_CREATED,
    FOLDER_STATE_PLANNED,
    FOLDER_STATE_RUNNING,
    FOLDER_STATE_SUSPENDED,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_CREATED,
    RUN_STATUS_RUNNING,
    RESPONSE_STATUS_CREATED,
    TASK_STATUS_DONE,
    TASK_STATUS_FAILED,
    TASK_STATUS_CANCELLED,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    utc_now,
)


class AgentOSStore:
    """SQLite-backed local event bus and task/run store.

    The API is intentionally event-bus shaped so a Redis Streams or NATS adapter
    can replace this class without changing workers.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                  event_id TEXT PRIMARY KEY,
                  type TEXT NOT NULL,
                  task_id TEXT,
                  folder_id TEXT,
                  run_id TEXT,
                  priority INTEGER NOT NULL DEFAULT 50,
                  source TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                  task_id TEXT PRIMARY KEY,
                  folder_id TEXT,
                  user_id TEXT NOT NULL,
                  channel TEXT NOT NULL,
                  goal TEXT NOT NULL,
                  intent TEXT NOT NULL,
                  worker_type TEXT NOT NULL,
                  preferred_role TEXT,
                  target_agent_id TEXT,
                  assigned_agent_id TEXT,
                  assigned_agent_role TEXT,
                  assigned_backend TEXT,
                  assigned_session_id TEXT,
                  priority INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  context_json TEXT NOT NULL,
                  result_text TEXT,
                  error_text TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  started_at TEXT,
                  completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS agents (
                  agent_id TEXT PRIMARY KEY,
                  role TEXT NOT NULL,
                  backend TEXT NOT NULL,
                  display_name TEXT NOT NULL,
                  description TEXT NOT NULL,
                  tools_json TEXT NOT NULL,
                  max_concurrency INTEGER NOT NULL DEFAULT 1,
                  enabled INTEGER NOT NULL DEFAULT 1,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                  run_id TEXT PRIMARY KEY,
                  folder_id TEXT,
                  task_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  worker_type TEXT NOT NULL,
                  context_json TEXT NOT NULL,
                  steps_json TEXT NOT NULL,
                  artifacts_json TEXT NOT NULL,
                  memory_buffer_json TEXT NOT NULL,
                  summary TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS folders (
                  folder_id TEXT PRIMARY KEY,
                  task_id TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  user_id TEXT NOT NULL,
                  channel TEXT NOT NULL,
                  goal TEXT NOT NULL,
                  state TEXT NOT NULL,
                  context_snapshot_json TEXT NOT NULL,
                  memory_buffer_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  completed_at TEXT,
                  archived_at TEXT
                );

                CREATE TABLE IF NOT EXISTS responses (
                  response_id TEXT PRIMARY KEY,
                  task_id TEXT NOT NULL,
                  folder_id TEXT,
                  run_id TEXT,
                  channel TEXT NOT NULL,
                  target_user_id TEXT NOT NULL,
                  text TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  sent_at TEXT
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                  tool_call_id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL,
                  task_id TEXT NOT NULL,
                  tool TEXT NOT NULL,
                  args_json TEXT NOT NULL,
                  timeout_ms INTEGER,
                  retry INTEGER NOT NULL DEFAULT 0,
                  idempotent INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL,
                  result_json TEXT,
                  error_text TEXT,
                  created_at TEXT NOT NULL,
                  completed_at TEXT
                );

                """
            )
            self._ensure_column(conn, "events", "folder_id", "TEXT")
            self._ensure_column(conn, "tasks", "folder_id", "TEXT")
            self._ensure_column(conn, "tasks", "claimed_by_worker_id", "TEXT")
            self._ensure_column(conn, "tasks", "claimed_by", "TEXT")
            self._ensure_column(conn, "tasks", "preferred_role", "TEXT")
            self._ensure_column(conn, "tasks", "target_agent_id", "TEXT")
            self._ensure_column(conn, "tasks", "assigned_agent_id", "TEXT")
            self._ensure_column(conn, "tasks", "assigned_agent_role", "TEXT")
            self._ensure_column(conn, "tasks", "assigned_backend", "TEXT")
            self._ensure_column(conn, "tasks", "assigned_session_id", "TEXT")
            self._ensure_column(conn, "runs", "folder_id", "TEXT")
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_folder ON events(folder_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_queue
                  ON tasks(status, worker_type, priority DESC, created_at ASC);
                CREATE INDEX IF NOT EXISTS idx_tasks_claimed_by
                  ON tasks(claimed_by_worker_id, started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tasks_assigned_agent
                  ON tasks(assigned_agent_id, status, created_at);
                CREATE INDEX IF NOT EXISTS idx_agents_role
                  ON agents(role, enabled);
                CREATE INDEX IF NOT EXISTS idx_folders_state ON folders(state, updated_at);
                CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
                CREATE INDEX IF NOT EXISTS idx_responses_task ON responses(task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_responses_status ON responses(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id, created_at);
                """
            )
            self._ensure_default_agents(conn)

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def _ensure_default_agents(self, conn: sqlite3.Connection) -> None:
        now = utc_now()
        defaults = [
            ("codex-general", "general", "codex", "Codex General", "通用微信助手，负责普通问答和兜底。", ["weather", "file", "ha", "web", "shell"], 10),
            ("codex-home", "home", "codex", "Codex Home", "家居控制和状态查询。", ["ha", "shell"], 5),
            ("codex-weather", "weather", "codex", "Codex Weather", "天气和实时查询。", ["weather", "web", "shell"], 5),
            ("codex-file", "file", "codex", "Codex File", "本地文件、图片、视频、附件定位和发送。", ["file", "media", "shell"], 5),
            ("codex-browser", "browser", "codex", "Codex Browser", "网页代办和已登录浏览器环境操作。", ["browser", "playwright", "shell"], 5),
            ("codex-research", "research", "codex", "Codex Research", "长文、论文、报告、调研和持续产出。", ["web", "file", "shell"], 3),
            ("codex-coder", "coder", "codex", "Codex Coder", "代码、项目、网页和自动化开发。", ["file", "shell", "web"], 5),
            ("claude-ui", "claude_ui", "claude", "Claude UI", "显式要求 Claude Code 时用于 UI/网页/视觉打磨。", ["file", "shell", "web"], 2),
        ]
        for agent_id, role, backend, display_name, description, tools, max_concurrency in defaults:
            conn.execute(
                """
                INSERT OR IGNORE INTO agents(
                  agent_id, role, backend, display_name, description, tools_json,
                  max_concurrency, enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    agent_id,
                    role,
                    backend,
                    display_name,
                    description,
                    json.dumps(tools, ensure_ascii=False),
                    max_concurrency,
                    now,
                    now,
                ),
            )

    def _select_agent(
        self,
        conn: sqlite3.Connection,
        *,
        preferred_role: str,
        target_agent_id: str | None,
    ) -> sqlite3.Row | None:
        if target_agent_id:
            row = conn.execute(
                "SELECT * FROM agents WHERE agent_id = ? AND enabled = 1",
                (target_agent_id,),
            ).fetchone()
            if row:
                return row

        role = preferred_role or "general"
        row = conn.execute(
            """
            SELECT a.*,
              (
                SELECT COUNT(*)
                FROM tasks t
                WHERE t.assigned_agent_id = a.agent_id
                  AND t.status = ?
              ) AS running_count
            FROM agents a
            WHERE a.role = ? AND a.enabled = 1
            ORDER BY running_count ASC, a.agent_id ASC
            LIMIT 1
            """,
            (TASK_STATUS_RUNNING, role),
        ).fetchone()
        if row:
            return row

        return conn.execute(
            "SELECT * FROM agents WHERE agent_id = 'codex-general' AND enabled = 1",
        ).fetchone()

    def emit_event(
        self,
        event_type: str,
        *,
        task_id: str | None = None,
        folder_id: str | None = None,
        run_id: str | None = None,
        priority: int = 50,
        source: str = "agent_os",
        payload: dict[str, Any] | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        event_id = f"e_{uuid.uuid4().hex}"
        close = conn is None
        if conn is None:
            conn = self.connect()
        try:
            conn.execute(
                """
                INSERT INTO events(event_id, type, task_id, folder_id, run_id, priority, source, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    task_id,
                    folder_id,
                    run_id,
                    priority,
                    source,
                    json.dumps(payload or {}, ensure_ascii=False),
                    utc_now(),
                ),
            )
            return event_id
        finally:
            if close:
                conn.close()

    def create_task(
        self,
        *,
        user_id: str,
        channel: str,
        goal: str,
        intent: str,
        worker_type: str,
        priority: int,
        preferred_role: str = "general",
        target_agent_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        task_id = f"t_{uuid.uuid4().hex}"
        folder_id = f"f_{uuid.uuid4().hex}"
        run_id = f"r_{uuid.uuid4().hex}"
        now = utc_now()
        context_json = json.dumps(context or {}, ensure_ascii=False)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            assigned_agent = self._select_agent(conn, preferred_role=preferred_role, target_agent_id=target_agent_id)
            assigned_agent_id = assigned_agent["agent_id"] if assigned_agent else None
            assigned_agent_role = assigned_agent["role"] if assigned_agent else preferred_role
            assigned_backend = assigned_agent["backend"] if assigned_agent else "codex"
            assigned_session_id = f"sess_{assigned_agent_id}_{folder_id}" if assigned_agent_id else None
            self.emit_event(
                "user.message",
                task_id=task_id,
                folder_id=folder_id,
                run_id=run_id,
                priority=priority,
                source=channel,
                payload={"user_id": user_id, "text": goal, "context": context or {}},
                conn=conn,
            )
            conn.execute(
                """
                INSERT INTO folders(
                  folder_id, task_id, run_id, user_id, channel, goal, state,
                  context_snapshot_json, memory_buffer_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)
                """,
                (
                    folder_id,
                    task_id,
                    run_id,
                    user_id,
                    channel,
                    goal,
                    FOLDER_STATE_CREATED,
                    context_json,
                    now,
                    now,
                ),
            )
            self.emit_event(
                "folder.created",
                task_id=task_id,
                folder_id=folder_id,
                run_id=run_id,
                priority=priority,
                source="scheduler",
                payload={"state": FOLDER_STATE_CREATED, "goal": goal},
                conn=conn,
            )
            conn.execute(
                "UPDATE folders SET state = ?, updated_at = ? WHERE folder_id = ?",
                (FOLDER_STATE_PLANNED, now, folder_id),
            )
            self.emit_event(
                "folder.planned",
                task_id=task_id,
                folder_id=folder_id,
                run_id=run_id,
                priority=priority,
                source="scheduler",
                payload={
                    "state": FOLDER_STATE_PLANNED,
                    "worker_type": worker_type,
                    "preferred_role": preferred_role,
                    "assigned_agent_id": assigned_agent_id,
                    "assigned_agent_role": assigned_agent_role,
                    "assigned_backend": assigned_backend,
                    "assigned_session_id": assigned_session_id,
                },
                conn=conn,
            )
            conn.execute(
                """
                INSERT INTO tasks(
                  task_id, folder_id, user_id, channel, goal, intent, worker_type,
                  preferred_role, target_agent_id, assigned_agent_id, assigned_agent_role,
                  assigned_backend, assigned_session_id, priority, status,
                  run_id, context_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    folder_id,
                    user_id,
                    channel,
                    goal,
                    intent,
                    worker_type,
                    preferred_role,
                    target_agent_id,
                    assigned_agent_id,
                    assigned_agent_role,
                    assigned_backend,
                    assigned_session_id,
                    priority,
                    TASK_STATUS_QUEUED,
                    run_id,
                    context_json,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO runs(
                  run_id, folder_id, task_id, status, worker_type, context_json, steps_json,
                  artifacts_json, memory_buffer_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, '[]', '[]', '[]', ?, ?)
                """,
                (run_id, folder_id, task_id, RUN_STATUS_CREATED, worker_type, context_json, now, now),
            )
            self.emit_event(
                "task.created",
                task_id=task_id,
                folder_id=folder_id,
                run_id=run_id,
                priority=priority,
                source=channel,
                payload={
                    "goal": goal,
                    "intent": intent,
                    "worker_type": worker_type,
                    "preferred_role": preferred_role,
                    "target_agent_id": target_agent_id,
                    "assigned_agent_id": assigned_agent_id,
                    "assigned_agent_role": assigned_agent_role,
                    "assigned_backend": assigned_backend,
                    "assigned_session_id": assigned_session_id,
                },
                conn=conn,
            )
            self.emit_event(
                "task.assigned",
                task_id=task_id,
                folder_id=folder_id,
                run_id=run_id,
                priority=priority,
                source="scheduler",
                payload={
                    "preferred_role": preferred_role,
                    "target_agent_id": target_agent_id,
                    "assigned_agent_id": assigned_agent_id,
                    "assigned_agent_role": assigned_agent_role,
                    "assigned_backend": assigned_backend,
                    "assigned_session_id": assigned_session_id,
                },
                conn=conn,
            )
            self.emit_event(
                "task.queued",
                task_id=task_id,
                folder_id=folder_id,
                run_id=run_id,
                priority=priority,
                source="scheduler",
                payload={"status": TASK_STATUS_QUEUED},
                conn=conn,
            )
            conn.execute("COMMIT")
        return {"task_id": task_id, "folder_id": folder_id, "run_id": run_id}

    def claim_next(self, worker_type: str, worker_id: str) -> ClaimedTask | None:
        return self._claim_next(worker_id=worker_id, worker_types=[worker_type], claimed_by=worker_type)

    def claim_next_any(self, worker_id: str, worker_types: list[str] | None = None) -> ClaimedTask | None:
        return self._claim_next(worker_id=worker_id, worker_types=worker_types or [], claimed_by="agent-pool")

    def _claim_next(self, *, worker_id: str, worker_types: list[str], claimed_by: str) -> ClaimedTask | None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            params: list[Any] = [TASK_STATUS_QUEUED]
            worker_filter = ""
            if worker_types:
                placeholders = ", ".join("?" for _ in worker_types)
                worker_filter = f"AND worker_type IN ({placeholders})"
                params.extend(worker_types)
            row = conn.execute(
                f"""
                SELECT * FROM tasks
                WHERE status = ? {worker_filter}
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if not row:
                conn.execute("COMMIT")
                return None
            now = utc_now()
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, started_at = COALESCE(started_at, ?), updated_at = ?,
                    claimed_by_worker_id = ?, claimed_by = ?
                WHERE task_id = ? AND status = ?
                """,
                (TASK_STATUS_RUNNING, now, now, worker_id, claimed_by, row["task_id"], TASK_STATUS_QUEUED),
            )
            conn.execute(
                """
                UPDATE runs
                SET status = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (RUN_STATUS_RUNNING, now, row["run_id"]),
            )
            if row["folder_id"]:
                conn.execute(
                    "UPDATE folders SET state = ?, updated_at = ? WHERE folder_id = ?",
                    (FOLDER_STATE_RUNNING, now, row["folder_id"]),
                )
                self.emit_event(
                    "folder.running",
                    task_id=row["task_id"],
                    folder_id=row["folder_id"],
                    run_id=row["run_id"],
                    priority=row["priority"],
                    source=worker_id,
                    payload={
                        "state": FOLDER_STATE_RUNNING,
                        "worker_type": row["worker_type"],
                        "claimed_by": claimed_by,
                        "assigned_agent_id": row["assigned_agent_id"],
                        "assigned_agent_role": row["assigned_agent_role"],
                        "assigned_backend": row["assigned_backend"],
                    },
                    conn=conn,
                )
            self.emit_event(
                "task.started",
                task_id=row["task_id"],
                folder_id=row["folder_id"],
                run_id=row["run_id"],
                priority=row["priority"],
                source=worker_id,
                payload={
                    "worker_type": row["worker_type"],
                    "claimed_by": claimed_by,
                    "assigned_agent_id": row["assigned_agent_id"],
                    "assigned_agent_role": row["assigned_agent_role"],
                    "assigned_backend": row["assigned_backend"],
                },
                conn=conn,
            )
            conn.execute("COMMIT")
        return ClaimedTask(
            task_id=row["task_id"],
            folder_id=row["folder_id"] or "",
            run_id=row["run_id"],
            goal=row["goal"],
            user_id=row["user_id"],
            channel=row["channel"],
            intent=row["intent"],
            worker_type=row["worker_type"],
            preferred_role=row["preferred_role"] or "general",
            target_agent_id=row["target_agent_id"],
            assigned_agent_id=row["assigned_agent_id"],
            assigned_agent_role=row["assigned_agent_role"],
            assigned_backend=row["assigned_backend"],
            assigned_session_id=row["assigned_session_id"],
            priority=row["priority"],
            context=json.loads(row["context_json"] or "{}"),
        )

    def claim_task(self, task_id: str, worker_id: str) -> ClaimedTask | None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ? AND status = ?",
                (task_id, TASK_STATUS_QUEUED),
            ).fetchone()
            if not row:
                conn.execute("COMMIT")
                return None
            now = utc_now()
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, started_at = COALESCE(started_at, ?), updated_at = ?,
                    claimed_by_worker_id = ?, claimed_by = ?
                WHERE task_id = ? AND status = ?
                """,
                (TASK_STATUS_RUNNING, now, now, worker_id, "direct", row["task_id"], TASK_STATUS_QUEUED),
            )
            conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (RUN_STATUS_RUNNING, now, row["run_id"]),
            )
            if row["folder_id"]:
                conn.execute(
                    "UPDATE folders SET state = ?, updated_at = ? WHERE folder_id = ?",
                    (FOLDER_STATE_RUNNING, now, row["folder_id"]),
                )
                self.emit_event(
                    "folder.running",
                    task_id=row["task_id"],
                    folder_id=row["folder_id"],
                    run_id=row["run_id"],
                    priority=row["priority"],
                    source=worker_id,
                    payload={
                        "state": FOLDER_STATE_RUNNING,
                        "worker_type": row["worker_type"],
                        "claimed_by": "direct",
                        "assigned_agent_id": row["assigned_agent_id"],
                        "assigned_agent_role": row["assigned_agent_role"],
                        "assigned_backend": row["assigned_backend"],
                    },
                    conn=conn,
                )
            self.emit_event(
                "task.started",
                task_id=row["task_id"],
                folder_id=row["folder_id"],
                run_id=row["run_id"],
                priority=row["priority"],
                source=worker_id,
                payload={
                    "worker_type": row["worker_type"],
                    "claimed_by": "direct",
                    "assigned_agent_id": row["assigned_agent_id"],
                    "assigned_agent_role": row["assigned_agent_role"],
                    "assigned_backend": row["assigned_backend"],
                },
                conn=conn,
            )
            conn.execute("COMMIT")
        return ClaimedTask(
            task_id=row["task_id"],
            folder_id=row["folder_id"] or "",
            run_id=row["run_id"],
            goal=row["goal"],
            user_id=row["user_id"],
            channel=row["channel"],
            intent=row["intent"],
            worker_type=row["worker_type"],
            preferred_role=row["preferred_role"] or "general",
            target_agent_id=row["target_agent_id"],
            assigned_agent_id=row["assigned_agent_id"],
            assigned_agent_role=row["assigned_agent_role"],
            assigned_backend=row["assigned_backend"],
            assigned_session_id=row["assigned_session_id"],
            priority=row["priority"],
            context=json.loads(row["context_json"] or "{}"),
        )

    def record_step(self, task: ClaimedTask, action: str, result: dict[str, Any]) -> None:
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute("SELECT steps_json FROM runs WHERE run_id = ?", (task.run_id,)).fetchone()
            steps = json.loads(row["steps_json"] or "[]") if row else []
            steps.append({"index": len(steps) + 1, "action": action, "result": result, "at": now})
            conn.execute(
                "UPDATE runs SET steps_json = ?, updated_at = ? WHERE run_id = ?",
                (json.dumps(steps, ensure_ascii=False), now, task.run_id),
            )
            self.emit_event(
                "folder.step",
                task_id=task.task_id,
                folder_id=task.folder_id or None,
                run_id=task.run_id,
                priority=task.priority,
                source="worker",
                payload={"index": len(steps), "action": action, "result": result},
                conn=conn,
            )

    def start_tool_call(
        self,
        task: ClaimedTask,
        *,
        tool: str,
        args: dict[str, Any],
        timeout_ms: int | None = None,
        retry: int = 0,
        idempotent: bool = False,
    ) -> str:
        tool_call_id = f"tc_{uuid.uuid4().hex}"
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tool_calls(
                  tool_call_id, run_id, task_id, tool, args_json, timeout_ms, retry,
                  idempotent, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
                """,
                (
                    tool_call_id,
                    task.run_id,
                    task.task_id,
                    tool,
                    json.dumps(args, ensure_ascii=False),
                    timeout_ms,
                    retry,
                    1 if idempotent else 0,
                    now,
                ),
            )
            self.emit_event(
                "tool.called",
                task_id=task.task_id,
                folder_id=task.folder_id or None,
                run_id=task.run_id,
                priority=task.priority,
                source="worker",
                payload={"tool_call_id": tool_call_id, "tool": tool, "args": args},
                conn=conn,
            )
        return tool_call_id

    def finish_tool_call(
        self,
        task: ClaimedTask,
        tool_call_id: str,
        *,
        ok: bool,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        event_type = "tool.succeeded" if ok else "tool.failed"
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tool_calls
                SET status = ?, result_json = ?, error_text = ?, completed_at = ?
                WHERE tool_call_id = ?
                """,
                (
                    "succeeded" if ok else "failed",
                    json.dumps(result or {}, ensure_ascii=False),
                    error,
                    now,
                    tool_call_id,
                ),
            )
            self.emit_event(
                event_type,
                task_id=task.task_id,
                folder_id=task.folder_id or None,
                run_id=task.run_id,
                priority=task.priority,
                source="worker",
                payload={"tool_call_id": tool_call_id, "result": result or {}, "error": error},
                conn=conn,
            )

    def complete_task(self, task: ClaimedTask, result_text: str, *, summary: str | None = None) -> None:
        now = utc_now()
        response_id = f"resp_{uuid.uuid4().hex}"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, result_text = ?, updated_at = ?, completed_at = ?
                WHERE task_id = ?
                """,
                (TASK_STATUS_DONE, result_text, now, now, task.task_id),
            )
            conn.execute(
                """
                UPDATE runs
                SET status = ?, summary = ?, updated_at = ?, completed_at = ?
                WHERE run_id = ?
                """,
                ("completed", summary or result_text[:500], now, now, task.run_id),
            )
            if task.folder_id:
                conn.execute(
                    """
                    UPDATE folders
                    SET state = ?, updated_at = ?, completed_at = ?
                    WHERE folder_id = ?
                    """,
                    (FOLDER_STATE_COMPLETED, now, now, task.folder_id),
                )
            conn.execute(
                """
                INSERT INTO responses(
                  response_id, task_id, folder_id, run_id, channel, target_user_id,
                  text, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    response_id,
                    task.task_id,
                    task.folder_id or None,
                    task.run_id,
                    task.channel,
                    task.user_id,
                    result_text,
                    RESPONSE_STATUS_CREATED,
                    now,
                ),
            )
            if task.folder_id:
                self.emit_event(
                    "folder.completed",
                    task_id=task.task_id,
                    folder_id=task.folder_id,
                    run_id=task.run_id,
                    priority=task.priority,
                    source="worker",
                    payload={"state": FOLDER_STATE_COMPLETED},
                    conn=conn,
                )
            self.emit_event(
                "response.created",
                task_id=task.task_id,
                folder_id=task.folder_id or None,
                run_id=task.run_id,
                priority=task.priority,
                source="worker",
                payload={"response_id": response_id, "text": result_text, "channel": task.channel},
                conn=conn,
            )
            self.emit_event(
                "task.completed",
                task_id=task.task_id,
                folder_id=task.folder_id or None,
                run_id=task.run_id,
                priority=task.priority,
                source="worker",
                payload={"status": TASK_STATUS_DONE},
                conn=conn,
            )
            conn.execute("COMMIT")

    def fail_task(self, task: ClaimedTask, error_text: str) -> None:
        now = utc_now()
        response_id = f"resp_{uuid.uuid4().hex}"
        response_text = f"处理失败：{error_text or '未知错误'}"
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, error_text = ?, updated_at = ?, completed_at = ?
                WHERE task_id = ?
                """,
                (TASK_STATUS_FAILED, error_text, now, now, task.task_id),
            )
            conn.execute(
                """
                UPDATE runs
                SET status = ?, summary = ?, updated_at = ?, completed_at = ?
                WHERE run_id = ?
                """,
                (TASK_STATUS_FAILED, error_text[:500], now, now, task.run_id),
            )
            if task.folder_id:
                conn.execute(
                    """
                    UPDATE folders
                    SET state = ?, updated_at = ?, completed_at = ?
                    WHERE folder_id = ?
                    """,
                    (FOLDER_STATE_BLOCKED, now, now, task.folder_id),
                )
                self.emit_event(
                    "folder.blocked",
                    task_id=task.task_id,
                    folder_id=task.folder_id,
                    run_id=task.run_id,
                    priority=task.priority,
                    source="worker",
                    payload={"state": FOLDER_STATE_BLOCKED, "error": error_text},
                    conn=conn,
                )
            conn.execute(
                """
                INSERT INTO responses(
                  response_id, task_id, folder_id, run_id, channel, target_user_id,
                  text, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    response_id,
                    task.task_id,
                    task.folder_id or None,
                    task.run_id,
                    task.channel,
                    task.user_id,
                    response_text,
                    RESPONSE_STATUS_CREATED,
                    now,
                ),
            )
            self.emit_event(
                "task.failed",
                task_id=task.task_id,
                folder_id=task.folder_id or None,
                run_id=task.run_id,
                priority=task.priority,
                source="worker",
                payload={"error": error_text},
                conn=conn,
            )
            self.emit_event(
                "response.created",
                task_id=task.task_id,
                folder_id=task.folder_id or None,
                run_id=task.run_id,
                priority=task.priority,
                source="worker",
                payload={"response_id": response_id, "text": response_text, "channel": task.channel},
                conn=conn,
            )
            conn.execute("COMMIT")

    def cancel_task(self, task_id: str, *, reason: str = "cancelled") -> bool:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                conn.execute("COMMIT")
                return False
            if row["status"] in {TASK_STATUS_DONE, TASK_STATUS_FAILED, TASK_STATUS_CANCELLED}:
                conn.execute("COMMIT")
                return True
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, error_text = ?, updated_at = ?, completed_at = ?
                WHERE task_id = ?
                """,
                (TASK_STATUS_CANCELLED, reason, now, now, task_id),
            )
            conn.execute(
                """
                UPDATE runs
                SET status = ?, summary = ?, updated_at = ?, completed_at = ?
                WHERE run_id = ?
                """,
                (RUN_STATUS_CANCELLED, reason[:500], now, now, row["run_id"]),
            )
            if row["folder_id"]:
                conn.execute(
                    """
                    UPDATE folders
                    SET state = ?, updated_at = ?, completed_at = ?
                    WHERE folder_id = ?
                    """,
                    (FOLDER_STATE_SUSPENDED, now, now, row["folder_id"]),
                )
                self.emit_event(
                    "folder.suspended",
                    task_id=task_id,
                    folder_id=row["folder_id"],
                    run_id=row["run_id"],
                    priority=row["priority"],
                    source="scheduler",
                    payload={"state": FOLDER_STATE_SUSPENDED, "reason": reason},
                    conn=conn,
                )
            self.emit_event(
                "task.cancelled",
                task_id=task_id,
                folder_id=row["folder_id"],
                run_id=row["run_id"],
                priority=row["priority"],
                source="scheduler",
                payload={"reason": reason},
                conn=conn,
            )
            conn.execute("COMMIT")
            return True

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return None
            return self._task_dict(row)

    def get_folder(self, folder_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM folders WHERE folder_id = ?", (folder_id,)).fetchone()
            if not row:
                return None
            return self._folder_dict(row)

    def list_folders(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM folders ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._folder_dict(row) for row in rows]

    def get_response(self, response_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM responses WHERE response_id = ?", (response_id,)).fetchone()
            if not row:
                return None
            return self._response_dict(row)

    def get_latest_response_for_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM responses WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if not row:
                return None
            return self._response_dict(row)

    def list_responses(self, limit: int = 20, *, status: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM responses WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM responses ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._response_dict(row) for row in rows]

    def mark_response_sent(self, response_id: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM responses WHERE response_id = ?", (response_id,)).fetchone()
            if not row:
                return
            conn.execute(
                "UPDATE responses SET status = ?, sent_at = ? WHERE response_id = ?",
                ("sent", now, response_id),
            )
            self.emit_event(
                "response.sent",
                task_id=row["task_id"],
                folder_id=row["folder_id"],
                run_id=row["run_id"],
                priority=50,
                source="gateway",
                payload={"response_id": response_id, "channel": row["channel"]},
                conn=conn,
            )

    def list_agents(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if enabled_only:
                rows = conn.execute("SELECT * FROM agents WHERE enabled = 1 ORDER BY role, agent_id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM agents ORDER BY role, agent_id").fetchall()
            return [self._agent_dict(row) for row in rows]

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            return self._agent_dict(row) if row else None

    def list_tasks(
        self,
        limit: int = 20,
        *,
        worker_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if worker_id:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE claimed_by_worker_id = ? ORDER BY started_at DESC LIMIT ?",
                    (worker_id, limit),
                ).fetchall()
            elif agent_id:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE assigned_agent_id = ? ORDER BY created_at DESC LIMIT ?",
                    (agent_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._task_dict(row) for row in rows]

    def list_events(
        self,
        task_id: str | None = None,
        folder_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if task_id:
                rows = conn.execute(
                    "SELECT * FROM events WHERE task_id = ? ORDER BY created_at ASC LIMIT ?",
                    (task_id, limit),
                ).fetchall()
            elif folder_id:
                rows = conn.execute(
                    "SELECT * FROM events WHERE folder_id = ? ORDER BY created_at ASC LIMIT ?",
                    (folder_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._event_dict(row) for row in rows]

    def list_tool_calls(self, task_id: str | None = None, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if task_id:
                rows = conn.execute(
                    "SELECT * FROM tool_calls WHERE task_id = ? ORDER BY created_at ASC",
                    (task_id,),
                ).fetchall()
            elif run_id:
                rows = conn.execute(
                    "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY created_at ASC",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM tool_calls ORDER BY created_at DESC LIMIT 20").fetchall()
            return [self._tool_call_dict(row) for row in rows]

    def _agent_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "agent_id": row["agent_id"],
            "role": row["role"],
            "backend": row["backend"],
            "display_name": row["display_name"],
            "description": row["description"],
            "tools": json.loads(row["tools_json"] or "[]"),
            "max_concurrency": row["max_concurrency"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _task_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "folder_id": row["folder_id"],
            "run_id": row["run_id"],
            "user_id": row["user_id"],
            "channel": row["channel"],
            "goal": row["goal"],
            "intent": row["intent"],
            "worker_type": row["worker_type"],
            "preferred_role": row["preferred_role"],
            "target_agent_id": row["target_agent_id"],
            "assigned_agent_id": row["assigned_agent_id"],
            "assigned_agent_role": row["assigned_agent_role"],
            "assigned_backend": row["assigned_backend"],
            "assigned_session_id": row["assigned_session_id"],
            "claimed_by_worker_id": row["claimed_by_worker_id"],
            "claimed_by": row["claimed_by"],
            "priority": row["priority"],
            "status": row["status"],
            "context": json.loads(row["context_json"] or "{}"),
            "result_text": row["result_text"],
            "error_text": row["error_text"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }

    def _event_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "type": row["type"],
            "task_id": row["task_id"],
            "folder_id": row["folder_id"],
            "run_id": row["run_id"],
            "priority": row["priority"],
            "source": row["source"],
            "payload": json.loads(row["payload_json"] or "{}"),
            "created_at": row["created_at"],
        }

    def _tool_call_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "tool_call_id": row["tool_call_id"],
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "tool": row["tool"],
            "args": json.loads(row["args_json"] or "{}"),
            "timeout_ms": row["timeout_ms"],
            "retry": row["retry"],
            "idempotent": bool(row["idempotent"]),
            "status": row["status"],
            "result": json.loads(row["result_json"] or "{}"),
            "error_text": row["error_text"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }

    def _folder_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "folder_id": row["folder_id"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "user_id": row["user_id"],
            "channel": row["channel"],
            "goal": row["goal"],
            "state": row["state"],
            "context_snapshot": json.loads(row["context_snapshot_json"] or "{}"),
            "memory_buffer": json.loads(row["memory_buffer_json"] or "[]"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "archived_at": row["archived_at"],
        }

    def _response_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "response_id": row["response_id"],
            "task_id": row["task_id"],
            "folder_id": row["folder_id"],
            "run_id": row["run_id"],
            "channel": row["channel"],
            "target_user_id": row["target_user_id"],
            "text": row["text"],
            "status": row["status"],
            "created_at": row["created_at"],
            "sent_at": row["sent_at"],
        }
