#!/usr/bin/env python3
"""Run a command while holding a non-blocking advisory file lock."""

import fcntl
import subprocess
import sys


if len(sys.argv) < 3:
    print(f"usage: {sys.argv[0]} LOCK_FILE COMMAND [ARG ...]", file=sys.stderr)
    sys.exit(64)

lock_path = sys.argv[1]
cmd = sys.argv[2:]

with open(lock_path, "w", encoding="utf-8") as lock_fd:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("already-running")
        sys.exit(2)

    proc = subprocess.run(cmd, check=False)
    sys.exit(proc.returncode)
