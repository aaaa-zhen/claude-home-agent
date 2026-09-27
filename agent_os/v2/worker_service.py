"""Background Worker Service (常驻) —— 把队列里的后台任务跑完并主动发回微信。

gateway 一次性进程只把后台任务入队(queued);这个常驻进程负责真正执行 + 完成通知。
launchd: com.zhen.v2-worker。

环境:由 run-with-env.sh 提供 HTTPS_PROXY(codex)+ NO_PROXY(HA/微信网关直连)+ .env。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import memory_review, memory_service, worker
from . import scheduler as scheduler_mod
from . import store as store_mod

ROOT = Path(__file__).resolve().parents[2]
NODE = "/opt/homebrew/bin/node"
WEIXIN_SEND = ROOT / "weixin-send.mjs"
WEIXIN_SEND_FILE = ROOT / "weixin-send-file.mjs"
POLL_SEC = float(os.getenv("V2_WORKER_POLL_SEC", "2"))

_SEND_FILE = re.compile(r"\[send_file:([^\]]+)\]")


def _send_text(text: str) -> bool:
    try:
        r = subprocess.run(
            [NODE, str(WEIXIN_SEND), "--text", text],
            cwd=str(ROOT), timeout=40, capture_output=True, text=True,
        )
        return r.returncode == 0
    except Exception as exc:  # noqa: BLE001
        print(f"[v2-worker] send text failed: {exc}", file=sys.stderr, flush=True)
        return False


def _send_file(file_path: str, caption: str, context_token: str = "") -> bool:
    try:
        cmd = [NODE, str(WEIXIN_SEND_FILE), "--file", file_path]
        if caption.strip():
            cmd += ["--text", caption]
        if context_token:
            cmd += ["--context-token", context_token]
        r = subprocess.run(cmd, cwd=str(ROOT), timeout=180, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"[v2-worker] send file failed: {r.stderr.strip()[:200]}", file=sys.stderr, flush=True)
        return r.returncode == 0
    except Exception as exc:  # noqa: BLE001
        print(f"[v2-worker] send file error: {exc}", file=sys.stderr, flush=True)
        return False


def _send_to_user(text: str, context_token: str = "") -> bool:
    """推送结果。含 [send_file:path] 时真正发文件(+清理后的文字说明);否则发文本。"""
    if not text.strip():
        return False
    m = _SEND_FILE.search(text)
    if m:
        fp = m.group(1).strip()
        if not os.path.isabs(fp):
            fp = str(ROOT / fp)
        caption = _SEND_FILE.sub("", text).strip()
        if os.path.exists(fp):
            ok = _send_file(fp, caption, context_token)
            if ok:
                return True
            # 发文件失败兜底:把说明 + 路径用文本发出,至少不丢信息
            print(f"[v2-worker] file send failed, falling back to text path={fp}", file=sys.stderr, flush=True)
            return _send_text(caption or "文件已就绪,但发送失败了。")
        return _send_text(f"{caption}\n(文件没找到:{os.path.basename(fp)})")
    return _send_text(text)


def _run_one(db_path, task_id: str, goal: str, *, kind: str = "task",
             session_id: str = "") -> None:
    # 记忆整理任务:后台 replay 复盘写 memory/,静默,不推送用户
    if kind == "memory_review":
        memory_review.review(db_path, task_id, session_id=session_id)
        return
    ctx = memory_service.build_context(goal)
    worker.run_background_task(db_path, task_id, context_render=ctx.render(), timeout=1800)
    st = store_mod.V2Store(db_path)
    t = st.get_task(task_id)
    ctx_token = st.get_kv(f"ctxtoken:{session_id}") or "" if session_id else ""
    st.close()
    if not t:
        return
    if t["status"] == "done" and (t.get("result") or "").strip():
        sent = _send_to_user(t["result"], ctx_token)
        memory_service.append_turn(f"（后台完成）{goal}", t["result"])
        print(f"[v2-worker] done task={task_id} sent={sent} goal={goal[:30]}", flush=True)
    elif t["status"] == "failed":
        msg = f"刚才那个「{goal[:18]}」没办成：{(t.get('result') or '')[:120]}"
        _send_to_user(msg)
        print(f"[v2-worker] failed task={task_id} goal={goal[:30]}", flush=True)


def main() -> int:
    store = store_mod.V2Store()
    scheduler = scheduler_mod.BackgroundScheduler()
    print(f"[v2-worker] started, polling {store.db_path} every {POLL_SEC}s", flush=True)
    while True:
        claimed = store.claim_queued()
        if claimed:
            tid, goal = claimed["task_id"], claimed["goal"]
            kind, sess = claimed.get("kind", "task"), claimed["session_id"]
            print(f"[v2-worker] claim task={tid} kind={kind} goal={goal[:40]}", flush=True)
            scheduler.submit(tid, lambda tid=tid, goal=goal, kind=kind, sess=sess:
                             _run_one(store.db_path, tid, goal, kind=kind, session_id=sess))
        else:
            time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[v2-worker] stopped.")
