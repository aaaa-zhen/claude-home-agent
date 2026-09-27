"""Scheduler (step 3) —— 后台任务并发管控。

设计基线:起步就是 FIFO + 并发上限,等多后台任务抢资源了再变聪明。
runner 依赖注入(callable),便于不依赖 codex 测并发。
"""

from __future__ import annotations

import os
import threading
from collections import deque
from typing import Callable

Runner = Callable[[], None]


class BackgroundScheduler:
    def __init__(self, *, max_concurrent: int | None = None):
        self.max_concurrent = max_concurrent or int(os.getenv("V2_MAX_BG_CONCURRENT", "2"))
        self._lock = threading.Lock()
        self._queue: deque[tuple[str, Runner]] = deque()
        self._running: set[str] = set()
        self._threads: dict[str, threading.Thread] = {}

    def submit(self, task_id: str, runner: Runner) -> None:
        with self._lock:
            self._queue.append((task_id, runner))
        self._dispatch()

    def _dispatch(self) -> None:
        with self._lock:
            while self._queue and len(self._running) < self.max_concurrent:
                task_id, runner = self._queue.popleft()
                self._running.add(task_id)
                th = threading.Thread(target=self._run, args=(task_id, runner), daemon=True)
                self._threads[task_id] = th
                th.start()

    def _run(self, task_id: str, runner: Runner) -> None:
        try:
            runner()
        finally:
            with self._lock:
                self._running.discard(task_id)
                self._threads.pop(task_id, None)
            self._dispatch()  # 空出一个槽,拉队列里下一个

    # ---- 观测(测试/调试用) ----
    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"running": len(self._running), "queued": len(self._queue),
                    "max": self.max_concurrent}

    def drain(self, timeout: float = 600.0) -> None:
        """阻塞等所有已提交任务跑完(测试用)。"""
        import time
        start = time.time()
        while time.time() - start < timeout:
            with self._lock:
                if not self._running and not self._queue:
                    return
            time.sleep(0.2)
