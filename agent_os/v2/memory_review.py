"""记忆自动 curate(借鉴 Hermes 后台 fork 复盘)。

每 N 轮由主链路入队一个 kind=memory_review 的后台任务,常驻 worker 执行本模块:
replay 最近对话 → 判断有没有值得长期记住的稳定事实 → 自己写进 memory/*.md(查重、按现有格式)。
全程后台、静默(不推送用户、不污染主对话上下文)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from . import brain  # noqa: E402
from . import memory_service  # noqa: E402
from . import store as store_mod  # noqa: E402

# 每 N 轮(用户消息)整理一次
REVIEW_EVERY = int(os.getenv("V2_MEMORY_REVIEW_TURNS", "10"))

REVIEW_PROMPT = """你是 Zhen 的微信家庭助手的【记忆整理后台子任务】。这不是对话,没有用户在等你回复。

下面是最近的一段对话。你的唯一任务:判断里面有没有【值得长期记住的稳定事实】,有就写进记忆文件,没有就什么都不做。

记忆目录:{mem_dir}
写入规则:
- 个人/家庭/住址/偏好/身体/兴趣/日程 → {mem_dir}/user-profile.md
- 设备/entity_id/家居操作要点 → {mem_dir}/devices.md
- 长期行为规则/导航/提醒/设备操作经验/工具用法(会影响以后判断的) → {mem_dir}/learned-facts.md(带 [日期] 和 #标签,沿用文件里现有格式)
- 写入前先读对应文件,**已经记过的不要重复写**;是订正就改原条目。
- 只记**稳定**的事实和偏好。临时任务、一次性查询、进度、闲聊、当天天气/行情数值,一律不要记。
- 不确定要不要记,就不记。宁缺毋滥。

只用本地文件编辑完成,不要调用网络/家居工具。完成后用一句话说明你写了什么(或"无需更新")。

最近对话:
"""


def review(db_path, task_id: str, *, session_id: str, n_turns: int = 24,
           timeout: int = 300) -> None:
    store = store_mod.V2Store(db_path)
    try:
        msgs = store.recent_messages(session_id, n_turns)
        # 跳过后台完成类的系统消息,只看真实对话
        convo = "\n".join(
            f"{'用户' if m['role'] == 'user' else '助手'}：{m['content']}"
            for m in msgs if m["content"]
        )
        if len(convo) < 20:
            store.set_task(task_id, status="done", result="对话太少,跳过整理")
            return
        prompt = REVIEW_PROMPT.format(mem_dir=str(memory_service.MEM)) + convo
        result = brain.run_prompt(prompt, timeout=timeout)
        summary = (result.get("result") or "").strip()[:300] or "(无输出)"
        store.set_task(task_id, status="done" if result.get("ok") else "failed",
                       result=summary)
        print(f"[v2-memory-review] task={task_id} ok={result.get('ok')} {summary}", flush=True)
    finally:
        store.close()


def maybe_enqueue(store: store_mod.V2Store, session_id: str) -> str | None:
    """主链路每轮调:到 N 的整数倍就入队一个记忆整理任务(静默后台)。返回 task_id 或 None。"""
    n = store.count_user_messages(session_id)
    if n > 0 and n % REVIEW_EVERY == 0:
        return store.create_task(
            session_id=session_id, goal="（记忆整理）replay 最近对话沉淀稳定事实",
            mode="background", status="queued", kind="memory_review",
        )
    return None
