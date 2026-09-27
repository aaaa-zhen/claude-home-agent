#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "homepod-say.py"
SPEC = importlib.util.spec_from_file_location("homepod_say", SCRIPT)
assert SPEC and SPEC.loader
HOMEPOD_SAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOMEPOD_SAY)


class FakeHAState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.player_state = "idle"
        self.attributes = {"volume_level": 0.7, "friendly_name": "Living Room"}
        self.fetch_tts = True
        self.play_delay = 0.15
        self.calls: list[tuple[str, dict]] = []
        self.received_audio: list[bytes] = []
        self.active_play_calls = 0
        self.max_active_play_calls = 0


class FakeHAHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def fake(self) -> FakeHAState:
        return self.server.fake_state  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/states/media_player.living_room":
            self._json(
                200,
                {
                    "entity_id": "media_player.living_room",
                    "state": self.fake.player_state,
                    "attributes": self.fake.attributes,
                },
            )
            return
        if self.path == "/api/error_log":
            body = b""
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        service = self.path.rsplit("/", 1)[-1]
        with self.fake.lock:
            self.fake.calls.append((service, payload))

        if service == "play_media" and str(payload.get("media_content_id", "")).startswith("http"):
            with self.fake.lock:
                self.fake.active_play_calls += 1
                self.fake.max_active_play_calls = max(
                    self.fake.max_active_play_calls, self.fake.active_play_calls
                )
            try:
                if self.fake.fetch_tts:
                    opener = build_opener(ProxyHandler({}))
                    with opener.open(payload["media_content_id"], timeout=5) as response:
                        audio = response.read()
                    with self.fake.lock:
                        self.fake.received_audio.append(audio)
                time.sleep(self.fake.play_delay)
            finally:
                with self.fake.lock:
                    self.fake.active_play_calls -= 1

        self._json(200, [])


class FakeHAServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), FakeHAHandler)
        self.fake_state = FakeHAState()


class HomePodSayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="homepod-say-test-")
        self.tmp_path = Path(self.tmp.name)
        self.audio = self.tmp_path / "fixture.mp3"
        self.audio.write_bytes(b"ID3" + bytes(range(256)) * 8)
        self.ha = FakeHAServer()
        self.ha_thread = threading.Thread(target=self.ha.serve_forever, daemon=True)
        self.ha_thread.start()

    def tearDown(self) -> None:
        self.ha.shutdown()
        self.ha.server_close()
        self.ha_thread.join(timeout=3)
        self.tmp.cleanup()

    def command(self, *extra: str) -> tuple[list[str], dict[str, str]]:
        command = [
            sys.executable,
            str(SCRIPT),
            "测试语音",
            "--audio-file",
            str(self.audio),
            *extra,
        ]
        env = os.environ.copy()
        env.update(
            {
                "HA_URL": f"http://127.0.0.1:{self.ha.server_address[1]}/api",
                "HA_TOKEN": "test-token",
                "HOMEPOD_LAN_IP": "127.0.0.1",
                "HOMEPOD_RESET_DELAY": "0",
                "HOMEPOD_MAX_ATTEMPTS": "1",
                "HOMEPOD_DISABLE_ERROR_LOG": "1",
                "HOMEPOD_TMP_ROOT": str(self.tmp_path / "runtime"),
                "HOMEPOD_LOCK_PATH": str(self.tmp_path / "homepod.lock"),
            }
        )
        return command, env

    def run_command(self, *extra: str) -> subprocess.CompletedProcess[str]:
        command, env = self.command(*extra)
        return subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)

    def test_delivers_audio_and_removes_per_request_files(self) -> None:
        result = self.run_command()
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "played")
        self.assertEqual(payload["confidence"], "high")
        self.assertEqual(self.ha.fake_state.received_audio, [self.audio.read_bytes()])
        runtime = self.tmp_path / "runtime"
        self.assertEqual(list(runtime.iterdir()), [])

    def test_does_not_claim_success_when_ha_never_fetches_media(self) -> None:
        self.ha.fake_state.fetch_tts = False
        result = self.run_command()
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stderr)
        self.assertFalse(payload["ok"])
        self.assertIn("incomplete_media_fetch", payload["error"])

    def test_concurrent_calls_are_serialized(self) -> None:
        self.ha.fake_state.play_delay = 0.3
        command, env = self.command()
        first = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        first_out, first_err = first.communicate(timeout=10)
        second_out, second_err = second.communicate(timeout=10)
        self.assertEqual(first.returncode, 0, first_err)
        self.assertEqual(second.returncode, 0, second_err)
        self.assertTrue(json.loads(first_out)["ok"])
        self.assertTrue(json.loads(second_out)["ok"])
        self.assertEqual(self.ha.fake_state.max_active_play_calls, 1)

    def test_restores_volume_and_known_previous_media(self) -> None:
        self.ha.fake_state.player_state = "playing"
        self.ha.fake_state.attributes = {
            "volume_level": 0.4,
            "media_content_id": "original://track/42",
            "media_content_type": "music",
        }
        result = self.run_command("--volume", "0.8")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["restored_previous_playback"])
        volume_values = [
            call[1]["volume_level"]
            for call in self.ha.fake_state.calls
            if call[0] == "volume_set"
        ]
        self.assertEqual(volume_values, [0.8, 0.4])
        play_targets = [
            call[1]["media_content_id"]
            for call in self.ha.fake_state.calls
            if call[0] == "play_media"
        ]
        self.assertEqual(play_targets[-1], "original://track/42")

    def test_media_server_has_no_listing_and_supports_suffix_range(self) -> None:
        opener = build_opener(ProxyHandler({}))
        with HOMEPOD_SAY.run_media_server("127.0.0.1", self.audio) as (url, _state):
            parts = url.rsplit("/", 1)
            with self.assertRaises(HTTPError) as root_error:
                opener.open(parts[0] + "/", timeout=3)
            self.assertEqual(root_error.exception.code, 404)

            request = Request(url, headers={"Range": "bytes=-4"})
            with opener.open(request, timeout=3) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), self.audio.read_bytes()[-4:])
                self.assertTrue(response.headers["Content-Range"].endswith(f"/{self.audio.stat().st_size}"))

            invalid = Request(url, headers={"Range": "bytes=999999-"})
            with self.assertRaises(HTTPError) as range_error:
                opener.open(invalid, timeout=3)
            self.assertEqual(range_error.exception.code, 416)

    def test_openai_is_primary_and_reports_provider_metadata(self) -> None:
        destination = self.tmp_path / "openai.mp3"

        def fake_openai(_message, _voice, path, _key, _model, _instructions):
            path.write_bytes(b"openai-audio")

        with (
            patch.dict(
                os.environ,
                {
                    "OPENAI_API_KEY": "test-key",
                    "HOMEPOD_OPENAI_MODEL": "gpt-4o-mini-tts",
                    "HOMEPOD_OPENAI_VOICE": "marin",
                },
                clear=False,
            ),
            patch.object(HOMEPOD_SAY, "generate_openai_audio", side_effect=fake_openai),
        ):
            result = HOMEPOD_SAY.generate_audio("你好", "", destination, None, "openai")

        self.assertEqual(destination.read_bytes(), b"openai-audio")
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["model"], "gpt-4o-mini-tts")
        self.assertEqual(result["voice"], "marin")
        self.assertEqual(result["warnings"], [])

    def test_openai_failure_falls_back_to_edge_and_reports_warning(self) -> None:
        destination = self.tmp_path / "fallback.mp3"

        def fake_edge(_message, _voice, path):
            path.write_bytes(b"edge-audio")

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False),
            patch.object(
                HOMEPOD_SAY,
                "generate_openai_audio",
                side_effect=HOMEPOD_SAY.HomePodError("provider unavailable"),
            ),
            patch.object(HOMEPOD_SAY, "generate_edge_audio", side_effect=fake_edge),
        ):
            result = HOMEPOD_SAY.generate_audio("你好", "", destination, None, "openai")

        self.assertEqual(destination.read_bytes(), b"edge-audio")
        self.assertEqual(result["provider"], "edge")
        self.assertIn("openai_tts_failed_fell_back_to_edge", result["warnings"])

    def test_legacy_neural_voice_keeps_using_edge_provider(self) -> None:
        destination = self.tmp_path / "legacy.mp3"

        def fake_edge(_message, _voice, path):
            path.write_bytes(b"legacy-edge-audio")

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False),
            patch.object(HOMEPOD_SAY, "generate_openai_audio") as openai_audio,
            patch.object(HOMEPOD_SAY, "generate_edge_audio", side_effect=fake_edge),
        ):
            result = HOMEPOD_SAY.generate_audio(
                "你好", "zh-CN-XiaoxiaoNeural", destination, None, "auto"
            )

        openai_audio.assert_not_called()
        self.assertEqual(result["provider"], "edge")
        self.assertEqual(result["voice"], "zh-CN-XiaoxiaoNeural")


if __name__ == "__main__":
    unittest.main()
