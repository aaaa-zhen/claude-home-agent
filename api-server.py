#!/usr/bin/env python3
"""Thin API server for the Android app: serves the notification queue for polling-based push."""

import json
import os
import pathlib
import time
import threading
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

API_TOKEN = os.getenv("APP_API_TOKEN")
if not API_TOKEN:
    raise SystemExit("APP_API_TOKEN is required")
API_HOST = os.getenv("APP_API_HOST", "127.0.0.1")
PORT = 8081
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
NOTIFICATIONS_FILE = SCRIPT_DIR / "notifications.json"
_lock = threading.Lock()


def load_notifications():
    if NOTIFICATIONS_FILE.exists():
        try:
            return json.loads(NOTIFICATIONS_FILE.read_text())
        except Exception:
            return []
    return []


def save_notifications(items):
    NOTIFICATIONS_FILE.write_text(json.dumps(items, ensure_ascii=False))


def add_notification(title, body, msg_type="general"):
    """Called by monitor.py or other scripts to queue a notification."""
    with _lock:
        items = load_notifications()
        items.append({
            "id": f"{int(time.time()*1000)}",
            "type": msg_type,
            "title": title,
            "body": body,
            "timestamp": int(time.time() * 1000),
        })
        # keep max 200
        items = items[-200:]
        save_notifications(items)


class Handler(BaseHTTPRequestHandler):
    def _auth_ok(self):
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {API_TOKEN}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return False
        return True

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def do_GET(self):
        if not self._auth_ok():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/notifications":
            self._get_notifications(parsed)
        elif parsed.path == "/health":
            self._json_response(200, {"status": "ok"})
        else:
            self._json_response(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth_ok():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/notifications/clear":
            with _lock:
                save_notifications([])
            self._json_response(200, {"status": "cleared"})
        else:
            self._json_response(404, {"error": "not found"})

    def _get_notifications(self, parsed):
        params = parse_qs(parsed.query)
        since = int(params.get("since", ["0"])[0])
        with _lock:
            items = load_notifications()
        if since > 0:
            items = [i for i in items if i.get("timestamp", 0) > since]
        self._json_response(200, {"items": items})

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    server = HTTPServer((API_HOST, PORT), Handler)
    print(f"API server listening on {API_HOST}:{PORT}")
    server.serve_forever()
