"""Session-Agent OS v2 sidecar CLI.

  python -m agent_os.v2 assemble "查一下室温"      # 只看组装上下文,不调 codex
  python -m agent_os.v2 turn "客厅空调开着吗"       # 单轮(instant/interactive 同步;background 立即 ack)
  python -m agent_os.v2 chat                        # 多轮 REPL:每轮 drain inbox,进 prompt 前主动推送
  python -m agent_os.v2 tasks                       # 看 v2.db 里的任务
  python -m agent_os.v2 events                      # 看 event inbox
  python -m agent_os.v2 tools                       # 工具清单

只读 memory/;状态写独立 runtime/v2.db。不碰在线服务。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import main_agent, memory_service, scheduler as scheduler_mod, tool_registry
from . import store as store_mod


def cmd_assemble(args: argparse.Namespace) -> int:
    ctx = memory_service.build_context(args.message)
    if args.json:
        print(json.dumps({"sources": ctx.sources, "render": ctx.render()}, ensure_ascii=False, indent=2))
    else:
        print(f"命中 memory 文件: {ctx.sources or '(无)'}\n")
        print(ctx.render())
    return 0


def cmd_turn(args: argparse.Namespace) -> int:
    store = store_mod.V2Store()
    try:
        out = main_agent.run_turn(args.message, store=store, session_id=args.session,
                                  timeout=args.timeout, model=args.model)
    finally:
        store.close()
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        tag = f"[{out['mode']}]" + (f" task={out['task_id']}" if out.get("task_id") else "")
        print(f"{tag}\n{out['reply']}")
    return 0 if out.get("ok") else 1


def cmd_chat(args: argparse.Namespace) -> int:
    print("v2 chat。每轮 drain inbox;进 prompt 前主动推送后台结果。Ctrl-C / 'exit' 退出。\n")
    store = store_mod.V2Store()
    scheduler = scheduler_mod.BackgroundScheduler()
    transcript: list[str] = []
    try:
        while True:
            note = main_agent.poll_notifications(store, session_id=args.session, timeout=args.timeout)
            if note:
                print(f"管家(主动)> {note}\n")
            try:
                message = input("你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not message or message in {"exit", "quit"}:
                return 0
            out = main_agent.run_turn(message, store=store, session_id=args.session,
                                      turns=transcript, scheduler=scheduler,
                                      timeout=args.timeout, model=args.model)
            transcript.append(f"[用户] {message}")
            transcript.append(f"[助手] {out['reply']}")
            tag = f"[{out['mode']}]"
            print(f"管家{tag}> {out['reply']}\n")
    finally:
        store.close()


def cmd_tasks(args: argparse.Namespace) -> int:
    store = store_mod.V2Store()
    rows = store.active_tasks(args.session) + store.recent_results(args.session, limit=10)
    store.close()
    for r in rows:
        print(f"{r['task_id']} [{r['status']}/{r['mode']}] {r['goal'][:40]}"
              + (f"  -> {(r['result'] or '')[:60]}" if r.get("result") else ""))
    if not rows:
        print("(无任务)")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    store = store_mod.V2Store()
    rows = store.undelivered_events(args.session)
    store.close()
    for e in rows:
        print(f"#{e['event_id']} {e['type']} delivered={e['delivered']} {e['payload']}")
    if not rows:
        print("(inbox 为空 / 全部已投递)")
    return 0


def cmd_gateway(args: argparse.Namespace) -> int:
    """微信 SDK 每条消息 shell 调这里。一次性进程:context 靠 recent-context.md,
    后台任务入队给常驻 worker。输出补丁期望的 {"response":{"text":...}}。"""
    session_id = args.user_id or args.session
    store = store_mod.V2Store()
    # 存最近 context_token,供 worker 发文件(媒体必须带,文本不用)
    if args.context_token:
        store.set_kv(f"ctxtoken:{session_id}", args.context_token)
    # 媒体(图片/文件):把路径折进消息(落库 + 写回 recent-context,跨轮可见/可重读),
    # 同时单独传 media dict 给主脑,驱动它先 Read 看图再答。
    media = None
    message = args.message
    if args.media:
        kind = (args.media_type or "image").strip().lower()
        media = {"path": args.media, "type": kind}
        label = {"image": "图片", "video": "视频", "audio": "语音", "file": "文件"}.get(kind, "文件")
        marker = f"[发来{label}:{args.media}]"
        message = f"{args.message.strip()} {marker}".strip() if args.message.strip() else marker
    try:
        out = main_agent.run_turn(
            message, store=store, session_id=session_id,
            defer_background=True, timeout=args.timeout, media=media,
        )
    finally:
        store.close()
    reply = out["reply"]
    # 每轮写回短期记忆 → 下一条消息的进程能读到(长期上下文)
    memory_service.append_turn(message, reply, channel=args.channel or "微信")
    print(json.dumps({
        "response": {"text": reply},
        "mode": out.get("mode"),
        "task": {"task_id": out.get("task_id")} if out.get("task_id") else None,
    }, ensure_ascii=False))
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    """跨会话检索历史对话(长期记忆)。主脑/用户都可调。"""
    import time as _t
    store = store_mod.V2Store()
    rows = store.search_messages(args.query, limit=args.limit)
    store.close()
    if not rows:
        print(f"(没找到跟「{args.query}」相关的历史对话)")
        return 0
    for r in rows:
        ago = ""
        if r.get("ts"):
            days = (_t.time() - r["ts"]) / 86400
            ago = f"{int(days)}天前" if days >= 1 else f"{int((_t.time()-r['ts'])/3600)}小时前"
        who = "你" if r["role"] == "user" else "我"
        print(f"[{ago}] {who}：{r['content']}")
    return 0


def cmd_tools(_args: argparse.Namespace) -> int:
    print(tool_registry.render())
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent_os.v2", description="Session-Agent OS v2 sidecar")
    p.add_argument("--session", default="zhen-main")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("assemble"); a.add_argument("message"); a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_assemble)

    t = sub.add_parser("turn"); t.add_argument("message")
    t.add_argument("--timeout", type=int, default=180); t.add_argument("--model", default="")
    t.add_argument("--json", action="store_true"); t.set_defaults(func=cmd_turn)

    c = sub.add_parser("chat"); c.add_argument("--timeout", type=int, default=180)
    c.add_argument("--model", default=""); c.set_defaults(func=cmd_chat)

    g = sub.add_parser("gateway", help="微信 SDK 入口:单条消息→回复 JSON")
    g.add_argument("message")
    g.add_argument("--user-id", default="")
    g.add_argument("--channel", default="微信")
    g.add_argument("--request-id", default="")
    g.add_argument("--context-token", default="")
    g.add_argument("--media", default="")            # 本地媒体文件绝对路径(图片/视频/语音/文件)
    g.add_argument("--media-type", default="image")  # image | video | audio | file
    g.add_argument("--timeout", type=int, default=120)
    g.add_argument("--wait", action="store_true")        # v1 兼容,v2 忽略
    g.add_argument("--mark-sent", action="store_true")   # v1 兼容,v2 忽略
    g.add_argument("--inline", action="store_true")      # v1 兼容,v2 忽略
    g.add_argument("--json", action="store_true")        # v2 总是 JSON
    g.set_defaults(func=cmd_gateway)

    r = sub.add_parser("recall", help="跨会话检索历史对话")
    r.add_argument("query")
    r.add_argument("--limit", type=int, default=6)
    r.set_defaults(func=cmd_recall)

    sub.add_parser("tasks").set_defaults(func=cmd_tasks)
    sub.add_parser("events").set_defaults(func=cmd_events)
    sub.add_parser("tools").set_defaults(func=cmd_tools)
    return p


def main(argv: list[str]) -> int:
    # --session 是顶层参数,但子命令也要拿到;argparse 顶层 + set_defaults 已覆盖
    args = build_parser().parse_args(argv)
    if not hasattr(args, "session"):
        args.session = "zhen-main"
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
