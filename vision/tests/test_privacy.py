"""P14 privacy guard tests (no real foreground window required)."""

from __future__ import annotations

import unittest
from unittest import mock

from vhcu_daemon.privacy import (
    DEFAULT_BLACKLIST,
    foreground_title,
    is_sensitive_title,
    sanitize_blacklist,
)
from vhcu_daemon.runner import Runner


class PrivacyMatchTest(unittest.TestCase):
    def test_default_blacklist_and_case_insensitive_matching(self):
        self.assertIn("密码", DEFAULT_BLACKLIST)
        self.assertTrue(is_sensitive_title("请登录 Password - Chrome", DEFAULT_BLACKLIST))
        self.assertTrue(is_sensitive_title("招商银行个人网银", DEFAULT_BLACKLIST))
        self.assertFalse(is_sensitive_title("记事本 - 未命名", DEFAULT_BLACKLIST))
        self.assertFalse(is_sensitive_title("", DEFAULT_BLACKLIST))

    def test_sanitize_blacklist(self):
        cleaned = sanitize_blacklist([" 密码 ", "password", "PASSWORD", "", 42, "x" * 70])
        self.assertEqual(cleaned, ["密码", "password", "42"])
        self.assertEqual(sanitize_blacklist(None), [])


class PrivacyRunnerTest(unittest.TestCase):
    class Proto:
        def __init__(self):
            self.events = []

        def event(self, kind, **fields):
            self.events.append((kind, fields))

        def error(self, *args, **kwargs):
            pass

    def test_sensitive_title_blocks_and_resumes_with_events(self):
        proto = self.Proto()
        runner = Runner(proto)
        runner.privacy_blacklist = ["密码"]
        with mock.patch("vhcu_daemon.runner.foreground_title", return_value="登录密码 - Chrome"):
            self.assertTrue(runner._privacy_should_block())
            self.assertTrue(runner.privacy_blocked)
            blocked = [event for event in proto.events if event[0] == "vision.blocked"]
            self.assertTrue(blocked)
            self.assertTrue(blocked[-1][1]["blocked"])
            self.assertIn("密码", blocked[-1][1]["title"])
        with mock.patch("vhcu_daemon.runner.foreground_title", return_value="记事本"):
            self.assertFalse(runner._privacy_should_block())
            self.assertFalse(runner.privacy_blocked)
            resumed = [event for event in proto.events if event[0] == "vision.blocked"][-1]
            self.assertFalse(resumed[1]["blocked"])

    def test_empty_blacklist_never_blocks(self):
        runner = Runner(self.Proto())
        runner.privacy_blacklist = []
        with mock.patch("vhcu_daemon.runner.foreground_title", return_value="密码"):
            self.assertFalse(runner._privacy_should_block())


class PrivacyCaptureTest(unittest.TestCase):
    class Proto:
        def __init__(self):
            self.events = []

        def event(self, kind, **fields):
            self.events.append((kind, fields))

        def error(self, *args, **kwargs):
            pass

    def test_sensitive_capture_returns_synthetic_warning_image(self):
        from types import SimpleNamespace

        runner = Runner(self.Proto())
        runner.privacy_blacklist = ["密码"]
        runner._source = SimpleNamespace(width=800, height=600)
        runner._latest = SimpleNamespace(width=800, height=600, data=b"old-sensitive-pixels")
        with mock.patch("vhcu_daemon.runner.foreground_title", return_value="密码 - Chrome"), mock.patch(
            "vhcu_daemon.runner.foreground_identity",
            return_value={"hwnd": 42, "pid": 7, "title": "密码 - Chrome"},
        ):
            result = runner.capture_now()
        self.assertTrue(result["privacyBlocked"])
        self.assertTrue(result["privacyImage"])
        self.assertEqual(result["privacyMode"], "warning_image")
        self.assertEqual(result["source"], "privacy_guard")
        frame = runner.latest_frame()
        self.assertIsNotNone(frame)
        self.assertEqual(frame.backend, "privacy_guard_warning")
        self.assertNotEqual(frame.data, b"old-sensitive-pixels")
        self.assertIn("密码", result["privacyTitle"])

    def test_black_privacy_mode_returns_pure_black(self):
        from types import SimpleNamespace

        runner = Runner(self.Proto())
        runner.privacy_blacklist = ["密码"]
        runner._source = SimpleNamespace(width=640, height=360)
        runner._latest = SimpleNamespace(width=640, height=360, data=b"old")
        with mock.patch("vhcu_daemon.runner.foreground_title", return_value="密码 - Chrome"), mock.patch(
            "vhcu_daemon.runner.foreground_identity",
            return_value={"hwnd": 42, "pid": 7, "title": "密码 - Chrome"},
        ):
            result = runner.capture_now(privacy_block_mode="black_image")
        self.assertEqual(result["privacyMode"], "black_image")
        frame = runner.latest_frame()
        self.assertEqual(frame.backend, "privacy_guard_black")
        self.assertEqual(set(frame.data), {0})

    def test_unprotected_capture_is_explicitly_flagged(self):
        from types import SimpleNamespace

        runner = Runner(self.Proto())
        runner.privacy_blacklist = ["密码"]
        runner.active_source = "fake"
        runner._source = SimpleNamespace(width=800, height=600)
        with mock.patch("vhcu_daemon.runner.foreground_title", return_value="密码 - Chrome"), mock.patch(
            "vhcu_daemon.runner.foreground_identity",
            return_value={"hwnd": 42, "pid": 7, "title": "密码 - Chrome"},
        ):
            result = runner.capture_now(allow_unprotected=True)
        self.assertTrue(result["privacyBlocked"])
        self.assertFalse(result["masked"])
        self.assertTrue(result["unprotected"])

    def test_capture_loop_invalidates_cached_frame_on_privacy(self):
        from types import SimpleNamespace

        runner = Runner(self.Proto())
        runner._latest = SimpleNamespace(width=800, height=600, data=b"old-sensitive-pixels")

        def block_once():
            runner._stop.set()
            return True

        with mock.patch.object(runner, "_privacy_should_block", side_effect=block_once):
            runner._loop(30)
        self.assertIsNone(runner.latest_frame())


class UiaPrivacyGateTest(unittest.TestCase):
    """P1 隐患 3: UIA entry points must honor the privacy blacklist."""

    def _daemon(self):
        from types import SimpleNamespace

        return SimpleNamespace(runner=SimpleNamespace(privacy_blacklist=["password"]))

    def test_sensitive_target_title_raises_privacy_blocked(self):
        from vhcu_daemon.cli import Daemon
        from vhcu_daemon.protocol import ProtocolError

        with mock.patch("vhcu_daemon.win_capture.window_title", return_value="Login Password"):
            with self.assertRaises(ProtocolError) as raised:
                Daemon._assert_uia_privacy(self._daemon(), 123)
        self.assertEqual(raised.exception.code, "E_PRIVACY_BLOCKED")

    def test_normal_target_title_passes(self):
        from vhcu_daemon.cli import Daemon

        with mock.patch("vhcu_daemon.win_capture.window_title", return_value="Notepad"):
            Daemon._assert_uia_privacy(self._daemon(), 123)

    def test_unreadable_target_title_fails_closed(self):
        from vhcu_daemon.cli import Daemon
        from vhcu_daemon.protocol import ProtocolError

        with mock.patch("vhcu_daemon.win_capture.window_title", side_effect=OSError("gone")):
            with self.assertRaises(ProtocolError) as raised:
                Daemon._assert_uia_privacy(self._daemon(), 123)
        self.assertEqual(raised.exception.code, "E_PRIVACY_BLOCKED")

    def test_empty_target_title_fails_closed(self):
        from vhcu_daemon.cli import Daemon
        from vhcu_daemon.protocol import ProtocolError

        with mock.patch("vhcu_daemon.win_capture.window_title", return_value=""):
            with self.assertRaises(ProtocolError) as raised:
                Daemon._assert_uia_privacy(self._daemon(), 123)
        self.assertEqual(raised.exception.code, "E_PRIVACY_BLOCKED")

    def test_explicit_unprotected_switch_bypasses_title_check(self):
        from vhcu_daemon.cli import Daemon

        # The user explicitly enabled privacyUnprotected; the Python half must
        # honor that choice instead of contradicting the Node gate.
        with mock.patch(
            "vhcu_daemon.win_capture.window_title",
            side_effect=OSError("unreadable"),
        ):
            Daemon._assert_uia_privacy(self._daemon(), 123, allow_unprotected=True)
