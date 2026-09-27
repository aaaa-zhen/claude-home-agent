#!/usr/bin/env python3
"""Push a notification to the Android app queue.
Usage:
  python3 app_notify.py "title" "body" [type] [--eta EPOCH_MS]
  
Can also be imported: from app_notify import notify
"""
import json
import time
import sys
import threading
from pathlib import Path

NOTIFICATIONS_FILE = Path(__file__).resolve().parents[1] / "notifications.json"
_lock = threading.Lock()


def notify(title, body, msg_type="general", eta=None):
    with _lock:
        items = []
        if NOTIFICATIONS_FILE.exists():
            try:
                items = json.loads(NOTIFICATIONS_FILE.read_text())
            except Exception:
                items = []
        item = {
            "id": f"{int(time.time()*1000)}",
            "type": msg_type,
            "title": title,
            "body": body,
            "timestamp": int(time.time() * 1000),
        }
        if eta is not None:
            item["eta"] = int(eta)
        items.append(item)
        items = items[-200:]
        NOTIFICATIONS_FILE.write_text(json.dumps(items, ensure_ascii=False))


if __name__ == "__main__":
    args = sys.argv[1:]
    eta = None
    if "--eta" in args:
        idx = args.index("--eta")
        eta = args[idx + 1]
        args = args[:idx] + args[idx+2:]
    
    if len(args) >= 2:
        notify(args[0], args[1], args[2] if len(args) > 2 else "general", eta=eta)
        print(f"Notification queued: {args[0]}")
    else:
        print("Usage: python3 app_notify.py <title> <body> [type] [--eta EPOCH_MS]")
