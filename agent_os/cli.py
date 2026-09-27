from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .classifier import classify_message
from .models import WORKER_AGENT, WORKER_CONTROL, WORKER_JOB
from .scheduler import Scheduler
from .store import AgentOSStore
from .workers import Worker


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "runtime" / "agent-os.sqlite3"


def make_store(args: argparse.Namespace) -> AgentOSStore:
    return AgentOSStore(Path(args.db).expanduser().resolve())


def print_result(data: Any, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif isinstance(data, str):
        print(data)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_init(args: argparse.Namespace) -> int:
    store = make_store(args)
    print_result({"ok": True, "db": str(store.db_path)}, json_output=args.json)
    return 0


def cmd_classify(args: argparse.Namespace) -> int:
    c = classify_message(args.text)
    print_result(
        {
            "intent": c.intent,
            "worker_type": c.worker_type,
            "priority": c.priority,
            "preferred_role": c.preferred_role,
            "target_agent_id": c.target_agent_id,
        },
        json_output=args.json,
    )
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    store = make_store(args)
    c = classify_message(args.text)
    created = Scheduler(store).submit_message(
        user_id=args.user_id,
        channel=args.channel,
        text=args.text,
        priority=args.priority if args.priority is not None else c.priority,
        classification=c,
    )
    task = store.get_task(created["task_id"])
    print_result(task, json_output=args.json)
    if args.wait:
        return wait_for_task(store, created["task_id"], timeout=args.timeout, json_output=args.json)
    return 0


def cmd_gateway(args: argparse.Namespace) -> int:
    store = make_store(args)
    c = classify_message(args.text)
    context = {}
    if args.request_id:
        context["request_id"] = args.request_id
    if args.context_token:
        context["context_token"] = args.context_token
    created = Scheduler(store).submit_message(
        user_id=args.user_id,
        channel=args.channel,
        text=args.text,
        priority=args.priority if args.priority is not None else c.priority,
        context=context,
        classification=c,
    )
    if args.inline:
        worker = Worker(
            store=store,
            root=ROOT,
            worker_type=c.worker_type,
            worker_id=f"gateway-inline-{c.worker_type}-{os.getpid()}",
        )
        worker.run_task_once(created["task_id"])
    if args.wait:
        return wait_for_response(
            store,
            created["task_id"],
            timeout=args.timeout,
            json_output=args.json,
            mark_sent=args.mark_sent,
        )
    task = store.get_task(created["task_id"])
    print_result({"ok": True, **created, "intent": c.intent, "worker_type": c.worker_type, "task": task}, json_output=args.json)
    return 0


def cmd_work(args: argparse.Namespace) -> int:
    store = make_store(args)
    worker = Worker(
        store=store,
        root=ROOT,
        worker_type=args.worker,
        worker_id=args.worker_id or f"{args.worker}-{os.getpid()}",
    )
    worker.loop(idle_sleep=args.idle_sleep, once=args.once)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    store = make_store(args)
    c = classify_message(args.text)
    created = Scheduler(store).submit_message(
        user_id=args.user_id,
        channel=args.channel,
        text=args.text,
        priority=args.priority if args.priority is not None else c.priority,
        classification=c,
    )
    worker = Worker(
        store=store,
        root=ROOT,
        worker_type=c.worker_type,
        worker_id=f"inline-{c.worker_type}-{os.getpid()}",
    )
    worker.run_task_once(created["task_id"])
    return wait_for_task(store, created["task_id"], timeout=args.timeout, json_output=args.json)


def cmd_wechat_control(args: argparse.Namespace) -> int:
    classification = classify_message(args.text)
    if classification.worker_type != WORKER_CONTROL:
        print_result({"handled": False, "intent": classification.intent}, json_output=True)
        return 0

    store = make_store(args)
    created = Scheduler(store).submit_message(
        user_id=args.user_id,
        channel=args.channel,
        text=args.text,
        priority=args.priority if args.priority is not None else classification.priority,
        context={"request_id": args.request_id} if args.request_id else None,
        classification=classification,
    )
    worker = Worker(
        store=store,
        root=ROOT,
        worker_type=WORKER_CONTROL,
        worker_id=f"wechat-control-{os.getpid()}",
    )
    worker.run_task_once(created["task_id"])
    task = store.get_task(created["task_id"])
    if not task:
        print_result({"handled": True, "ok": False, "error": "task disappeared", **created}, json_output=True)
        return 1
    if task["status"] == "done":
        print_result(
            {
                "handled": True,
                "ok": True,
                "task_id": task["task_id"],
                "run_id": task["run_id"],
                "intent": task["intent"],
                "text": task["result_text"] or "已完成。",
            },
            json_output=True,
        )
        return 0
    print_result(
        {
            "handled": True,
            "ok": False,
            "task_id": task["task_id"],
            "run_id": task["run_id"],
            "intent": task["intent"],
            "error": task["error_text"] or "处理失败",
            "text": f"处理失败：{task['error_text'] or '未知错误'}",
        },
        json_output=True,
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = make_store(args)
    task = store.get_task(args.task_id)
    if not task:
        print_result({"ok": False, "error": "task not found"}, json_output=args.json)
        return 1
    print_result(task, json_output=args.json)
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    store = make_store(args)
    ok = store.cancel_task(args.task_id, reason=args.reason)
    task = store.get_task(args.task_id)
    print_result({"ok": ok, "task": task}, json_output=args.json)
    return 0 if ok else 1


def cmd_folder(args: argparse.Namespace) -> int:
    store = make_store(args)
    folder = store.get_folder(args.folder_id)
    if not folder:
        print_result({"ok": False, "error": "folder not found"}, json_output=args.json)
        return 1
    if args.events:
        folder = {**folder, "events": store.list_events(folder_id=args.folder_id, limit=args.limit)}
    print_result(folder, json_output=args.json)
    return 0


def cmd_folders(args: argparse.Namespace) -> int:
    store = make_store(args)
    print_result(store.list_folders(limit=args.limit), json_output=args.json)
    return 0


def cmd_responses(args: argparse.Namespace) -> int:
    store = make_store(args)
    print_result(store.list_responses(limit=args.limit, status=args.status), json_output=args.json)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = make_store(args)
    print_result(store.list_tasks(limit=args.limit, worker_id=args.worker_id, agent_id=args.agent_id), json_output=args.json)
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    store = make_store(args)
    print_result(store.list_agents(enabled_only=args.enabled), json_output=args.json)
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    store = make_store(args)
    print_result(store.list_events(task_id=args.task_id, folder_id=args.folder_id, limit=args.limit), json_output=args.json)
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    store = make_store(args)
    task = store.get_task(args.task_id)
    if not task:
        print_result({"ok": False, "error": "task not found"}, json_output=args.json)
        return 1
    folder = store.get_folder(task["folder_id"]) if task.get("folder_id") else None
    response = store.get_latest_response_for_task(args.task_id)
    trace = {
        "ok": True,
        "task": task,
        "folder": folder,
        "tool_calls": store.list_tool_calls(task_id=args.task_id),
        "events": store.list_events(task_id=args.task_id, limit=args.limit),
        "response": response,
    }
    print_result(trace, json_output=args.json)
    return 0


def wait_for_task(store: AgentOSStore, task_id: str, *, timeout: float, json_output: bool) -> int:
    deadline = time.time() + timeout
    terminal = {"done", "failed", "cancelled"}
    task = None
    while time.time() <= deadline:
        task = store.get_task(task_id)
        if task and task["status"] in terminal:
            print_result(task, json_output=json_output)
            return 0 if task["status"] == "done" else 1
        time.sleep(0.5)
    task = task or store.get_task(task_id)
    print_result({"ok": False, "error": "timeout", "task": task}, json_output=json_output)
    return 124


def wait_for_response(
    store: AgentOSStore,
    task_id: str,
    *,
    timeout: float,
    json_output: bool,
    mark_sent: bool = False,
) -> int:
    deadline = time.time() + timeout
    response = None
    task = None
    while time.time() <= deadline:
        response = store.get_latest_response_for_task(task_id)
        if response:
            if mark_sent:
                store.mark_response_sent(response["response_id"])
                response = store.get_response(response["response_id"])
            task = store.get_task(task_id)
            print_result({"ok": True, "task": task, "response": response}, json_output=json_output)
            return 0
        task = store.get_task(task_id)
        if task and task["status"] in {"failed", "cancelled"}:
            break
        time.sleep(0.25)
    print_result(
        {"ok": False, "error": "timeout waiting for response", "task": task or store.get_task(task_id)},
        json_output=json_output,
    )
    return 124


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent OS v1 local task/event runtime.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite event store path.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize the event store.")
    init.set_defaults(func=cmd_init)

    classify = sub.add_parser("classify", help="Classify a message without enqueueing it.")
    classify.add_argument("text")
    classify.set_defaults(func=cmd_classify)

    submit = sub.add_parser("submit", help="Create a task/run from a message.")
    submit.add_argument("text")
    submit.add_argument("--user-id", default="wechat:zhen")
    submit.add_argument("--channel", default="wechat")
    submit.add_argument("--priority", type=int)
    submit.add_argument("--wait", action="store_true")
    submit.add_argument("--timeout", type=float, default=60)
    submit.set_defaults(func=cmd_submit)

    gateway = sub.add_parser("gateway", help="Gateway entrypoint: emit user_message, create task/folder, optionally wait for response.")
    gateway.add_argument("text")
    gateway.add_argument("--user-id", default="wechat:zhen")
    gateway.add_argument("--channel", default="wechat")
    gateway.add_argument("--priority", type=int)
    gateway.add_argument("--request-id", default="")
    gateway.add_argument("--context-token", default="")
    gateway.add_argument("--inline", action="store_true", help="Run the selected worker in this process after submit.")
    gateway.add_argument("--wait", action="store_true", help="Wait for a response.created event.")
    gateway.add_argument("--mark-sent", action="store_true", help="Mark the response sent after waiting.")
    gateway.add_argument("--timeout", type=float, default=60)
    gateway.set_defaults(func=cmd_gateway)

    work = sub.add_parser("work", help="Run a worker loop.")
    work.add_argument("--worker", choices=[WORKER_CONTROL, WORKER_AGENT, WORKER_JOB], required=True)
    work.add_argument("--worker-id", default="")
    work.add_argument("--idle-sleep", type=float, default=1.0)
    work.add_argument("--once", action="store_true")
    work.set_defaults(func=cmd_work)

    run = sub.add_parser("run", help="Submit one message and run the matching worker once.")
    run.add_argument("text")
    run.add_argument("--user-id", default="wechat:zhen")
    run.add_argument("--channel", default="wechat")
    run.add_argument("--priority", type=int)
    run.add_argument("--timeout", type=float, default=60)
    run.set_defaults(func=cmd_run)

    wechat_control = sub.add_parser("wechat-control", help="Compatibility entrypoint for WeChat home control/status tasks; execution still runs through the agent backend.")
    wechat_control.add_argument("text")
    wechat_control.add_argument("--user-id", default="wechat:zhen")
    wechat_control.add_argument("--channel", default="wechat")
    wechat_control.add_argument("--priority", type=int)
    wechat_control.add_argument("--request-id", default="")
    wechat_control.set_defaults(func=cmd_wechat_control)

    status = sub.add_parser("status", help="Show one task.")
    status.add_argument("task_id")
    status.set_defaults(func=cmd_status)

    cancel = sub.add_parser("cancel", help="Cancel a queued/running task.")
    cancel.add_argument("task_id")
    cancel.add_argument("--reason", default="cancelled")
    cancel.set_defaults(func=cmd_cancel)

    folder = sub.add_parser("folder", help="Show one folder runtime.")
    folder.add_argument("folder_id")
    folder.add_argument("--events", action="store_true")
    folder.add_argument("--limit", type=int, default=50)
    folder.set_defaults(func=cmd_folder)

    folders = sub.add_parser("folders", help="List recent folders.")
    folders.add_argument("--limit", type=int, default=20)
    folders.set_defaults(func=cmd_folders)

    responses = sub.add_parser("responses", help="List response events/outbox rows.")
    responses.add_argument("--limit", type=int, default=20)
    responses.add_argument("--status")
    responses.set_defaults(func=cmd_responses)

    list_cmd = sub.add_parser("list", help="List recent tasks.")
    list_cmd.add_argument("--limit", type=int, default=20)
    list_cmd.add_argument("--worker-id", help="List tasks claimed by one worker, e.g. launchd-agent-3.")
    list_cmd.add_argument("--agent-id", help="List tasks assigned to one logical agent, e.g. codex-weather.")
    list_cmd.set_defaults(func=cmd_list)

    agents = sub.add_parser("agents", help="List logical agent registry entries.")
    agents.add_argument("--enabled", action="store_true")
    agents.set_defaults(func=cmd_agents)

    events = sub.add_parser("events", help="List events.")
    events.add_argument("--task-id")
    events.add_argument("--folder-id")
    events.add_argument("--limit", type=int, default=50)
    events.set_defaults(func=cmd_events)

    trace = sub.add_parser("trace", help="Show task, folder, tool calls, events, and latest response.")
    trace.add_argument("task_id")
    trace.add_argument("--limit", type=int, default=80)
    trace.set_defaults(func=cmd_trace)

    return parser


def main(argv: list[str]) -> int:
    if "--json" in argv and (not argv or argv[0] != "--json"):
        argv = ["--json", *[arg for arg in argv if arg != "--json"]]
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
