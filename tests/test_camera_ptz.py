#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "camera-ptz.py"
SPEC = importlib.util.spec_from_file_location("camera_ptz", SCRIPT)
assert SPEC and SPEC.loader
CAMERA_PTZ = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAMERA_PTZ)


class FakeProfile:
    token = "profile-token"


class FakeMedia:
    def GetProfiles(self):
        return [FakeProfile()]


class FakeCamera:
    last_init = None

    def __init__(self, *args):
        FakeCamera.last_init = args

    def create_media_service(self):
        return FakeMedia()

    def create_ptz_service(self):
        return "ptz-service"


class CameraPTZTests(unittest.TestCase):
    def test_get_ptz_bypasses_upper_and_lower_case_proxy_variables(self) -> None:
        fake_onvif = types.ModuleType("onvif")
        fake_onvif.ONVIFCamera = FakeCamera
        proxy_env = {
            "HTTP_PROXY": "http://proxy.invalid",
            "HTTPS_PROXY": "http://proxy.invalid",
            "ALL_PROXY": "socks://proxy.invalid",
            "http_proxy": "http://proxy.invalid",
        }
        with (
            patch.dict(os.environ, proxy_env, clear=False),
            patch.dict(sys.modules, {"onvif": fake_onvif}),
            patch.object(CAMERA_PTZ, "load_env", return_value={"CAM_LIVINGROOM_PWD": "secret"}),
        ):
            ptz, token = CAMERA_PTZ.get_ptz()
            for name in proxy_env:
                self.assertNotIn(name, os.environ)

        self.assertEqual(ptz, "ptz-service")
        self.assertEqual(token, "profile-token")
        self.assertEqual(
            FakeCamera.last_init,
            (CAMERA_PTZ.CAM_HOST, CAMERA_PTZ.ONVIF_PORT, "admin", "secret"),
        )

    def test_snap_uses_collision_safe_sanitized_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="camera-ptz-test-") as tmp:
            snap_dir = Path(tmp)

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"jpeg")
                return subprocess.CompletedProcess(command, 0, b"", b"")

            with (
                patch.object(CAMERA_PTZ, "SNAP_DIR", snap_dir),
                patch.object(CAMERA_PTZ, "load_env", return_value={"CAM_LIVINGROOM_PWD": "secret"}),
                patch.object(CAMERA_PTZ.subprocess, "run", side_effect=fake_run),
            ):
                first = CAMERA_PTZ.snap("沙发/../x")
                second = CAMERA_PTZ.snap("沙发/../x")

            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, snap_dir)
            self.assertNotIn("..", first.name)
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())

    def test_snap_failure_does_not_expose_camera_password(self) -> None:
        secret = "do-not-leak"
        failed = subprocess.CompletedProcess([], 1, b"", f"rtsp://admin:{secret}@camera".encode())
        with tempfile.TemporaryDirectory(prefix="camera-ptz-test-") as tmp:
            with (
                patch.object(CAMERA_PTZ, "SNAP_DIR", Path(tmp)),
                patch.object(CAMERA_PTZ, "load_env", return_value={"CAM_LIVINGROOM_PWD": secret}),
                patch.object(CAMERA_PTZ.subprocess, "run", return_value=failed),
            ):
                with self.assertRaises(RuntimeError) as caught:
                    CAMERA_PTZ.snap()
        self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
