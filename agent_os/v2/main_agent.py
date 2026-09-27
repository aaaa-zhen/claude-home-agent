"""主链路编排 (step 2).

主链路:
  open_question? → 当成答案绑回 task(决定三)
  else classify mode:
    background → 建 task + spawn worker + 立即 ack(不干等;决定五:慢任务后台化)
    instant/interactive → drain inbox → 组装 → 主脑同步 → 回复 → mark delivered(决定二)

proactive:无用户消息时,poll_notifications 把已完成后台结果主动推给用户。
仍是旁路:状态写独立 v2.db,不写 memory/、不碰在线服务。
"""

from __future__ import annotations

import re

from . import brain, contextual_reminder, memory_review, memory_service, probe as probe_mod, worker
from . import scheduler as scheduler_mod
from . import store as store_mod

# 后台候选:慢/多步/可后台完成的任务(decision five 的启发式;不确定再 probe,后续接)
_BG = re.compile(
    r"下载|下个视频|爬虫|抓取|批量|大范围|全球|扫描|调研|研究一下|整理.*文件"
    r"|写.{0,15}(论文|文章|报告|介绍|网页|页面|代码|脚本|总结|文案|稿|攻略)"
    r"|做.{0,6}(网页|页面|网站|表格|ppt|PPT)"
)
_INSTANT = {"在吗", "在不在", "在不", "人呢", "你好", "谢谢", "多谢", "没事", "没事了",
            "好的", "好", "嗯", "收到", "早", "早安", "晚安"}


def classify_mode(message: str) -> str:
    m = message.strip()
    if re.sub(r"\s+", "", m) in _INSTANT:
        return "instant"
    if _BG.search(m):
        return "background"
    return "interactive"


def _ack_for(goal: str, eta_sec: int | None = None) -> str:
    short = goal.strip()
    if len(short) > 22:
        short = short[:22] + "…"
    if eta_sec and eta_sec >= 30:
        mins = max(1, round(eta_sec / 60))
        return f"好，我去办「{short}」，大概 {mins} 分钟，办好叫你。"
    return f"好，我去办「{short}」，办好第一时间叫你。"


def _dispatch_background(
    store: store_mod.V2Store, *, session_id: str, goal: str,
    scheduler: scheduler_mod.BackgroundScheduler | None, eta_sec: int | None = None,
    defer: bool = False,
) -> str:
    """建后台 task。defer=True(gateway 一次性进程):只入队(queued),交常驻 worker 跑。
    defer=False(常驻进程):经 scheduler 限并发或直接 spawn。返回 task_id。"""
    if defer:
        return store.create_task(session_id=session_id, goal=goal, mode="background",
                                 eta_sec=eta_sec, status="queued")
    task_id = store.create_task(session_id=session_id, goal=goal, mode="background", eta_sec=eta_sec)
    ctx = memory_service.build_context(goal)
    render = ctx.render()
    if scheduler is not None:
        scheduler.submit(task_id, lambda: worker.run_background_task(
            store.db_path, task_id, context_render=render, timeout=1800))
    else:
        worker.spawn(store, task_id, context_render=render, timeout=1800)
    return task_id


def _task_stack_text(store: store_mod.V2Store, session_id: str) -> str:
    rows = store.active_tasks(session_id)
    if not rows:
        return ""
    return "\n".join(f"- [{r['status']}/{r['mode']}] {r['goal']}" for r in rows)


def _drain_inbox(store: store_mod.V2Store, session_id: str) -> tuple[str, list[int]]:
    """取未 delivered 事件,渲染成文本给主脑;返回 (text, event_ids)。mark 在主脑消费后做。"""
    events = store.undelivered_events(session_id)
    if not events:
        return "", []
    lines, ids = [], []
    label = {
        store_mod.EV_DONE: "已完成", store_mod.EV_FAILED: "失败",
        store_mod.EV_PROGRESS: "进度", store_mod.EV_NEEDS_USER: "需要你确认",
    }
    for e in events:
        ids.append(e["event_id"])
        p = e["payload"]
        goal = p.get("goal", "")
        detail = p.get("result") or p.get("error") or p.get("question") or ""
        lines.append(f"- 后台任务「{goal}」{label.get(e['type'], e['type'])}：{detail}")
    return "\n".join(lines), ids


def _store_recent_turns(store: store_mod.V2Store, session_id: str, limit: int = 24) -> list[str]:
    """Current session transcript from SQLite, formatted for Memory Service merge.

    recent-context.md is cross-route glue; SQLite is the source of truth for text
    turns in this WeChat conversation. Feed both to the brain.
    """
    rows = store.recent_messages(session_id, limit)
    lines: list[str] = []
    i = 0
    while i < len(rows):
        row = rows[i]
        if row.get("role") == "user" and i + 1 < len(rows) and rows[i + 1].get("role") == "assistant":
            line = memory_service.format_turn_line(
                ts=rows[i + 1].get("ts"), user_msg=row.get("content", ""),
                reply=rows[i + 1].get("content", ""),
            )
            if line:
                lines.append(line)
            i += 2
            continue
        line = memory_service.format_message_line(
            ts=row.get("ts"), role=row.get("role", ""), content=row.get("content", ""),
        )
        if line:
            lines.append(line)
        i += 1
    return lines


def run_turn(
    message: str,
    *,
    store: store_mod.V2Store,
    session_id: str = "zhen-main",
    turns: list[str] | None = None,
    scheduler: scheduler_mod.BackgroundScheduler | None = None,
    defer_background: bool = False,
    timeout: int = 180,
    model: str = "",
    media: dict | None = None,
) -> dict:
    def _done(d: dict) -> dict:
        # turn 结束才落库 → 主脑 turn 内 recall 只看到历史,不含当前句
        store.record_message(session_id, "user", message)
        store.record_message(session_id, "assistant", d.get("reply", ""))
        # 每 N 轮入队一个记忆整理任务(静默后台,由常驻 worker 执行)
        memory_review.maybe_enqueue(store, session_id)
        return d

    # 带媒体(图片/文件)的消息:必须当场让主脑看图再答,跳过快捷分流,直接走同步主脑。
    if media is None:
        contextual = contextual_reminder.try_handle(message, store=store, session_id=session_id)
        if contextual is not None:
            return _done(contextual)

        # 决定三:有挂着的问题 → 这条消息当答案绑回原 task,恢复执行
        q = store.open_question(session_id)
        if q is not None:
            store.answer_question(q["qid"], message)
            orig = store.get_task(q["task_id"])
            goal = orig["goal"] if orig else q["question"]
            resume_goal = f"{goal}（用户补充信息：{message}）"
            new_id = _dispatch_background(store, session_id=session_id, goal=resume_goal,
                                          scheduler=scheduler, defer=defer_background)
            store.set_task(q["task_id"], status="done", result=f"已收到补充信息，继续任务已转入 {new_id}")
            return _done({"reply": "好，收到，我接着办，办好叫你。", "mode": "resume",
                          "task_id": new_id, "ok": True})

    mode = classify_mode(message)
    eta_sec: int | None = None

    # 带媒体:强制 interactive,当场看图/文件再答(不后台化、不当寒暄)
    if media is not None:
        mode = "interactive"

    # decision five:interactive 但签名像慢任务 → probe 估 ETA,超预算就后台化
    if mode == "interactive" and media is None:
        p = probe_mod.probe(message)
        if p.backgroundable:
            mode = "background"
            eta_sec = p.eta_sec

    # 后台:不干等。建 task + 经 scheduler/spawn/入队 + 立即 ack(带 ETA)
    if mode == "background":
        task_id = _dispatch_background(store, session_id=session_id, goal=message,
                                       scheduler=scheduler, eta_sec=eta_sec,
                                       defer=defer_background)
        return _done({"reply": _ack_for(message, eta_sec), "mode": "background",
                      "task_id": task_id, "ok": True})

    # instant / interactive:同步。先 drain inbox,组装,主脑,再 mark delivered
    inbox_text, event_ids = _drain_inbox(store, session_id)
    recent_turns = turns if turns is not None else _store_recent_turns(store, session_id)
    ctx = memory_service.build_context(
        message, turns=recent_turns, inbox_text=inbox_text,
        task_stack_text=_task_stack_text(store, session_id),
    )
    result = brain.run(message, ctx.render(), timeout=timeout, model=model, media=media)
    reply = (result.get("result") or "").strip() or "(主脑没有产出回复)"
    if result.get("ok"):
        store.mark_delivered(event_ids)  # 决定二:主脑消费后才标记,失败不丢事件
    return _done({
        "reply": reply, "mode": mode, "ok": result.get("ok", False), "task_id": None,
        "durationMs": result.get("durationMs"), "context_sources": ctx.sources,
        "delivered_events": event_ids if result.get("ok") else [],
    })


def poll_notifications(
    store: store_mod.V2Store, *, session_id: str = "zhen-main", timeout: int = 120,
) -> str | None:
    """主动推送路径(决定二的唤醒源 b):没有用户消息时,把已完成后台结果主动通知用户。"""
    inbox_text, event_ids = _drain_inbox(store, session_id)
    if not event_ids:
        return None
    ctx = memory_service.build_context("（系统）后台任务有进展", inbox_text=inbox_text)
    result = brain.run(
        "（无用户消息）后台任务刚有结果,用一两句话主动、自然地通知用户,不要寒暄铺垫。",
        ctx.render(), timeout=timeout,
    )
    reply = (result.get("result") or "").strip()
    if result.get("ok") and reply:
        store.mark_delivered(event_ids)
        return reply
    return None
