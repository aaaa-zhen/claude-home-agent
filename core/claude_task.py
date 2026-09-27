#!/usr/bin/env python3
"""Run Claude Code as a bounded one-shot command for the home agent."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(os.getenv('WEIXIN_AGENT_ROOT') or SCRIPT_DIR.parent)
CLAUDE_BIN = Path(os.getenv("CLAUDE_CODE_BIN", REPO_ROOT / "node_modules" / ".bin" / "claude"))
DEFAULT_TIMEOUT = int(os.getenv("CLAUDE_TASK_TIMEOUT", "900"))
DEFAULT_PERMISSION_MODE = os.getenv("CLAUDE_TASK_PERMISSION_MODE", "bypassPermissions")
DEFAULT_EFFORT = os.getenv("CLAUDE_TASK_EFFORT", "high")
DEFAULT_MAX_OUTPUT_CHARS = int(os.getenv("CLAUDE_TASK_MAX_OUTPUT_CHARS", "30000"))


SYSTEM_PROMPT = """You are Claude Code running as a one-shot command for Zhen's home agent.
Do the requested task in the specified working directory, then stop.
Be concise in the final answer. Do not ask follow-up questions unless the task is impossible without them.
Do not reveal secrets, tokens, API keys, auth files, or private runtime credentials.
Avoid destructive actions unless the user explicitly requested them."""


def read_stdin() -> str:
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit] + "\n...[truncated]...", True


def parse_stream_json(output: str, max_events: int = 80) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    tool_uses: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    result_text = ""
    session_id = ""
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "result":
            result_text = str(event.get("result") or "")
            session_id = str(event.get("session_id") or session_id)
        if event_type == "assistant":
            for block in event.get("message", {}).get("content", []) or []:
                if block.get("type") == "tool_use":
                    tool_uses.append({
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": block.get("input") or {},
                    })
        if event_type == "user":
            for block in event.get("message", {}).get("content", []) or []:
                if block.get("type") == "tool_result":
                    result = {
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": block.get("is_error", False),
                        "content": block.get("content"),
                    }
                    if event.get("tool_use_result"):
                        result["tool_use_result"] = event["tool_use_result"]
                    tool_results.append(result)
        if len(events) < max_events and event_type in {"system", "assistant", "user", "result"}:
            compact = {"type": event_type}
            if event_type == "system":
                compact["subtype"] = event.get("subtype")
                compact["status"] = event.get("status")
            if event_type == "result":
                compact["subtype"] = event.get("subtype")
                compact["is_error"] = event.get("is_error")
                compact["duration_ms"] = event.get("duration_ms")
            events.append(compact)
    return {
        "result": result_text,
        "session_id": session_id,
        "toolUses": tool_uses,
        "toolResults": tool_results,
        "events": events,
    }


def parse_json_result(output: str) -> dict[str, Any]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return {}
    return {
        "result": str(payload.get("result") or ""),
        "session_id": str(payload.get("session_id") or ""),
        "toolUses": [],
        "toolResults": [],
        "events": [{"type": "result", "subtype": payload.get("subtype"), "is_error": payload.get("is_error")}],
    }


def run_claude(
    *,
    prompt: str,
    cwd: Path,
    timeout: int,
    model: str,
    effort: str,
    permission_mode: str,
    max_budget_usd: str,
    max_output_chars: int,
    add_dirs: list[str],
    output_format: str,
    verbose: bool,
) -> dict[str, Any]:
    if not CLAUDE_BIN.exists():
        raise FileNotFoundError(f"Claude Code binary not found: {CLAUDE_BIN}")
    if not prompt.strip():
        raise ValueError("prompt is required")
    if not cwd.exists() or not cwd.is_dir():
        raise NotADirectoryError(f"cwd does not exist or is not a directory: {cwd}")

    full_prompt = f"{SYSTEM_PROMPT}\n\nTask:\n{prompt.strip()}\n"
    cmd = [
        str(CLAUDE_BIN),
        "--print",
        full_prompt,
        "--output-format",
        output_format,
        "--permission-mode",
        permission_mode,
        "--effort",
        effort,
        "--no-session-persistence",
    ]
    if output_format == "stream-json" or verbose:
        cmd.append("--verbose")
    if model:
        cmd.extend(["--model", model])
    if max_budget_usd:
        cmd.extend(["--max-budget-usd", max_budget_usd])
    for directory in add_dirs:
        if directory:
            cmd.extend(["--add-dir", directory])

    env = os.environ.copy()
    env["PATH"] = f"{REPO_ROOT / 'node_modules' / '.bin'}:/opt/homebrew/bin:{env.get('PATH', '')}"

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        stdout, stdout_truncated = truncate(proc.stdout, max_output_chars)
        stderr, stderr_truncated = truncate(proc.stderr, max_output_chars)
        trace = parse_stream_json(stdout) if output_format == "stream-json" else parse_json_result(stdout) if output_format == "json" else {}
        return {
            "ok": proc.returncode == 0,
            "exitCode": proc.returncode,
            "durationMs": int((time.time() - started) * 1000),
            "cwd": str(cwd),
            "stdout": stdout,
            "stderr": stderr,
            "result": trace.get("result") or stdout.strip(),
            "trace": trace,
            "truncated": stdout_truncated or stderr_truncated,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
        stdout, stdout_truncated = truncate(stdout, max_output_chars)
        stderr, stderr_truncated = truncate(stderr, max_output_chars)
        return {
            "ok": False,
            "exitCode": 124,
            "durationMs": int((time.time() - started) * 1000),
            "cwd": str(cwd),
            "stdout": stdout,
            "stderr": stderr + "\nClaude Code task timed out",
            "result": "",
            "trace": parse_stream_json(stdout) if output_format == "stream-json" else {},
            "error": "timeout",
            "truncated": stdout_truncated or stderr_truncated,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Claude Code as a one-shot home-agent command.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Run a task with Claude Code.")
    run_parser.add_argument("--cwd", default=str(REPO_ROOT))
    run_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    run_parser.add_argument("--model", default=os.getenv("CLAUDE_TASK_MODEL", ""))
    run_parser.add_argument("--effort", default=DEFAULT_EFFORT)
    run_parser.add_argument("--permission-mode", default=DEFAULT_PERMISSION_MODE)
    run_parser.add_argument("--max-budget-usd", default=os.getenv("CLAUDE_TASK_MAX_BUDGET_USD", ""))
    run_parser.add_argument("--max-output-chars", type=int, default=DEFAULT_MAX_OUTPUT_CHARS)
    run_parser.add_argument("--add-dir", action="append", default=[])
    run_parser.add_argument("--output-format", choices=["text", "json", "stream-json"], default=os.getenv("CLAUDE_TASK_OUTPUT_FORMAT", "text"))
    run_parser.add_argument("--verbose", action="store_true")
    run_parser.add_argument("--json", action="store_true")
    run_parser.add_argument("prompt", nargs=argparse.REMAINDER)

    sub.add_parser("doctor", help="Check that Claude Code is callable without starting a model request.")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        if not CLAUDE_BIN.exists():
            print(json.dumps({"ok": False, "error": f"missing: {CLAUDE_BIN}"}, ensure_ascii=False))
            return 1
        result = subprocess.run([str(CLAUDE_BIN), "--version"], text=True, capture_output=True, timeout=15)
        print(json.dumps({
            "ok": result.returncode == 0,
            "claudeBin": str(CLAUDE_BIN),
            "version": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }, ensure_ascii=False))
        return result.returncode

    if args.command == "run":
        prompt = " ".join(args.prompt).strip()
        if prompt.startswith("-- "):
            prompt = prompt[3:].strip()
        if not prompt:
            prompt = read_stdin().strip()
        result = run_claude(
            prompt=prompt,
            cwd=Path(args.cwd).expanduser().resolve(),
            timeout=args.timeout,
            model=args.model,
            effort=args.effort,
            permission_mode=args.permission_mode,
            max_budget_usd=args.max_budget_usd,
            max_output_chars=args.max_output_chars,
            add_dirs=args.add_dir,
            output_format=args.output_format,
            verbose=args.verbose,
        )
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            if result.get("stdout"):
                sys.stdout.write(result["stdout"])
                if not result["stdout"].endswith("\n"):
                    sys.stdout.write("\n")
            if result.get("stderr"):
                sys.stderr.write(result["stderr"])
                if not result["stderr"].endswith("\n"):
                    sys.stderr.write("\n")
        return 0 if result.get("ok") else int(result.get("exitCode") or 1)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
