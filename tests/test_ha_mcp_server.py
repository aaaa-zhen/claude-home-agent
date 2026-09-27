#!/usr/bin/env python3

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import ha_mcp_server


class HomeAssistantMCPTests(unittest.TestCase):
    def test_homepod_tools_are_registered(self) -> None:
        tools = ha_mcp_server.mcp._tool_manager._tools
        self.assertIn("homepod_status", tools)
        self.assertIn("homepod_say", tools)
        self.assertIn("living_room_camera_snapshot", tools)

    def test_homepod_say_returns_structured_success(self) -> None:
        result = {
            "ok": True,
            "action": "homepod.say",
            "status": "played",
            "confidence": "high",
            "attempts": 1,
        }
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(result), stderr=""
        )
        with patch.object(
            ha_mcp_server.subprocess, "run", return_value=completed
        ) as run:
            payload = json.loads(ha_mcp_server.homepod_say("测试"))
        self.assertEqual(payload, result)
        self.assertEqual(run.call_args.kwargs["env"]["HOMEPOD_TTS_PROVIDER"], "openai")
        self.assertEqual(run.call_args.kwargs["env"]["HOMEPOD_OPENAI_VOICE"], "nova")

    def test_homepod_say_turns_cli_failure_into_tool_error(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=json.dumps({"ok": False, "error": "decoder_error"}),
        )
        with patch.object(ha_mcp_server.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "decoder_error"):
                ha_mcp_server.homepod_say("测试")

    def test_homepod_say_rejects_unbounded_inputs(self) -> None:
        with self.assertRaises(ValueError):
            ha_mcp_server.homepod_say("x" * 501)
        with self.assertRaises(ValueError):
            ha_mcp_server.homepod_say("测试", voice="bad voice")
        with self.assertRaises(ValueError):
            ha_mcp_server.homepod_say("测试", volume=1.1)

    def test_camera_snapshot_returns_image_bytes_cleans_file_and_restores(self) -> None:
        with tempfile.TemporaryDirectory(prefix="camera-mcp-test-") as tmp:
            tmp_path = Path(tmp)
            snap_dir = tmp_path / "snaps"
            snap_dir.mkdir()
            image_path = snap_dir / "沙发.jpg"
            image_bytes = b"\xff\xd8test-jpeg\xff\xd9"
            image_path.write_bytes(image_bytes)
            presets = tmp_path / "presets.json"
            presets.write_text('{"餐桌": {}, "沙发": {}}', encoding="utf-8")
            captured = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=str(image_path) + "\n", stderr=""
            )
            restored = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with (
                patch.object(ha_mcp_server, "CAMERA_PRESETS_FILE", presets),
                patch.object(ha_mcp_server, "CAMERA_SNAP_DIR", snap_dir),
                patch.object(
                    ha_mcp_server.subprocess, "run", side_effect=[captured, restored]
                ) as run,
            ):
                image = ha_mcp_server.living_room_camera_snapshot("沙发")
            self.assertEqual(image.data, image_bytes)
            self.assertFalse(image_path.exists())
            self.assertEqual(run.call_count, 2)
            self.assertIn("goto", run.call_args_list[1].args[0])
            self.assertIn("餐桌", run.call_args_list[1].args[0])

    def test_camera_snapshot_rejects_unknown_position(self) -> None:
        with tempfile.TemporaryDirectory(prefix="camera-mcp-test-") as tmp:
            presets = Path(tmp) / "presets.json"
            presets.write_text('{"餐桌": {}}', encoding="utf-8")
            with (
                patch.object(ha_mcp_server, "CAMERA_PRESETS_FILE", presets),
                patch.object(ha_mcp_server.subprocess, "run") as run,
            ):
                with self.assertRaisesRegex(ValueError, "未知摄像头机位"):
                    ha_mcp_server.living_room_camera_snapshot("卧室")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
