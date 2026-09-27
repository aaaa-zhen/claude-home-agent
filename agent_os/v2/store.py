"""最小 v2 Store (step 2) —— 旁路专用,独立 SQLite,不碰生产 agent_os DB。

三张表对齐 docs/session-agent-os-v2.md §9:
  tasks              背景/活跃任务 + mode/eta/status/result
  events             event inbox,带 delivered(决定二:幂等)
  pending_questions  blocked 任务挂着的待答问题(决定三:答案绑回 task)

时间用 epoch 秒。task_id/event 自带顺序。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

RUNTIME = Path(__file__).resolve().parent / "runtime"
DEFAULT_DB = RUNTIME / "v2.db"

# inbox 事件类型(设计基线)
EV_DONE = "task.done"
EV_FAILED = "task.failed"
EV_PROGRESS = "task.progress"
EV_NEEDS_USER = "task.needs_user"


def _now() -> float:
    return time.time()


_jieba = None
_jieba_tried = False


def _get_jieba():
    """惰性导入 jieba(首次加载词典 ~0.3s);不可用返回 None。"""
    global _jieba, _jieba_tried
    if not _jieba_tried:
        _jieba_tried = True
        try:
            import jieba  # noqa: PLC0415
            jieba.setLogLevel(60)  # 静音 building prefix dict 等日志
            _jieba = jieba
        except Exception:  # noqa: BLE001
            _jieba = None
    return _jieba


def _clean(toks) -> list[str]:
    # 去掉纯标点/单字虚词噪声,保留有检索价值的词
    return [t for t in (x.strip() for x in toks) if t and (len(t) >= 2 or t.isalnum())]


def _segment_tokens(text: str) -> list[str]:
    """索引用:精确分词。"""
    j = _get_jieba()
    return _clean(j.lcut(text)) if j else []


def _query_tokens(text: str) -> list[str]:
    """检索用:搜索引擎模式,把长词再切出子词,提升召回。"""
    j = _get_jieba()
    if not j:
        return []
    toks = _clean(j.lcut_for_search(text))
    return list(dict.fromkeys(toks))  # 去重保序


_CJK = re.compile(r"[一-鿿]+")


def _cjk_bigrams(text: str) -> list[str]:
    """每段连续中文的 2 字滑窗 —— 不赌分词,任何 2 字重叠都能召回。"""
    out: list[str] = []
    for run in _CJK.findall(text):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _candidates(query: str) -> list[str]:
    """检索候选子串:jieba 词(≥2)∪ 中文 2 字滑窗 ∪ 英数 token。去重保序。"""
    cands: list[str] = [t for t in _query_tokens(query) if len(t) >= 2 or t.isalnum()]
    cands += _cjk_bigrams(query)
    cands += re.findall(r"[A-Za-z0-9]{2,}", query)
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c); out.append(c)
    return out


def _segment(text: str) -> str:
    return " ".join(_segment_tokens(text))


class V2Store:
    def __init__(self, db_path: str | os.PathLike | None = None):
        self.db_path = Path(db_path or os.getenv("V2_DB", DEFAULT_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks(
              task_id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              goal TEXT NOT NULL,
              mode TEXT NOT NULL,
              status TEXT NOT NULL,
              eta_sec INTEGER,
              result TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events(
              event_id INTEGER PRIMARY KEY AUTOINCREMENT,
              type TEXT NOT NULL,
              task_id TEXT,
              session_id TEXT NOT NULL,
              payload_json TEXT NOT NULL DEFAULT '{}',
              delivered INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_questions(
              qid INTEGER PRIMARY KEY AUTOINCREMENT,
              task_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              question TEXT NOT NULL,
              answer TEXT,
              created_at REAL NOT NULL,
              answered_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_events_inbox
              ON events(session_id, delivered, created_at);
            CREATE TABLE IF NOT EXISTS kv(
              key TEXT PRIMARY KEY, value TEXT, updated_at REAL
            );
            """
        )
        # 跨会话长期检索:jieba 中文分词 → 词级 FTS5(unicode61),bm25 相关性排序。
        # messages 存原文;messages_fts 存分词后的 seg,rowid 对齐 messages.msg_id。
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS messages("
            "msg_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, ts REAL, content TEXT)"
        )
        try:
            self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(seg)")
            self._fts = True
        except sqlite3.OperationalError:
            self._fts = False
        # 迁移:tasks.kind 区分普通任务 / 记忆整理(memory_review)
        try:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'task'")
        except sqlite3.OperationalError:
            pass  # 列已存在
        self.conn.commit()

    def recent_messages(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT role, content, ts FROM messages WHERE session_id=? "
            "ORDER BY msg_id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [{"role": r[0], "content": r[1], "ts": r[2]} for r in reversed(rows)]

    def count_user_messages(self, session_id: str) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM messages WHERE session_id=? AND role='user'", (session_id,),
        ).fetchone()[0]

    # ---- kv(存最近 context_token 等) ----
    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _now()),
        )
        self.conn.commit()

    def get_kv(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    # ---- 消息记录 & 跨会话检索(长期记忆) ----
    def record_message(self, session_id: str, role: str, content: str) -> None:
        content = (content or "").strip()
        if not content:
            return
        cur = self.conn.execute(
            "INSERT INTO messages(session_id, role, ts, content) VALUES(?,?,?,?)",
            (session_id, role, _now(), content),
        )
        if self._fts:
            seg = _segment(content)
            if seg:
                self.conn.execute("INSERT INTO messages_fts(rowid, seg) VALUES(?,?)",
                                  (cur.lastrowid, seg))
        self.conn.commit()

    def search_messages(self, query: str, *, limit: int = 8,
                        session_id: str | None = None) -> list[dict[str, Any]]:
        """FTS5(jieba 词,bm25 排序)拿主命中 + 2 字滑窗 LIKE 兜底召回,合并去重。"""
        q = (query or "").strip()
        if not q:
            return []
        sess_sql = " AND m.session_id=?" if session_id else ""
        sess_p = [session_id] if session_id else []
        merged: list[tuple] = []
        seen: set = set()

        def add(rows):
            for r in rows:
                key = (r[0], r[3])
                if key not in seen:
                    seen.add(key); merged.append(r)

        # 1) FTS5 词级匹配(快、bm25 排序)
        toks = _query_tokens(q) if self._fts else []
        if toks:
            match = " OR ".join(f'"{t}"' for t in toks)
            try:
                add(self.conn.execute(
                    "SELECT m.session_id, m.role, m.ts, m.content "
                    "FROM messages_fts f JOIN messages m ON m.msg_id=f.rowid "
                    f"WHERE messages_fts MATCH ?{sess_sql} ORDER BY rank LIMIT ?",
                    [match, *sess_p, limit],
                ).fetchall())
            except sqlite3.OperationalError:
                pass
        # 2) 候选子串 LIKE 兜底召回(不赌分词,2 字滑窗保证不漏)
        if len(merged) < limit:
            cands = _candidates(q) or [q]
            like_sql = " OR ".join("m.content LIKE ?" for _ in cands)
            add(self.conn.execute(
                f"SELECT m.session_id, m.role, m.ts, m.content FROM messages m "
                f"WHERE ({like_sql}){sess_sql} ORDER BY m.msg_id DESC LIMIT ?",
                [*[f"%{c}%" for c in cands], *sess_p, limit],
            ).fetchall())
        rows = merged[:limit]
        return [{"session_id": r[0], "role": r[1], "ts": r[2], "content": r[3]} for r in rows]

    # ---- tasks ----
    def create_task(self, *, session_id: str, goal: str, mode: str,
                    eta_sec: int | None = None, status: str = "running",
                    kind: str = "task") -> str:
        task_id = "t_" + uuid.uuid4().hex[:10]
        now = _now()
        self.conn.execute(
            "INSERT INTO tasks(task_id, session_id, goal, mode, status, eta_sec, kind, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, session_id, goal, mode, status, eta_sec, kind, now, now),
        )
        self.conn.commit()
        return task_id

    def claim_queued(self) -> dict | None:
        """常驻 worker 用:原子领取一个 queued 任务(置 running)。"""
        cur = self.conn.execute(
            "UPDATE tasks SET status='running', updated_at=? "
            "WHERE task_id=(SELECT task_id FROM tasks WHERE status='queued' "
            "ORDER BY created_at LIMIT 1) RETURNING *",
            (_now(),),
        )
        row = cur.fetchone()
        self.conn.commit()
        return dict(row) if row else None

    def set_task(self, task_id: str, *, status: str | None = None, result: str | None = None) -> None:
        sets, vals = [], []
        if status is not None:
            sets.append("status=?"); vals.append(status)
        if result is not None:
            sets.append("result=?"); vals.append(result)
        if not sets:
            return
        sets.append("updated_at=?"); vals.append(_now())
        vals.append(task_id)
        self.conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id=?", vals)
        self.conn.commit()

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def active_tasks(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE session_id=? AND status IN ('queued','running','blocked')"
            " ORDER BY created_at",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_results(self, session_id: str, limit: int = 3) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE session_id=? AND status IN ('done','failed')"
            " ORDER BY updated_at DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- events / inbox ----
    def add_event(self, *, type: str, session_id: str, task_id: str | None = None,
                  payload: dict[str, Any] | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO events(type, task_id, session_id, payload_json, created_at) VALUES(?,?,?,?,?)",
            (type, task_id, session_id, json.dumps(payload or {}, ensure_ascii=False), _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def undelivered_events(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE session_id=? AND delivered=0 ORDER BY created_at",
            (session_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            out.append(d)
        return out

    def mark_delivered(self, event_ids: list[int]) -> None:
        if not event_ids:
            return
        q = ",".join("?" * len(event_ids))
        self.conn.execute(f"UPDATE events SET delivered=1 WHERE event_id IN ({q})", event_ids)
        self.conn.commit()

    # ---- pending questions (决定三) ----
    def add_pending_question(self, *, task_id: str, session_id: str, question: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO pending_questions(task_id, session_id, question, created_at) VALUES(?,?,?,?)",
            (task_id, session_id, question, _now()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def open_question(self, session_id: str) -> dict[str, Any] | None:
        """该 session 最早一个未回答的问题 —— 主脑用它把下一条消息绑回 task。"""
        row = self.conn.execute(
            "SELECT * FROM pending_questions WHERE session_id=? AND answered_at IS NULL"
            " ORDER BY created_at LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def answer_question(self, qid: int, answer: str) -> None:
        self.conn.execute(
            "UPDATE pending_questions SET answer=?, answered_at=? WHERE qid=?",
            (answer, _now(), qid),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
