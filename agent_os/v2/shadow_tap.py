"""Shadow Tap (影子测试) —— 只读旁路,验证 v2 不切流量。

tail 当天 /tmp/openclaw/openclaw-<date>.log,抓 SDK 写的 `[weixin-msg] start` 行(含全文),
把真实用户微信消息**复制一份**喂给 v2 main_agent 跑,结果只写 runtime/shadow.jsonl。

安全边界:
- 纯只读 tail,不碰在线 weixin-acp / 主链路。
- 默认**绝不发任何微信消息**(log-only)。--notify-me 才会把 v2 影子回复单独发给你自己
  (前缀『[v2影子]』),且永远不会替代主链路回用户。
- 从文件末尾开始,只看新消息,不回放历史。

用法:
  ./venv/bin/python -m agent_os.v2.shadow_tap            # log-only,推荐先这样
  ./venv/bin/python -m agent_os.v2.shadow_tap --notify-me # 额外把影子回复发给你自己
  tail -f agent_os/v2/runtime/shadow.jsonl              # 另开一个终端看影子结果
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import main_agent, scheduler as scheduler_mod
from . import store as store_mod

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = Path("/tmp/openclaw")
SHADOW_LOG = Path(__file__).resolve().parent / "runtime" / "shadow.jsonl"
WEIXIN_SEND = ROOT / "weixin-send.mjs"

_START = "[weixin-msg] start"
_RE_REQ = re.compile(r"requestId=(\S+)")
_RE_FROM = re.compile(r"from=(\S+)")
_RE_TYPES = re.compile(r"types=(\d+)")
_RE_TEXT = re.compile(r'text="(.*)"\s*$')


def _today_log() -> Path:
    return LOG_DIR / f"openclaw-{datetime.now().strftime('%Y-%m-%d')}.log"


def _parse_line(line: str) -> dict | None:
    """从一行 JSONL 里抽出入站消息;非 start 行 / 无文本 / 解析失败 → None。"""
    line = line.strip()
    if not line or _START not in line:
        return None
    try:
        body = json.loads(line).get("1", "")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not body.startswith(_START):
        return None
    text_m = _RE_TEXT.search(body)
    text = text_m.group(1) if text_m else ""
    if not text:
        return None  # 媒体/无文本消息,本测略过
    req = _RE_REQ.search(body)
    frm = _RE_FROM.search(body)
    return {
        "requestId": req.group(1) if req else "",
        "conv": frm.group(1) if frm else "unknown",
        "text": text,
    }


def _send_to_self(reply: str) -> None:
    try:
        subprocess.run(
            ["/opt/homebrew/bin/node", str(WEIXIN_SEND), "--text", f"[v2影子] {reply}"],
            cwd=str(ROOT), timeout=30, capture_output=True,
        )
    except Exception:  # noqa: BLE001 - 影子发送失败不影响测试
        pass


class ShadowTap:
    def __init__(self, *, notify_me: bool = False, max_turns: int = 12):
        self.notify_me = notify_me
        self.max_turns = max_turns
        self.store = store_mod.V2Store(SHADOW_LOG.parent / "shadow.db")
        self.scheduler = scheduler_mod.BackgroundScheduler()
        self.transcripts: dict[str, list[str]] = {}
        self.seen: set[str] = set()
        SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)

    def _record(self, rec: dict) -> None:
        with SHADOW_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _handle(self, msg: dict) -> None:
        if msg["requestId"] and msg["requestId"] in self.seen:
            return
        self.seen.add(msg["requestId"])
        conv = msg["conv"]
        turns = self.transcripts.setdefault(conv, [])
        t0 = time.time()
        out = main_agent.run_turn(
            msg["text"], store=self.store, session_id=f"shadow:{conv}",
            turns=turns, scheduler=self.scheduler,
        )
        turns.append(f"[用户] {msg['text']}")
        turns.append(f"[助手] {out['reply']}")
        del turns[: max(0, len(turns) - self.max_turns * 2)]
        rec = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "conv": conv, "inbound": msg["text"], "mode": out.get("mode"),
            "reply": out["reply"], "task_id": out.get("task_id"),
            "durationMs": out.get("durationMs"),
        }
        self._record(rec)
        print(f"[{rec['time']}] ({out.get('mode')}) 收<< {msg['text']}\n           v2>> {out['reply']}\n",
              flush=True)
        if self.notify_me:
            _send_to_self(out["reply"])

    def run(self) -> None:
        print(f"shadow tap 启动:tail {LOG_DIR}/openclaw-<date>.log  notify_me={self.notify_me}", flush=True)
        print(f"影子结果写入:{SHADOW_LOG}\n", flush=True)
        cur_path = _today_log()
        fh = cur_path.open("r", encoding="utf-8", errors="replace") if cur_path.exists() else None
        if fh:
            fh.seek(0, 2)  # 从末尾开始,只看新消息
        while True:
            # 日期滚动 → 换当天文件
            want = _today_log()
            if want != cur_path:
                if fh:
                    fh.close()
                cur_path = want
                fh = cur_path.open("r", encoding="utf-8", errors="replace") if cur_path.exists() else None
                if fh:
                    fh.seek(0, 2)
            if fh is None:
                if cur_path.exists():
                    fh = cur_path.open("r", encoding="utf-8", errors="replace")
                    fh.seek(0, 2)
                else:
                    time.sleep(1.0)
                    continue
            line = fh.readline()
            if not line:
                time.sleep(0.5)
                continue
            msg = _parse_line(line)
            if msg:
                try:
                    self._handle(msg)
                except Exception as exc:  # noqa: BLE001 - 影子单条失败不该挂掉 tap
                    print(f"[shadow] handle error: {exc}", file=sys.stderr, flush=True)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="agent_os.v2.shadow_tap", description="v2 影子测试(只读 tap)")
    p.add_argument("--notify-me", action="store_true",
                   help="把 v2 影子回复单独发给你自己(前缀[v2影子]);默认只写日志不发")
    args = p.parse_args(argv)
    try:
        ShadowTap(notify_me=args.notify_me).run()
    except KeyboardInterrupt:
        print("\nshadow tap 停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
