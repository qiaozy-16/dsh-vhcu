"""P1 platform abstraction: absolute paths, interrupt channel and safety."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from vhcu_daemon.drivers import create_drivers
from vhcu_daemon.httpframe import HttpFrameServer
from vhcu_daemon.drivers.windows import WindowsAudioBackend, WindowsCaptureBackend, WindowsInputBackend
from vhcu_daemon.interrupts import InterruptController, InterruptError
from vhcu_daemon.input import InputController
from vhcu_daemon.paths import PathConfigError, models_dir, resource_paths
from vhcu_daemon.safety import InvalidScreen, clamp_point, in_screen_corner, normalize_point


class AbsolutePathsTest(unittest.TestCase):
    def test_default_model_root_is_absolute(self):
        self.assertTrue(models_dir().is_absolute())

    def test_injected_absolute_paths_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "VHCU_MODEL_PATH": str(root / "models"),
                "VHCU_MACROS_PATH": str(root / "macros.json"),
                "VHCU_LOG_DIR": str(root / "logs"),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                got = resource_paths()
            self.assertEqual(got["models"], env["VHCU_MODEL_PATH"])
            self.assertEqual(got["macros"], env["VHCU_MACROS_PATH"])
            self.assertEqual(got["logs"], env["VHCU_LOG_DIR"])
            for value in got.values():
                self.assertTrue(Path(value).is_absolute())

    def test_relative_model_path_is_rejected(self):
        with mock.patch.dict(os.environ, {"VHCU_MODEL_PATH": "models"}, clear=False):
            with self.assertRaises(PathConfigError):
                models_dir()


class InterruptChannelTest(unittest.TestCase):
    def test_request_check_and_clear(self):
        interrupt = InterruptController()
        self.assertFalse(interrupt.is_set())
        interrupt.request("user-takeover")
        self.assertTrue(interrupt.is_set())
        self.assertEqual(interrupt.reason, "user-takeover")
        with self.assertRaises(InterruptError):
            interrupt.check()
        interrupt.clear()
        self.assertFalse(interrupt.is_set())

    def test_interruptible_sleep_raises_early(self):
        interrupt = InterruptController()
        interrupt.request("cancel")
        with self.assertRaises(InterruptError):
            interrupt.sleep(5.0)

    def test_input_controller_checks_interrupt_before_action(self):
        interrupt = InterruptController()
        controller = InputController(dry_run=True, interrupt=interrupt)
        controller.width = 1920
        controller.height = 1080
        interrupt.request("user-takeover")
        with self.assertRaises(InterruptError):
            controller.click(10, 10)

    def test_macro_loop_stops_on_interrupt(self):
        interrupt = InterruptController()
        controller = InputController(dry_run=False, interrupt=interrupt)
        controller._rate_limit = lambda: None
        controller._failsafe = lambda: None
        interrupt.request("user-takeover")
        with self.assertRaises(InterruptError):
            controller.run_macro([{"op": "wait", "ms": 1}])


class DriverFactoryTest(unittest.TestCase):
    def test_windows_factory_builds_all_three_drivers(self):
        drivers = create_drivers(emit=lambda *args, **kwargs: None)
        self.assertIsInstance(drivers.input, WindowsInputBackend)
        self.assertIsInstance(drivers.capture, WindowsCaptureBackend)
        self.assertIsInstance(drivers.audio, WindowsAudioBackend)


class InterruptHttpTest(unittest.TestCase):
    def test_loopback_interrupt_endpoint_uses_token(self):
        seen = []
        server = HttpFrameServer(
            lambda: None,
            "p1-token-123",
            port=0,
            on_interrupt=lambda reason, clear: seen.append((reason, clear)) or {"ok": True},
        )
        port = server.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/interrupt?token=p1-token-123&reason=test",
                method="POST",
                data=b"",
            )
            with urllib.request.urlopen(req, timeout=2) as response:
                body = json.loads(response.read())
            self.assertTrue(body["ok"])
            self.assertEqual(seen, [("test", False)])
        finally:
            server.stop()


class SafetyPrimitivesTest(unittest.TestCase):
    def test_clamp_and_normalize(self):
        self.assertEqual(clamp_point(-5, 2000, 1920, 1080), (0, 1079, True))
        self.assertEqual(normalize_point(10, 20, 1920, 1080), (10, 20))
        with self.assertRaises(InvalidScreen):
            clamp_point(1, 1, 0, 0)

    def test_corner_failsafe(self):
        self.assertTrue(in_screen_corner(0, 0, 1920, 1080))
        self.assertTrue(in_screen_corner(1919, 1079, 1920, 1080))
        self.assertFalse(in_screen_corner(100, 100, 1920, 1080))


if __name__ == "__main__":
    unittest.main()
