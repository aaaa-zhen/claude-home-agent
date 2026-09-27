"""Memory Service — 上下文组装器 (step 1).

决定四:context = 五块变长内容,每块固定预算、确定性组装、超预算截断。
检索先用关键词/recency grep,不上 embedding。

step 1 里 task stack / event inbox / 最近 worker 结果都为空(还没接 Store),
预留好块和预算,接 Store 时只填内容,不改组装器结构。

预算单位:字符数(CJK 近似 1 字符≈1 token,起步够用;之后可换真 tokenizer)。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MEM = Path(os.getenv("V2_MEMORY_DIR", str(ROOT / "memory")))


def _budget(name: str, default: int) -> int:
    return int(os.getenv(f"V2_CTX_{name}", str(default)))


# 每块字符预算(可用 env V2_CTX_* 覆盖)
B_RECENT = _budget("RECENT", 1500)
B_TASKSTACK = _budget("TASKSTACK", 500)
B_INBOX = _budget("INBOX", 500)
B_MEMORY = _budget("MEMORY", 1500)
B_WORKER = _budget("WORKER", 500)

_ENTRY_RE = re.compile(r"^\[(?P<ts>[^\]]+)\]\s*(?P<body>.*)$")
_META_REPLY_RE = re.compile(
    r"^\s*(?:\(主脑没有产出回复\)|"
    r"The user (?:just )?(?:said|sent|wants|is|asked|message)|"
    r"This is (?:just )?a |"
    r"这是接着.*用户说|"
    r".*我直接.*(?:回应|回复)即可|"
    r".*(?:no tools needed|casual chitchat|general knowledge question))",
    re.I,
)


# 关键词 → memory 文件(对齐 CLAUDE.md 的检索规则)
KEYWORD_FILES: list[tuple[str, str]] = [
    (r"空调|灯|温度|窗帘|设备|家居|开关|室温|音乐|homepod", "devices.md"),
    (r"家人|老婆|孩子|女儿|儿子|日程|偏好|公司|住址|生日|个人", "user-profile.md"),
    (r"导航|路线|怎么去|附近|商场|餐厅|医院|珠海|口岸|地铁|公交站", "zhuhai-guide.md"),
    (r"自动化|触发|规则|提醒我|每次|纠正|习惯|上下文|context|刚才|刚让你|刚聊|当下"
     r"|发布|预览|部署|上线|publish|preview|网页|页面|项目|demo|链接", "learned-facts.md"),
    (r"在哪|位置|出行|轨迹|到家|离家|去过", "location-log.md"),
    (r"读书|书单|在读|想读|读过", "reading-list.md"),
]


@dataclass
class ContextBundle:
    recent: str = ""        # 近 N 轮对话
    task_stack: str = ""    # 当前 task stack (step1 空)
    inbox: str = ""         # event inbox 摘要 (step1 空)
    memory: str = ""        # 检索到的 memory
    worker: str = ""        # 最近 worker 结果 (step1 空)
    sources: list[str] = field(default_factory=list)  # 命中的 memory 文件,便于调试

    def render(self) -> str:
        blocks = []
        if self.recent.strip():
            blocks.append(
                "## 近期对话(连贯性,最新在下; 旧助手回复只代表当时说过, 不当成事实或规则)\n"
                f"{self.recent.strip()}"
            )
        if self.task_stack.strip():
            blocks.append(f"## 当前任务栈\n{self.task_stack.strip()}")
        if self.inbox.strip():
            blocks.append(f"## 后台事件(需要时合并回复)\n{self.inbox.strip()}")
        if self.memory.strip():
            blocks.append(f"## 相关记忆\n{self.memory.strip()}")
        if self.worker.strip():
            blocks.append(f"## 最近后台结果\n{self.worker.strip()}")
        return "\n\n".join(blocks) if blocks else "(无可用上下文)"


def _read(name: str) -> str:
    p = MEM / name
    try:
        return p.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def _clip_tail(text: str, limit: int) -> str:
    """保留尾部(recent-context 最新在末尾),按行对齐不切半行。"""
    text = text.strip()
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    nl = tail.find("\n")  # 丢掉开头被切半的那行
    if nl != -1:
        tail = tail[nl + 1:]
    return "…(更早略)\n" + tail


def _clip_head(text: str, limit: int) -> str:
    """保留头部(memory 文件结论通常在前),按行对齐不切半行。"""
    text = text.strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    nl = head.rfind("\n")  # 丢掉结尾被切半的那行
    if nl != -1:
        head = head[:nl]
    return head + "\n…(略)"


def _format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_entry_ts(value: str) -> datetime | None:
    raw = value.strip()
    try:
        if "T" in raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(raw[:len(datetime.now().strftime(fmt))], fmt)
            except ValueError:
                continue
    except ValueError:
        return None
    return None


def _compact(text: str, limit: int = 240) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _reply_part(line: str) -> str:
    if "→" not in line:
        return line
    return line.rsplit("→", 1)[1].strip()


def _is_noisy_recent_line(line: str) -> bool:
    reply = _reply_part(line)
    return bool(_META_REPLY_RE.search(reply))


def _recent_line_key(line: str) -> str:
    line = re.sub(r"^\[[^\]]+\]\s*", "", line)
    line = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", line)
    return re.sub(r"\s+", " ", line).strip().lower()


def _normalize_recent_entries(raw: str, *, max_entries: int | None = None) -> str:
    """Keep only parseable/useful recent-context entries, sorted by real time.

    The file has historically mixed local timestamps with ISO UTC timestamps and
    occasionally included half-written/debug lines. Normalize at read time so the
    brain receives a conversation, not an append-only log accident.
    """
    entries: list[tuple[datetime | None, int, str]] = []
    for index, original in enumerate(raw.splitlines()):
        line = original.strip()
        if not line:
            continue
        match = _ENTRY_RE.match(line)
        if match:
            dt = _parse_entry_ts(match.group("ts"))
            body = match.group("body").strip()
            normalized = f"[{_format_dt(dt)}] {body}" if dt else line
        elif line.startswith("[用户]") or line.startswith("[助手]"):
            dt = None
            normalized = line
        else:
            continue
        if _is_noisy_recent_line(normalized):
            continue
        entries.append((dt, index, normalized))

    if not entries:
        return ""

    seen: set[str] = set()
    deduped: list[tuple[datetime | None, int, str]] = []
    for item in entries:
        key = _recent_line_key(item[2])
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    far_future = datetime.max.replace(microsecond=0)
    deduped.sort(key=lambda item: (item[0] or far_future, item[1]))
    if max_entries is not None and len(deduped) > max_entries:
        deduped = deduped[-max_entries:]
    return "\n".join(item[2] for item in deduped)


def _recent_turns(turns: list[str] | None) -> str:
    """近 N 轮:合并 recent-context.md 与调用方传入的当前 session 消息。"""
    parts = [_read("recent-context.md")]
    if turns:
        parts.append("\n".join(turns))
    normalized = _normalize_recent_entries("\n".join(p for p in parts if p), max_entries=80)
    return _clip_tail(normalized, B_RECENT)


def _active_followups() -> str:
    """pending-followups 必读;只给活跃事项,避免把文件说明当成用户待办。"""
    raw = _read("pending-followups.md").strip()
    if not raw:
        return ""
    if "---" in raw:
        raw = raw.split("---", 1)[1].strip()
    return _clip_head(raw, 600)


def _memory_terms(message: str) -> list[str]:
    terms = re.findall(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}", message.lower())
    if "提醒" in message:
        terms += ["提醒", "cron", "crontab"]
    if "上下文" in message or "context" in message.lower() or "刚才" in message:
        terms += ["上下文", "context", "刚才"]
    if "preply" in message.lower() or "课" in message or "老师" in message:
        terms += ["preply", "课", "老师"]
    if "灯" in message or "空调" in message or "家居" in message:
        terms += ["灯", "空调", "设备", "家居"]
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if len(term) < 2 and term not in {"灯", "课"}:
            continue
        if term not in seen:
            seen.add(term)
            out.append(term)
    return out


def _clip_relevant(body: str, message: str, limit: int) -> str:
    """Return relevant bullets/lines instead of blindly taking file head."""
    body = body.strip()
    if len(body) <= limit:
        return body
    terms = _memory_terms(message)
    if not terms:
        return _clip_head(body, limit)

    lines = body.splitlines()
    selected: list[str] = []
    seen: set[int] = set()
    current_section = ""
    for index, line in enumerate(lines):
        if line.startswith("## "):
            current_section = line
            continue
        hay = line.lower()
        if not any(term.lower() in hay for term in terms):
            continue
        if current_section and current_section not in selected:
            selected.append(current_section)
        start = index
        while start > 0 and lines[start].startswith("  "):
            start -= 1
        for i in range(start, min(len(lines), index + 2)):
            if i in seen:
                continue
            seen.add(i)
            selected.append(lines[i])
        if sum(len(x) + 1 for x in selected) >= limit:
            break

    if not selected:
        return _clip_head(body, limit)
    return _clip_head("\n".join(selected), limit)


def _retrieve_memory(message: str) -> tuple[str, list[str]]:
    """关键词命中 → 拼对应文件的相关摘录。预算内截断。"""
    chunks: list[str] = []
    sources: list[str] = []

    followups = _active_followups() if re.search(r"待办|跟进|提醒|还有|刚才.*做|todo|follow", message, re.I) else ""
    if followups:
        chunks.append(
            "[pending-followups.md | 活跃待办/提醒, 不是当下聊天上下文]\n"
            f"{followups}"
        )
        sources.append("pending-followups.md")

    seen: set[str] = set()
    remaining = B_MEMORY - len(followups)
    for pattern, fname in KEYWORD_FILES:
        if remaining <= 0:
            break
        if fname in seen:
            continue
        if re.search(pattern, message):
            body = _read(fname).strip()
            if not body:
                continue
            clipped = _clip_relevant(body, message, min(remaining, 1200))
            chunks.append(f"[{fname}]\n{clipped}")
            sources.append(fname)
            seen.add(fname)
            remaining -= len(clipped)

    return "\n\n".join(chunks), sources


def format_message_line(*, ts: float | None, role: str, content: str,
                        channel: str = "wechat-db") -> str:
    content = _compact(content)
    if not content:
        return ""
    if role == "assistant" and _META_REPLY_RE.search(content):
        return ""
    label = "用户" if role == "user" else "助手"
    when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else _format_dt(datetime.now())
    return f"[{when}] [{channel}] {label}：{content}"


def format_turn_line(*, ts: float | None, user_msg: str, reply: str,
                     channel: str = "wechat-db") -> str:
    user = _compact(user_msg, 220)
    answer = _compact(reply, 240)
    if not user or not answer or _META_REPLY_RE.search(answer):
        return ""
    when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else _format_dt(datetime.now())
    return f"[{when}] [{channel}] {user} → {answer}"


def normalize_recent_context_file(*, cap: int = 80) -> bool:
    path = MEM / "recent-context.md"
    try:
        existing = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False
    normalized = _normalize_recent_entries(existing, max_entries=cap).strip()
    next_text = (normalized + "\n") if normalized else ""
    if next_text == existing:
        return False
    try:
        path.write_text(next_text, encoding="utf-8")
        return True
    except OSError:
        return False


def append_turn(user_msg: str, reply: str, *, channel: str = "微信", cap: int = 80) -> None:
    """每轮把对话写回 recent-context.md —— 一次性 gateway 进程间 + 重启后的连贯靠它。

    长期记忆(devices/user-profile/learned-facts 等)由主脑按需写;这里只维护短期对话缓冲。
    """
    if _META_REPLY_RE.search(reply or ""):
        return
    ts = _format_dt(datetime.now())
    line = f"[{ts}] [{channel}] {_compact(user_msg, 220)} → {_compact(reply, 240)}"
    path = MEM / "recent-context.md"
    try:
        existing = path.read_text(encoding="utf-8").rstrip("\n")
    except (FileNotFoundError, OSError):
        existing = ""
    next_text = _normalize_recent_entries(f"{existing}\n{line}" if existing else line, max_entries=cap)
    try:
        path.write_text(next_text + "\n", encoding="utf-8")
    except OSError:
        pass


def build_context(
    message: str,
    *,
    turns: list[str] | None = None,
    inbox_text: str = "",
    task_stack_text: str = "",
    worker_text: str = "",
) -> ContextBundle:
    """组装主脑上下文。step2:inbox/task_stack/worker 由主链路从 Store drain 后传入。

    turns: 进程内对话 transcript,演示"每轮重组=连贯"。
    inbox_text/task_stack_text/worker_text: 已 drain 的后台事件 / 任务栈 / 最近结果。
    """
    mem_text, sources = _retrieve_memory(message)
    return ContextBundle(
        recent=_recent_turns(turns),
        task_stack=_clip_head(task_stack_text, B_TASKSTACK),
        inbox=_clip_head(inbox_text, B_INBOX),
        memory=mem_text,
        worker=_clip_head(worker_text, B_WORKER),
        sources=sources,
    )
