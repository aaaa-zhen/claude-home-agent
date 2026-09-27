#!/usr/bin/env python3
"""Queue and deliver one short TTS message to a HomePod through Home Assistant."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
import secrets

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENTITY = "media_player.living_room"
DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini-tts"
DEFAULT_OPENAI_VOICE = "nova"
DEFAULT_OPENAI_INSTRUCTIONS = (
    "请使用自然、温暖的普通话，像家里人当面提醒一样；语速稍慢，停顿自然，"
    "不要使用新闻播音腔，也不要添加输入文字以外的内容。"
)
DEFAULT_LOCK_PATH = ROOT / "runtime" / "homepod-say.lock"
DEFAULT_TMP_ROOT = ROOT / "tmp" / "homepod-runtime"
MAX_MESSAGE_CHARS = 500
DECODE_ERROR_MARKERS = ("failed to init decoder", "error in read callback")


class HomePodError(RuntimeError):
    """Expected operational failure that is safe to return to the agent."""


class HAHTTPError(HomePodError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class HAClient:
    def __init__(self, api_url: str, token: str):
        self.api_url = api_url.rstrip("/")
        self.token = token
        # HA is on the LAN. Never let a desktop proxy turn a local failure into
        # a misleading 502 or expose the bearer token to that proxy.
        self.opener = build_opener(ProxyHandler({}))

    def _request(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 15,
    ) -> bytes:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener.open(req, timeout=timeout) as response:
                return response.read()
        except HTTPError as exc:
            with contextlib.suppress(Exception):
                exc.read()
            raise HAHTTPError(exc.code, f"Home Assistant returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise HomePodError(f"Home Assistant request failed: {type(exc).__name__}") from exc

    def get_json(self, path: str, timeout: float = 15) -> dict[str, Any] | list[Any]:
        raw = self._request("GET", f"{self.api_url}{path}", timeout=timeout)
        try:
            return json.loads(raw or b"null")
        except json.JSONDecodeError as exc:
            raise HomePodError("Home Assistant returned invalid JSON") from exc

    def call_service(
        self,
        domain: str,
        service: str,
        payload: dict[str, Any],
        timeout: float = 30,
    ) -> dict[str, Any] | list[Any]:
        raw = self._request(
            "POST",
            f"{self.api_url}/services/{domain}/{service}",
            payload,
            timeout,
        )
        try:
            return json.loads(raw or b"null")
        except json.JSONDecodeError as exc:
            raise HomePodError("Home Assistant returned invalid service JSON") from exc

    def get_error_log(self) -> str | None:
        if os.environ.get("HOMEPOD_DISABLE_ERROR_LOG") == "1":
            return None
        parts = urlsplit(self.api_url)
        error_url = f"{parts.scheme}://{parts.netloc}/api/error_log"
        try:
            return self._request("GET", error_url, timeout=10).decode("utf-8", "replace")
        except HomePodError:
            # Error-log access is a useful secondary signal, not a reason to
            # make an otherwise healthy playback fail.
            return None


class MediaTransferState:
    def __init__(self, media_path: Path, route_path: str):
        self.media_path = media_path
        self.route_path = route_path
        self.lock = threading.Lock()
        self.request_count = 0
        self.bytes_sent = 0
        self.completed_requests = 0

    def snapshot(self) -> tuple[int, int, int]:
        with self.lock:
            return self.request_count, self.bytes_sent, self.completed_requests


def _parse_range(value: str, size: int) -> tuple[int, int] | None:
    if not value.startswith("bytes=") or "," in value:
        return None
    spec = value[6:].strip()
    if "-" not in spec:
        return None
    start_text, end_text = spec.split("-", 1)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


def make_media_handler(state: MediaTransferState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._serve(send_body=False)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._serve(send_body=True)

        def _serve(self, send_body: bool) -> None:
            if urlsplit(self.path).path != state.route_path:
                self.send_error(HTTPStatus.NOT_FOUND)
                return

            size = state.media_path.stat().st_size
            range_header = self.headers.get("Range")
            byte_range = _parse_range(range_header, size) if range_header else None
            if range_header and byte_range is None:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            start, end = byte_range or (0, size - 1)
            length = end - start + 1
            self.send_response(HTTPStatus.PARTIAL_CONTENT if byte_range else HTTPStatus.OK)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if byte_range:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if not send_body:
                return

            sent = 0
            with state.lock:
                state.request_count += 1
            try:
                with state.media_path.open("rb") as media:
                    media.seek(start)
                    remaining = length
                    while remaining:
                        chunk = media.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        sent += len(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with state.lock:
                    state.bytes_sent += sent
                    if sent == length:
                        state.completed_requests += 1

    return Handler


class MediaServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextlib.contextmanager
def run_media_server(
    bind_ip: str, media_path: Path
) -> Iterator[tuple[str, MediaTransferState]]:
    route_path = f"/{secrets.token_urlsafe(24)}.mp3"
    state = MediaTransferState(media_path, route_path)
    server = MediaServer((bind_ip, 0), make_media_handler(state))
    thread = threading.Thread(target=server.serve_forever, name="homepod-media", daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://{bind_ip}:{port}{route_path}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@contextlib.contextmanager
def exclusive_lock(path: Path, timeout: float) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock_file:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise HomePodError("HomePod queue is busy")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def discover_lan_ip(api_url: str) -> str:
    override = os.environ.get("HOMEPOD_LAN_IP")
    if override:
        return override
    parts = urlsplit(api_url)
    if not parts.hostname:
        raise HomePodError("HA_URL has no hostname")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((parts.hostname, port))
            return str(sock.getsockname()[0])
    except OSError as exc:
        raise HomePodError("Could not determine the LAN address") from exc


def generate_edge_audio(message: str, voice: str, destination: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "edge_tts",
        "--voice",
        voice,
        "--text",
        message,
        "--write-media",
        str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=90)
    except FileNotFoundError as exc:
        raise HomePodError("edge-tts is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise HomePodError("Speech generation timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise HomePodError("Speech generation failed") from exc


def generate_openai_audio(
    message: str,
    voice: str,
    destination: Path,
    api_key: str,
    model: str,
    instructions: str,
) -> None:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise HomePodError("OpenAI SDK is not installed") from exc

    try:
        # The desktop exports both an HTTP Clash proxy and ALL_PROXY=socks5.
        # httpx otherwise selects SOCKS and fails when the optional socksio
        # package is absent.  Use the working HTTP proxy explicitly.
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        with httpx.Client(proxy=proxy, trust_env=False, timeout=90.0) as http_client:
            with OpenAI(
                api_key=api_key,
                http_client=http_client,
                max_retries=1,
            ) as client:
                with client.audio.speech.with_streaming_response.create(
                    model=model,
                    voice=voice,
                    input=message,
                    instructions=instructions,
                    response_format="mp3",
                ) as response:
                    response.stream_to_file(destination)
    except Exception as exc:
        # API exceptions can contain request details. Return only the class so
        # the Home Agent never echoes credentials or raw provider responses.
        raise HomePodError(f"OpenAI speech generation failed: {type(exc).__name__}") from exc


def generate_audio(
    message: str,
    voice: str,
    destination: Path,
    source: Path | None,
    provider: str = "",
) -> dict[str, Any]:
    if source is not None:
        if not source.is_file():
            raise HomePodError("Test audio file does not exist")
        shutil.copyfile(source, destination)
        result = {
            "provider": "fixture",
            "model": "fixture",
            "voice": "fixture",
            "warnings": [],
        }
    else:
        requested = (provider or os.environ.get("HOMEPOD_TTS_PROVIDER", "auto")).lower()
        if requested not in {"auto", "openai", "edge"}:
            raise HomePodError(f"Unsupported TTS provider: {requested}")

        openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
        openai_model = os.environ.get("HOMEPOD_OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
        openai_voice = voice or os.environ.get("HOMEPOD_OPENAI_VOICE", DEFAULT_OPENAI_VOICE)
        edge_voice = os.environ.get("HOMEPOD_EDGE_VOICE", DEFAULT_EDGE_VOICE)
        instructions = os.environ.get(
            "HOMEPOD_OPENAI_INSTRUCTIONS", DEFAULT_OPENAI_INSTRUCTIONS
        )

        # Preserve the old CLI contract: a Microsoft Neural voice explicitly
        # requested without a provider continues to use edge-tts.
        if requested == "auto":
            if voice.endswith("Neural"):
                requested = "edge"
            else:
                requested = "openai" if openai_key else "edge"

        warnings: list[str] = []
        if requested == "openai" and openai_key:
            try:
                generate_openai_audio(
                    message,
                    openai_voice,
                    destination,
                    openai_key,
                    openai_model,
                    instructions,
                )
                result = {
                    "provider": "openai",
                    "model": openai_model,
                    "voice": openai_voice,
                    "warnings": warnings,
                }
            except HomePodError:
                destination.unlink(missing_ok=True)
                if os.environ.get("HOMEPOD_TTS_FALLBACK", "edge").lower() != "edge":
                    raise
                warnings.append("openai_tts_failed_fell_back_to_edge")
                generate_edge_audio(message, edge_voice, destination)
                result = {
                    "provider": "edge",
                    "model": "edge-tts",
                    "voice": edge_voice,
                    "warnings": warnings,
                }
        elif requested == "openai":
            warnings.append("openai_api_key_missing_fell_back_to_edge")
            generate_edge_audio(message, edge_voice, destination)
            result = {
                "provider": "edge",
                "model": "edge-tts",
                "voice": edge_voice,
                "warnings": warnings,
            }
        else:
            selected_edge_voice = voice or edge_voice
            generate_edge_audio(message, selected_edge_voice, destination)
            result = {
                "provider": "edge",
                "model": "edge-tts",
                "voice": selected_edge_voice,
                "warnings": warnings,
            }
    if not destination.is_file() or destination.stat().st_size == 0:
        raise HomePodError("Speech generation produced no audio")
    return result


def audio_duration(path: Path, message: str) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return max(0.5, float(result.stdout.strip()))
    except (FileNotFoundError, ValueError, subprocess.SubprocessError):
        return max(1.0, min(60.0, len(message) * 0.22))


def new_decode_error(before: str | None, after: str | None) -> bool:
    if before is None or after is None:
        return False
    if after.startswith(before):
        delta = after[len(before) :]
        return any(marker in delta for marker in DECODE_ERROR_MARKERS)
    return any(after.count(marker) > before.count(marker) for marker in DECODE_ERROR_MARKERS)


def safe_state(client: HAClient, entity_id: str) -> dict[str, Any]:
    state = client.get_json(f"/states/{entity_id}")
    if not isinstance(state, dict) or state.get("entity_id") != entity_id:
        raise HomePodError("HomePod state response was invalid")
    if state.get("state") in {"unavailable", "unknown"}:
        raise HomePodError(f"HomePod is {state.get('state')}")
    return state


def restore_player(
    client: HAClient,
    entity_id: str,
    original_state: str,
    original_attributes: dict[str, Any],
    original_volume: float | None,
    changed_volume: bool,
) -> tuple[list[str], bool]:
    warnings: list[str] = []
    resumed = False
    if changed_volume and original_volume is not None:
        try:
            client.call_service(
                "media_player",
                "volume_set",
                {"entity_id": entity_id, "volume_level": original_volume},
            )
        except HomePodError:
            warnings.append("volume_restore_failed")
    if original_state == "playing":
        media_id = original_attributes.get("media_content_id")
        media_type = original_attributes.get("media_content_type")
        if media_id and media_type:
            try:
                client.call_service(
                    "media_player",
                    "play_media",
                    {
                        "entity_id": entity_id,
                        "media_content_id": media_id,
                        "media_content_type": media_type,
                    },
                )
                resumed = True
            except HomePodError:
                warnings.append("playback_resume_failed")
        else:
            # Calling media_play here can replay the just-finished TTS instead
            # of the previous source, so prefer an honest warning.
            warnings.append("previous_playback_not_restorable")
    return warnings, resumed


def deliver(
    message: str,
    voice: str,
    volume: float | None,
    test_audio: Path | None,
    provider: str = "",
) -> dict[str, Any]:
    if not message.strip():
        raise HomePodError("Message is empty")
    if len(message) > MAX_MESSAGE_CHARS:
        raise HomePodError(f"Message exceeds {MAX_MESSAGE_CHARS} characters")
    if volume is not None and not 0 <= volume <= 1:
        raise HomePodError("Volume must be between 0 and 1")

    load_dotenv(ROOT / ".env")
    api_url = os.environ.get("HA_URL", "")
    token = os.environ.get("HA_TOKEN", "")
    if not api_url or not token:
        raise HomePodError("HA_URL or HA_TOKEN is missing")
    entity_id = os.environ.get("HOMEPOD_ENTITY", DEFAULT_ENTITY)
    attempts_limit = max(1, min(5, int(os.environ.get("HOMEPOD_MAX_ATTEMPTS", "3"))))
    reset_delay = max(0.0, float(os.environ.get("HOMEPOD_RESET_DELAY", "1.5")))
    queue_timeout = max(1.0, float(os.environ.get("HOMEPOD_QUEUE_TIMEOUT", "120")))
    lock_path = Path(os.environ.get("HOMEPOD_LOCK_PATH", str(DEFAULT_LOCK_PATH)))
    tmp_root = Path(os.environ.get("HOMEPOD_TMP_ROOT", str(DEFAULT_TMP_ROOT)))
    tmp_root.mkdir(parents=True, exist_ok=True)

    with exclusive_lock(lock_path, queue_timeout):
        client = HAClient(api_url, token)
        original = safe_state(client, entity_id)
        original_state = str(original.get("state", "unknown"))
        original_attributes = original.get("attributes") or {}
        raw_volume = original_attributes.get("volume_level")
        original_volume = float(raw_volume) if isinstance(raw_volume, (int, float)) else None
        changed_volume = volume is not None and volume != original_volume
        warnings: list[str] = []
        restored_previous_playback = False

        with tempfile.TemporaryDirectory(prefix="say-", dir=tmp_root) as work_dir:
            media_path = Path(work_dir) / "speech.mp3"
            tts_result = generate_audio(message, voice, media_path, test_audio, provider)
            warnings.extend(tts_result.pop("warnings"))
            duration = audio_duration(media_path, message)
            bind_ip = discover_lan_ip(api_url)

            if changed_volume:
                client.call_service(
                    "media_player",
                    "volume_set",
                    {"entity_id": entity_id, "volume_level": volume},
                )

            try:
                # Reset stale RAOP sessions. If this interrupts active media,
                # restore_player makes a best-effort resume after the message.
                with contextlib.suppress(HomePodError):
                    client.call_service("media_player", "media_stop", {"entity_id": entity_id})
                if reset_delay:
                    time.sleep(reset_delay)

                with run_media_server(bind_ip, media_path) as (media_url, transfer):
                    played = False
                    confidence = ""
                    elapsed = 0.0
                    attempt = 0
                    last_reason = "no_media_fetch"
                    for attempt in range(1, attempts_limit + 1):
                        transfer_before = transfer.snapshot()
                        error_before = client.get_error_log()
                        started = time.monotonic()
                        response_status = 200
                        try:
                            client.call_service(
                                "media_player",
                                "play_media",
                                {
                                    "entity_id": entity_id,
                                    "media_content_id": media_url,
                                    "media_content_type": "music",
                                },
                                timeout=max(30.0, duration + 20.0),
                            )
                        except HAHTTPError as exc:
                            response_status = exc.status
                            if exc.status != 500:
                                raise
                        elapsed = time.monotonic() - started
                        # Give HA's logger a brief chance to append the decoder
                        # exception that accompanies a failed service response.
                        if response_status == 500:
                            time.sleep(0.2)
                        error_after = client.get_error_log()
                        transfer_after = transfer.snapshot()
                        fetched_bytes = transfer_after[1] - transfer_before[1]
                        completed = transfer_after[2] - transfer_before[2]
                        decode_failed = new_decode_error(error_before, error_after)
                        fetched_all = fetched_bytes >= media_path.stat().st_size and completed > 0

                        if decode_failed:
                            last_reason = "decoder_error"
                        elif not fetched_all:
                            last_reason = "incomplete_media_fetch"
                        elif response_status == 200:
                            played = True
                            confidence = "high"
                        elif response_status == 500 and elapsed >= min(1.0, duration * 0.5):
                            # pyatv can raise while closing an otherwise fully
                            # consumed RAOP stream. Mark it separately so callers
                            # do not confuse arbitrary HTTP 500s with success.
                            played = True
                            confidence = "medium"
                            warnings.append("home_assistant_close_error")
                        else:
                            last_reason = f"home_assistant_http_{response_status}"

                        if played:
                            break
                        if attempt < attempts_limit:
                            with contextlib.suppress(HomePodError):
                                client.call_service(
                                    "media_player", "media_stop", {"entity_id": entity_id}
                                )
                            if reset_delay:
                                time.sleep(reset_delay)

                    if not played:
                        raise HomePodError(
                            f"HomePod playback failed after {attempts_limit} attempts ({last_reason})"
                        )
            finally:
                restore_warnings, restored_previous_playback = restore_player(
                    client,
                    entity_id,
                    original_state,
                    original_attributes,
                    original_volume,
                    changed_volume,
                )
                warnings.extend(restore_warnings)

        return {
            "ok": True,
            "action": "homepod.say",
            "status": "played",
            "confidence": confidence,
            "attempts": attempt,
            "duration_seconds": round(duration, 2),
            "elapsed_seconds": round(elapsed, 2),
            "restored_previous_playback": restored_previous_playback,
            "tts_provider": tts_result["provider"],
            "tts_model": tts_result["model"],
            "tts_voice": tts_result["voice"],
            "warnings": sorted(set(warnings)),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="让客厅 HomePod 播放一条短语音")
    parser.add_argument("message")
    parser.add_argument("--provider", choices=["auto", "openai", "edge"], default="")
    parser.add_argument("--voice", default="")
    parser.add_argument("--volume", type=float, default=None)
    parser.add_argument("--audio-file", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = deliver(args.message, args.voice, args.volume, args.audio_file, args.provider)
    except (HomePodError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "action": "homepod.say",
                    "status": "failed",
                    "error": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
