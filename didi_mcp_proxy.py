#!/usr/bin/env python3
"""Stdio MCP proxy for Didi MCP (streamable HTTP -> stdio)."""
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

DIDI_URL = os.getenv("DIDI_MCP_URL")
if not DIDI_URL:
    key = os.getenv("DIDI_MCP_KEY")
    if key:
        DIDI_URL = f"https://mcp.didichuxing.com/mcp-servers?key={key}"
if not DIDI_URL:
    raise RuntimeError("DIDI_MCP_URL or DIDI_MCP_KEY must be set in .env")

_session_id = None
_http = requests.Session()


def send(msg):
    line = json.dumps(msg, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def call_didi(payload):
    global _session_id
    headers = {"Content-Type": "application/json"}
    if _session_id:
        headers["mcp-session-id"] = _session_id
    response = _http.post(DIDI_URL, json=payload, headers=headers, timeout=30)
    response.raise_for_status()
    sid = response.headers.get("mcp-session-id")
    if sid:
        _session_id = sid
    return response.json()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method", "")
        msg_id = msg.get("id")

        if method.startswith("notifications/"):
            try:
                call_didi(msg)
            except Exception:
                pass
            continue

        if msg_id is not None:
            try:
                result = call_didi(msg)
                send(result)
            except Exception as exc:
                send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -1, "message": str(exc)}})


if __name__ == "__main__":
    main()
