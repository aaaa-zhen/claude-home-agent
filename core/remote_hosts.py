#!/usr/bin/env python3
"""Remote workstation registry and SSH executor for the home agent."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import stat
import sys
import time
import fcntl
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paramiko


SCRIPT_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.getenv("WEIXIN_AGENT_RUNTIME_DIR", Path(__file__).resolve().parents[1] / "runtime"))
STORE_PATH = Path(os.getenv("REMOTE_HOSTS_STORE", RUNTIME_DIR / "remote-hosts.json"))
STORE_LOCK_PATH = Path(os.getenv("REMOTE_HOSTS_LOCK", str(STORE_PATH) + ".lock"))
CONNECT_TOKEN_PATH = Path(os.getenv("REMOTE_CONNECT_TOKEN_FILE", RUNTIME_DIR / "remote-connect-token"))
DEFAULT_PUBLIC_BASE_URL = os.getenv("REMOTE_PUBLIC_BASE_URL", "https://your-api-domain.example.com")
CONNECTOR_ONLINE_SECONDS = int(os.getenv("REMOTE_CONNECTOR_ONLINE_SECONDS", "90"))
MAX_CONNECTOR_JOBS = 100
MAX_PUBLIC_JOBS = int(os.getenv("REMOTE_PUBLIC_JOBS", "10"))
MAX_STORED_OUTPUT_CHARS = int(os.getenv("REMOTE_MAX_STORED_OUTPUT_CHARS", "4000"))
MAX_STORED_ERROR_CHARS = int(os.getenv("REMOTE_MAX_STORED_ERROR_CHARS", "2000"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iso_to_epoch(value: str) -> float:
    if not value:
        return 0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0


def is_connector_online(profile: dict[str, Any]) -> bool:
    last_seen = iso_to_epoch(str(profile.get("lastHeartbeatAt") or profile.get("lastConnectedAt") or ""))
    return last_seen > 0 and (time.time() - last_seen) <= CONNECTOR_ONLINE_SECONDS


def ensure_runtime_dir() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(RUNTIME_DIR, stat.S_IRWXU)
    except OSError:
        pass


@contextmanager
def store_lock(*, exclusive: bool):
    ensure_runtime_dir()
    with STORE_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def slug_alias(value: str) -> str:
    alias = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-._").lower()
    if not alias:
        raise ValueError("alias is required")
    return alias[:64]


def _read_store_unlocked() -> dict[str, Any]:
    if not STORE_PATH.exists():
        return {"schemaVersion": 1, "hosts": {}}
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"schemaVersion": 1, "hosts": {}}
    data.setdefault("schemaVersion", 1)
    data.setdefault("hosts", {})
    return data


def read_store() -> dict[str, Any]:
    with store_lock(exclusive=False):
        return _read_store_unlocked()


def _write_store_unlocked(data: dict[str, Any]) -> None:
    ensure_runtime_dir()
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, STORE_PATH)
    try:
        os.chmod(STORE_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def write_store(data: dict[str, Any]) -> None:
    with store_lock(exclusive=True):
        _write_store_unlocked(data)


def update_store(mutator):
    with store_lock(exclusive=True):
        data = _read_store_unlocked()
        result = mutator(data)
        _write_store_unlocked(data)
        return result


def get_connect_token() -> str:
    env_token = os.getenv("REMOTE_CONNECT_TOKEN", "").strip()
    if env_token:
        return env_token
    ensure_runtime_dir()
    if CONNECT_TOKEN_PATH.exists():
        token = CONNECT_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    CONNECT_TOKEN_PATH.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(CONNECT_TOKEN_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return token


def clip_text(value: Any, *, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def public_job_summary(job: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "id",
        "status",
        "createdAt",
        "startedAt",
        "completedAt",
        "exitCode",
        "durationMs",
        "timeout",
    )
    safe = {key: job[key] for key in allowed if key in job}
    if job.get("error"):
        safe["error"] = clip_text(job["error"], limit=240)
    return safe


def mask_host(profile: dict[str, Any]) -> dict[str, Any]:
    safe = deepcopy(profile)
    if safe.get("password"):
        safe["password"] = "***"
    safe.pop("connectorToken", None)
    if isinstance(safe.get("jobs"), list):
        safe["jobs"] = [public_job_summary(job) for job in safe["jobs"][-MAX_PUBLIC_JOBS:] if isinstance(job, dict)]
    return safe


def host_summary(profile: dict[str, Any]) -> str:
    mode = profile.get("mode") or "ssh"
    if mode == "connector":
        user = profile.get("username") or "?"
        host = profile.get("hostname") or profile.get("host") or "connector"
        status = profile.get("lastStatus") or "unknown"
        return f"{profile.get('alias')} {status} connector {user}@{host}"
    user = profile.get("username") or "?"
    host = profile.get("host") or "?"
    port = profile.get("port") or 22
    status = profile.get("lastStatus") or "unknown"
    return f"{profile.get('alias')} {status} {user}@{host}:{port}"


def add_or_update_host(
    *,
    alias: str,
    host: str,
    username: str,
    port: int = 22,
    password: str = "",
    key_path: str = "",
    workdir: str = "",
    note: str = "",
    source: str = "cli",
) -> dict[str, Any]:
    alias = slug_alias(alias)
    host = host.strip()
    username = username.strip()
    key_path = key_path.strip()
    workdir = workdir.strip()
    note = note.strip()
    if not host:
        raise ValueError("host is required")
    if not username:
        raise ValueError("username is required")
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        existing = data["hosts"].get(alias, {})
        now = utc_now()
        profile = {
            **existing,
            "alias": alias,
            "mode": "ssh",
            "host": host,
            "port": int(port),
            "username": username,
            "keyPath": key_path,
            "workdir": workdir,
            "note": note,
            "source": source,
            "createdAt": existing.get("createdAt") or now,
            "updatedAt": now,
            "lastStatus": existing.get("lastStatus") or "unknown",
            "lastConnectedAt": existing.get("lastConnectedAt") or "",
            "lastError": existing.get("lastError") or "",
        }
        if password:
            profile["password"] = password
        elif "password" in existing:
            profile["password"] = existing["password"]

        data["hosts"][alias] = profile
        return profile

    return mask_host(update_store(mutate))


def register_connector(
    *,
    alias: str,
    hostname: str = "",
    username: str = "",
    platform_name: str = "",
    cwd: str = "",
    note: str = "",
    source: str = "connector",
) -> dict[str, Any]:
    alias = slug_alias(alias)
    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        existing = data["hosts"].get(alias, {})
        now = utc_now()
        connector_token = existing.get("connectorToken") or secrets.token_urlsafe(32)
        jobs = existing.get("jobs") if isinstance(existing.get("jobs"), list) else []
        profile = {
            **existing,
            "alias": alias,
            "mode": "connector",
            "hostname": hostname.strip(),
            "username": username.strip(),
            "platform": platform_name.strip(),
            "workdir": cwd.strip() or existing.get("workdir", ""),
            "note": note.strip() or existing.get("note", ""),
            "source": source,
            "connectorToken": connector_token,
            "createdAt": existing.get("createdAt") or now,
            "updatedAt": now,
            "lastHeartbeatAt": now,
            "lastConnectedAt": now,
            "lastStatus": "online",
            "lastError": "",
            "jobs": jobs[-MAX_CONNECTOR_JOBS:],
        }
        data["hosts"][alias] = profile
        return profile

    profile = update_store(mutate)
    connector_token = profile["connectorToken"]
    return {"host": mask_host(profile), "connectorToken": connector_token}


def forget_host(alias: str) -> bool:
    alias = slug_alias(alias)
    def mutate(data: dict[str, Any]) -> bool:
        existed = alias in data["hosts"]
        data["hosts"].pop(alias, None)
        return existed

    return update_store(mutate)


def get_host(alias: str, *, include_secrets: bool = False) -> dict[str, Any]:
    alias = slug_alias(alias)
    profile = read_store()["hosts"].get(alias)
    if not profile:
        raise KeyError(f"remote host not found: {alias}")
    return profile if include_secrets else mask_host(profile)


def list_hosts(*, include_secrets: bool = False) -> list[dict[str, Any]]:
    def refresh(data: dict[str, Any]) -> list[dict[str, Any]]:
        for profile in data["hosts"].values():
            if profile.get("mode") == "connector":
                online = is_connector_online(profile)
                status_text = "online" if online else "offline"
                if profile.get("lastStatus") != status_text:
                    profile["lastStatus"] = status_text
                    profile["updatedAt"] = utc_now()
        return list(data["hosts"].values())

    data_hosts = update_store(refresh)
    hosts = data_hosts
    hosts.sort(key=lambda item: str(item.get("updatedAt") or item.get("createdAt") or ""), reverse=True)
    if include_secrets:
        return hosts
    return [mask_host(item) for item in hosts]


def _connect(profile: dict[str, Any], *, timeout: int = 15) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    kwargs: dict[str, Any] = {
        "hostname": profile["host"],
        "port": int(profile.get("port") or 22),
        "username": profile["username"],
        "timeout": timeout,
        "banner_timeout": timeout,
        "auth_timeout": timeout,
    }
    if profile.get("password"):
        kwargs["password"] = profile["password"]
        kwargs["look_for_keys"] = False
        kwargs["allow_agent"] = False
    elif profile.get("keyPath"):
        kwargs["key_filename"] = os.path.expanduser(profile["keyPath"])
        kwargs["look_for_keys"] = False
    else:
        kwargs["look_for_keys"] = True
        kwargs["allow_agent"] = True

    client.connect(**kwargs)
    return client


def update_status(alias: str, *, status_text: str, error: str = "") -> None:
    def mutate(data: dict[str, Any]) -> None:
        profile = data["hosts"].get(slug_alias(alias))
        if not profile:
            return
        profile["lastStatus"] = status_text
        profile["lastCheckedAt"] = utc_now()
        profile["lastError"] = clip_text(error, limit=1000)
        if status_text == "online":
            profile["lastConnectedAt"] = profile["lastCheckedAt"]

    update_store(mutate)


def test_host(alias: str, *, timeout: int = 15) -> dict[str, Any]:
    profile = get_host(alias, include_secrets=True)
    if profile.get("mode") == "connector":
        online = is_connector_online(profile)
        update_status(alias, status_text="online" if online else "offline", error="" if online else "connector heartbeat is stale")
        return {
            "ok": online,
            "alias": profile["alias"],
            "error": "" if online else "connector heartbeat is stale",
            "host": mask_host(get_host(alias, include_secrets=True)),
        }
    started = time.time()
    try:
        client = _connect(profile, timeout=timeout)
        client.close()
        duration_ms = int((time.time() - started) * 1000)
        update_status(alias, status_text="online")
        return {"ok": True, "alias": profile["alias"], "durationMs": duration_ms, "host": mask_host(profile)}
    except Exception as exc:
        error = str(exc)
        update_status(alias, status_text="offline", error=error)
        return {"ok": False, "alias": profile["alias"], "error": error, "host": mask_host(profile)}


def run_command(alias: str, command: str, *, cwd: str = "", timeout: int = 60) -> dict[str, Any]:
    if not command.strip():
        raise ValueError("command is required")
    profile = get_host(alias, include_secrets=True)
    if profile.get("mode") == "connector":
        return run_connector_command(alias, command, cwd=cwd, timeout=timeout)
    workdir = cwd.strip() or profile.get("workdir", "").strip()
    remote_command = command
    if workdir:
        remote_command = f"cd {shlex.quote(workdir)} && {command}"

    started = time.time()
    try:
        client = _connect(profile, timeout=min(timeout, 30))
        _stdin, stdout, stderr = client.exec_command(remote_command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        client.close()
        duration_ms = int((time.time() - started) * 1000)
        update_status(alias, status_text="online")
        return {
            "ok": exit_code == 0,
            "alias": profile["alias"],
            "exitCode": exit_code,
            "durationMs": duration_ms,
            "stdout": out,
            "stderr": err,
        }
    except Exception as exc:
        error = str(exc)
        update_status(alias, status_text="offline", error=error)
        return {"ok": False, "alias": profile["alias"], "error": error, "stdout": "", "stderr": ""}


def validate_connector(alias: str, connector_token: str) -> dict[str, Any]:
    profile = get_host(alias, include_secrets=True)
    if profile.get("mode") != "connector":
        raise ValueError(f"{alias} is not a connector host")
    if not connector_token or connector_token != profile.get("connectorToken"):
        raise PermissionError("invalid connector token")
    return profile


def connector_heartbeat(alias: str, connector_token: str) -> dict[str, Any]:
    profile = validate_connector(alias, connector_token)
    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        current = data["hosts"].get(profile["alias"])
        if not current:
            raise KeyError(f"remote host not found: {alias}")
        now = utc_now()
        current["lastHeartbeatAt"] = now
        current["lastConnectedAt"] = now
        current["lastStatus"] = "online"
        current["lastError"] = ""
        current["updatedAt"] = now
        return current

    return mask_host(update_store(mutate))


def _jobs(profile: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = profile.get("jobs")
    if not isinstance(jobs, list):
        jobs = []
        profile["jobs"] = jobs
    return jobs


def queue_connector_job(alias: str, command: str, *, cwd: str = "", timeout: int = 60) -> dict[str, Any]:
    alias = slug_alias(alias)
    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        profile = data["hosts"].get(alias)
        if not profile:
            raise KeyError(f"remote host not found: {alias}")
        if profile.get("mode") != "connector":
            raise ValueError(f"{alias} is not a connector host")
        job = {
            "id": secrets.token_hex(8),
            "status": "queued",
            "command": command,
            "cwd": cwd.strip() or profile.get("workdir", ""),
            "timeout": int(timeout),
            "createdAt": utc_now(),
        }
        jobs = _jobs(profile)
        jobs.append(job)
        profile["jobs"] = jobs[-MAX_CONNECTOR_JOBS:]
        profile["updatedAt"] = utc_now()
        return job

    return update_store(mutate)


def connector_poll(alias: str, connector_token: str) -> dict[str, Any]:
    profile = validate_connector(alias, connector_token)
    def mutate(data: dict[str, Any]) -> dict[str, Any] | None:
        current = data["hosts"].get(profile["alias"])
        if not current:
            raise KeyError(f"remote host not found: {alias}")
        now = utc_now()
        current["lastHeartbeatAt"] = now
        current["lastConnectedAt"] = now
        current["lastStatus"] = "online"
        current["lastError"] = ""
        current["updatedAt"] = now
        for job in _jobs(current):
            if job.get("status") == "queued":
                job["status"] = "running"
                job["startedAt"] = now
                return {
                    "id": job["id"],
                    "command": job["command"],
                    "cwd": job.get("cwd", ""),
                    "timeout": job.get("timeout", 60),
                }
        return None

    return {"ok": True, "job": update_store(mutate)}


def connector_result(alias: str, connector_token: str, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
    profile = validate_connector(alias, connector_token)
    def mutate(data: dict[str, Any]) -> dict[str, bool]:
        current = data["hosts"].get(profile["alias"])
        if not current:
            raise KeyError(f"remote host not found: {alias}")
        now = utc_now()
        for job in _jobs(current):
            if job.get("id") == job_id:
                job["status"] = "completed"
                job["completedAt"] = now
                job["exitCode"] = int(result.get("exitCode", 1))
                job["stdout"] = clip_text(result.get("stdout", ""), limit=MAX_STORED_OUTPUT_CHARS)
                job["stderr"] = clip_text(result.get("stderr", ""), limit=MAX_STORED_ERROR_CHARS)
                job["error"] = clip_text(result.get("error", ""), limit=MAX_STORED_ERROR_CHARS)
                job["durationMs"] = int(result.get("durationMs", 0) or 0)
                current["lastHeartbeatAt"] = now
                current["lastConnectedAt"] = now
                current["lastStatus"] = "online"
                current["updatedAt"] = now
                return {"ok": True}
        raise KeyError(f"job not found: {job_id}")

    return update_store(mutate)


def get_connector_job(alias: str, job_id: str) -> dict[str, Any] | None:
    profile = get_host(alias, include_secrets=True)
    for job in _jobs(profile):
        if job.get("id") == job_id:
            return job
    return None


def mark_connector_job_timeout(alias: str, job_id: str) -> None:
    alias = slug_alias(alias)
    def mutate(data: dict[str, Any]) -> None:
        profile = data["hosts"].get(alias)
        if not profile:
            return
        for job in _jobs(profile):
            if job.get("id") == job_id and job.get("status") in {"queued", "running"}:
                job["status"] = "timeout"
                job["completedAt"] = utc_now()
                job["exitCode"] = 124
                job["stderr"] = "connector job timed out"
                break

    update_store(mutate)


def run_connector_command(alias: str, command: str, *, cwd: str = "", timeout: int = 60) -> dict[str, Any]:
    online = test_host(alias).get("ok")
    if not online:
        return {
            "ok": False,
            "alias": slug_alias(alias),
            "error": "connector is offline; open the connect page and run the connector script first",
            "stdout": "",
            "stderr": "",
        }
    job = queue_connector_job(alias, command, cwd=cwd, timeout=timeout)
    deadline = time.time() + max(timeout, 1) + 10
    while time.time() < deadline:
        current = get_connector_job(alias, job["id"])
        if current and current.get("status") == "completed":
            exit_code = int(current.get("exitCode", 1))
            return {
                "ok": exit_code == 0,
                "alias": slug_alias(alias),
                "exitCode": exit_code,
                "durationMs": int(current.get("durationMs", 0) or 0),
                "stdout": str(current.get("stdout", "")),
                "stderr": str(current.get("stderr", "")),
                "error": str(current.get("error", "")),
            }
        time.sleep(0.5)
    mark_connector_job_timeout(alias, job["id"])
    return {
        "ok": False,
        "alias": slug_alias(alias),
        "exitCode": 124,
        "error": "connector job timed out",
        "stdout": "",
        "stderr": "connector job timed out",
    }


def connect_url(base_url: str = DEFAULT_PUBLIC_BASE_URL) -> str:
    base = str(base_url or DEFAULT_PUBLIC_BASE_URL).rstrip("/")
    return f"{base}/remote/connect?k={get_connect_token()}"


def print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage remote workstations for the home agent.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="List registered remote hosts.")
    list_parser.add_argument("--json", action="store_true")
    list_parser.add_argument("--status", action="store_true", help="Test each host before listing.")

    add_parser = sub.add_parser("add", help="Add or update a remote host.")
    add_parser.add_argument("--alias", required=True)
    add_parser.add_argument("--host", required=True)
    add_parser.add_argument("--port", type=int, default=22)
    add_parser.add_argument("--user", "--username", dest="username", required=True)
    add_parser.add_argument("--password", default="")
    add_parser.add_argument("--key-path", default="")
    add_parser.add_argument("--workdir", default="")
    add_parser.add_argument("--note", default="")
    add_parser.add_argument("--json", action="store_true")
    add_parser.add_argument("--test", action="store_true")

    test_parser = sub.add_parser("test", help="Test SSH connectivity.")
    test_parser.add_argument("alias")
    test_parser.add_argument("--json", action="store_true")

    run_parser = sub.add_parser("run", help="Run a shell command on a remote host.")
    run_parser.add_argument("alias")
    run_parser.add_argument("--cwd", default="")
    run_parser.add_argument("--timeout", type=int, default=60)
    run_parser.add_argument("--json", action="store_true")
    run_parser.add_argument("remote_command", nargs=argparse.REMAINDER)

    forget_parser = sub.add_parser("forget", help="Remove a remote host.")
    forget_parser.add_argument("alias")
    forget_parser.add_argument("--json", action="store_true")

    url_parser = sub.add_parser("connect-url", help="Print the web connect URL.")
    url_parser.add_argument("--base-url", default=DEFAULT_PUBLIC_BASE_URL)
    url_parser.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "list":
        if args.status:
            for profile in list_hosts(include_secrets=True):
                test_host(profile["alias"])
        hosts = list_hosts()
        if args.json:
            print_json({"ok": True, "hosts": hosts})
        else:
            if not hosts:
                print("No remote hosts registered.")
            for profile in hosts:
                print(host_summary(profile))
        return 0

    if args.command == "add":
        profile = add_or_update_host(
            alias=args.alias,
            host=args.host,
            username=args.username,
            port=args.port,
            password=args.password,
            key_path=args.key_path,
            workdir=args.workdir,
            note=args.note,
        )
        result: dict[str, Any] = {"ok": True, "host": profile}
        if args.test:
            result["test"] = test_host(profile["alias"])
        if args.json:
            print_json(result)
        else:
            print(host_summary(profile))
            if "test" in result:
                print("test: ok" if result["test"].get("ok") else f"test: {result['test'].get('error')}")
        return 0 if result.get("test", {"ok": True}).get("ok") else 1

    if args.command == "test":
        result = test_host(args.alias)
        if args.json:
            print_json(result)
        else:
            print("ok" if result["ok"] else f"failed: {result.get('error')}")
        return 0 if result["ok"] else 1

    if args.command == "run":
        command_parts = args.remote_command
        if command_parts and command_parts[0] == "--":
            command_parts = command_parts[1:]
        command = " ".join(command_parts).strip()
        result = run_command(args.alias, command, cwd=args.cwd, timeout=args.timeout)
        if args.json:
            print_json(result)
        else:
            if result.get("stdout"):
                sys.stdout.write(result["stdout"])
            if result.get("stderr"):
                sys.stderr.write(result["stderr"])
        return int(result.get("exitCode", 1)) if result.get("ok") is not True else 0

    if args.command == "forget":
        removed = forget_host(args.alias)
        result = {"ok": True, "removed": removed, "alias": slug_alias(args.alias)}
        if args.json:
            print_json(result)
        else:
            print("removed" if removed else "not found")
        return 0

    if args.command == "connect-url":
        url = connect_url(args.base_url)
        if args.json:
            print_json({"ok": True, "url": url})
        else:
            print(url)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
