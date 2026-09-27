"""Background Worker (step 2) —— 手:拿 Context Packet 执行,把结果写回 Store inbox。

决定:一次性 exec 对 background job 是对的(写论文/查路线不需要对话连贯,只需要 Packet)。
worker 绝不直接回复用户;只写 task.done / task.failed 事件,由主脑下轮 drain 或主动推送决定怎么告诉用户。
"""

from __future__ import annotations

import threading

from . import brain, store as store_mod


def run_background_task(db_path, task_id: str, *, context_render: str = "",
                        timeout: int = 1800) -> None:
    """同步执行一个后台任务,完成后写事件。通常由 spawn() 放线程里跑。

    自己开一条到同一 DB 的连接(sqlite 连接不跨线程;WAL 管并发)。
    """
    store = store_mod.V2Store(db_path)
    task = store.get_task(task_id)
    if not task:
        store.close()
        return
    try:
        result = brain.run(task["goal"], context_render, timeout=timeout)
        if result.get("ok"):
            text = (result.get("result") or "").strip() or "已完成。"
            store.set_task(task_id, status="done", result=text)
            store.add_event(
                type=store_mod.EV_DONE,
                session_id=task["session_id"],
                task_id=task_id,
                payload={"goal": task["goal"], "result": text},
            )
        else:
            err = (result.get("stderr") or result.get("error") or "backend failed").strip()
            store.set_task(task_id, status="failed", result=err[:500])
            store.add_event(
                type=store_mod.EV_FAILED,
                session_id=task["session_id"],
                task_id=task_id,
                payload={"goal": task["goal"], "error": err[:500]},
            )
    except Exception as exc:  # noqa: BLE001 - worker boundary records all failures
        store.set_task(task_id, status="failed", result=str(exc)[:500])
        store.add_event(
            type=store_mod.EV_FAILED,
            session_id=task["session_id"],
            task_id=task_id,
            payload={"goal": task["goal"], "error": str(exc)[:500]},
        )
    finally:
        store.close()


def spawn(store: store_mod.V2Store, task_id: str, *, context_render: str = "",
          timeout: int = 1800) -> threading.Thread:
    """旁路用线程模拟独立 worker 进程(转正时换成 launchd worker + 共享 Store)。"""
    th = threading.Thread(
        target=run_background_task,
        args=(store.db_path, task_id),
        kwargs={"context_render": context_render, "timeout": timeout},
        daemon=True,
    )
    th.start()
    return th
