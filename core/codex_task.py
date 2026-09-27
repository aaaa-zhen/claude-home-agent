#!/usr/bin/env python3
"""Run Codex as a bounded one-shot command for Agent OS."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = Path(os.getenv('WEIXIN_AGENT_ROOT') or SCRIPT_DIR.parent)
CODEX_BIN = Path(os.getenv("CODEX_BIN", "/opt/homebrew/bin/codex"))
if not CODEX_BIN.exists():
    CODEX_BIN = Path(os.getenv("CODEX_BIN", "codex"))
DEFAULT_TIMEOUT = int(os.getenv("CODEX_TASK_TIMEOUT", "900"))
DEFAULT_SANDBOX = os.getenv("CODEX_TASK_SANDBOX", "danger-full-access")
DEFAULT_MAX_OUTPUT_CHARS = int(os.getenv("CODEX_TASK_MAX_OUTPUT_CHARS", "30000"))


SYSTEM_PROMPT = """You are Codex running as a one-shot command for Zhen's home agent.
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


def run_codex(
    *,
    prompt: str,
    cwd: Path,
    timeout: int,
    model: str,
    sandbox: str,
    max_output_chars: int,
    add_dirs: list[str],
) -> dict[str, Any]:
    if not prompt.strip():
        raise ValueError("prompt is required")
    if not cwd.exists() or not cwd.is_dir():
        raise NotADirectoryError(f"cwd does not exist or is not a directory: {cwd}")

    full_prompt = f"{SYSTEM_PROMPT}\n\nTask:\n{prompt.strip()}\n"
    with tempfile.NamedTemporaryFile("w+", delete=False, prefix="codex-task-", suffix=".txt") as last_file:
        last_message_path = Path(last_file.name)
    cmd = [
        str(CODEX_BIN),
        "exec",
        "-C",
        str(cwd),
        "-s",
        sandbox,
        "--output-last-message",
        str(last_message_path),
    ]
    if model:
        cmd.extend(["--model", model])
    for directory in add_dirs:
        if directory:
            cmd.extend(["--add-dir", directory])
    cmd.append(full_prompt)

    env = os.environ.copy()
    env["PATH"] = f"{REPO_ROOT / 'node_modules' / '.bin'}:/opt/homebrew/bin:{env.get('PATH', '')}"
    env.setdefault("CODEX_HOME", "/Users/zhen/home-agent/.codex-weixin")
    env.setdefault("CODEX_SQLITE_HOME", "/Users/zhen/home-agent/.codex-weixin")

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            input="",
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        stdout, stdout_truncated = truncate(proc.stdout, max_output_chars)
        stderr, stderr_truncated = truncate(proc.stderr, max_output_chars)
        result = ""
        try:
            result = last_message_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            result = ""
        finally:
            try:
                last_message_path.unlink()
            except FileNotFoundError:
                pass
        return {
            "ok": proc.returncode == 0,
            "exitCode": proc.returncode,
            "durationMs": int((time.time() - started) * 1000),
            "cwd": str(cwd),
            "stdout": stdout,
            "stderr": stderr,
            "result": result or stdout.strip(),
            "trace": {"backend": "codex", "events": []},
            "truncated": stdout_truncated or stderr_truncated,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
        stdout, stdout_truncated = truncate(stdout, max_output_chars)
        stderr, stderr_truncated = truncate(stderr, max_output_chars)
        try:
            last_message_path.unlink()
        except FileNotFoundError:
            pass
        return {
            "ok": False,
            "exitCode": 124,
            "durationMs": int((time.time() - started) * 1000),
            "cwd": str(cwd),
            "stdout": stdout,
            "stderr": stderr + "\nCodex task timed out",
            "result": "",
            "trace": {"backend": "codex", "events": []},
            "error": "timeout",
            "truncated": stdout_truncated or stderr_truncated,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Codex as a one-shot Agent OS command.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Run a task with Codex.")
    run_parser.add_argument("--cwd", default=str(REPO_ROOT))
    run_parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    run_parser.add_argument("--model", default=os.getenv("CODEX_TASK_MODEL", ""))
    run_parser.add_argument("--sandbox", default=DEFAULT_SANDBOX)
    run_parser.add_argument("--max-output-chars", type=int, default=DEFAULT_MAX_OUTPUT_CHARS)
    run_parser.add_argument("--add-dir", action="append", default=[])
    run_parser.add_argument("--json", action="store_true")
    run_parser.add_argument("prompt", nargs=argparse.REMAINDER)

    sub.add_parser("doctor", help="Check that Codex is callable without starting a model request.")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        result = subprocess.run([str(CODEX_BIN), "--version"], text=True, capture_output=True, timeout=15)
        print(json.dumps({
            "ok": result.returncode == 0,
            "codexBin": str(CODEX_BIN),
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
        result = run_codex(
            prompt=prompt,
            cwd=Path(args.cwd).expanduser().resolve(),
            timeout=args.timeout,
            model=args.model,
            sandbox=args.sandbox,
            max_output_chars=args.max_output_chars,
            add_dirs=args.add_dir,
        )
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            if result.get("result"):
                print(result["result"])
            if result.get("stderr"):
                sys.stderr.write(result["stderr"])
                if not result["stderr"].endswith("\n"):
                    sys.stderr.write("\n")
        return 0 if result.get("ok") else int(result.get("exitCode") or 1)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
