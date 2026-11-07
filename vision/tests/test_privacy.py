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
from vhcu_daemon.protocol import ProtocolError
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

    def test_sensitive_capture_raises_fatal_and_serves_no_frame(self):
        from types import SimpleNamespace

        runner = Runner(self.Proto())
        runner.privacy_blacklist = ["密码"]
        runner._source = SimpleNamespace(width=800, height=600)
        runner._latest = SimpleNamespace(width=800, height=600, data=b"old-sensitive-pixels")
        with mock.patch("vhcu_daemon.runner.foreground_title", return_value="密码 - Chrome"), mock.patch(
            "vhcu_daemon.runner.foreground_identity",
            return_value={"hwnd": 42, "pid": 7, "title": "密码 - Chrome"},
        ):
            with self.assertRaises(ProtocolError) as ctx:
                runner.capture_now()
        self.assertEqual(ctx.exception.code, "E_PRIVACY_BLOCKED")
        self.assertTrue(ctx.exception.fatal)
        self.assertIn("强制拉闸", str(ctx.exception))
        self.assertIn("vp_*", ctx.exception.hint or "")
        self.assertIsNone(runner.latest_frame(), "敏感窗口触发时绝不能留下任何可读取帧")

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
