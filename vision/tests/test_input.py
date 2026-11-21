"""Unit tests for the P5 SendInput layer (no real input is injected).

Run: py -3 -m unittest discover -s vision/tests -t vision -v
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from vhcu_daemon.virtual_cursor import VirtualCursorUnsupported
from vhcu_daemon.input import (
    ABSOLUTE_MOVE,
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    MAX_TEXT_CHARS,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MIDDLEUP,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_VIRTUALDESK,
    RELEASE_VKS,
    InputController,
    InputError,
    clamp_click,
    extended_key_flag,
    in_corner,
    normalize_click,
    parse_hotkey,
    virtual_screen_rect,
    validate_text,
)

WINDOWS = os.name == "nt"


class ExtendedKeyFlagTest(unittest.TestCase):
    def test_win_and_navigation_keys_are_extended(self):
        self.assertEqual(extended_key_flag(0x5B), KEYEVENTF_EXTENDEDKEY)  # VK_LWIN
        self.assertEqual(extended_key_flag(0x5C), KEYEVENTF_EXTENDEDKEY)  # VK_RWIN
        for vk in (0x25, 0x26, 0x27, 0x28, 0x24, 0x23, 0x21, 0x22, 0x2D, 0x2E):
            self.assertEqual(extended_key_flag(vk), KEYEVENTF_EXTENDEDKEY, hex(vk))
        self.assertEqual(extended_key_flag(0x41), 0)  # 'A' stays a plain key


@unittest.skipUnless(WINDOWS, "SendInput is Windows-only")
class InputPrivacyGateTest(unittest.TestCase):
    def test_privacy_block_prevents_injection_before_send(self):
        controller = InputController(dry_run=True)
        controller.set_privacy_check(lambda hwnd: True)
        with self.assertRaises(InputError) as raised:
            controller.click(1, 1)
        self.assertEqual(raised.exception.code, "E_PRIVACY_BLOCKED")

    def test_privacy_check_failure_fails_closed(self):
        controller = InputController(dry_run=True)

        def broken(hwnd):
            raise RuntimeError("checker unavailable")

        controller.set_privacy_check(broken)
        with self.assertRaises(InputError) as raised:
            controller.click(1, 1)
        self.assertEqual(raised.exception.code, "E_PRIVACY_BLOCKED")

    def test_explicit_unprotected_switch_bypasses_input_gate(self):
        controller = InputController(dry_run=True)
        controller.set_privacy_check(lambda hwnd: True)
        result = controller.click(1, 1, allow_unprotected=True)
        self.assertEqual(result.get("dryRun"), True)


@unittest.skipUnless(WINDOWS, "SendInput is Windows-only")
class ReleaseAllTest(unittest.TestCase):
    """P1 隐患 1: startup/shutdown must force-release stuck input state."""

    def test_release_all_clears_modifiers_and_mouse_buttons(self):
        controller = InputController(dry_run=False)
        captured = []
        controller._send = lambda *events: captured.extend(events)
        result = controller.release_all()
        self.assertTrue(result["released"])
        self.assertEqual(result["keys"], len(RELEASE_VKS))
        key_flags = [int(event.ki.dwFlags) for event in captured if event.type == INPUT_KEYBOARD]
        mouse_flags = [int(event.mi.dwFlags) for event in captured if event.type == INPUT_MOUSE]
        self.assertEqual(len(key_flags), len(RELEASE_VKS))
        self.assertTrue(all(flag & KEYEVENTF_KEYUP for flag in key_flags))
        self.assertEqual(
            set(mouse_flags),
            {MOUSEEVENTF_LEFTUP, MOUSEEVENTF_MIDDLEUP, MOUSEEVENTF_RIGHTUP},
        )

    def test_release_all_dry_run_does_not_inject(self):
        controller = InputController(dry_run=True)
        captured = []
        controller._send = lambda *events: captured.extend(events)
        result = controller.release_all()
        self.assertTrue(result["dryRun"])
        self.assertEqual(captured, [])


class GeometryTest(unittest.TestCase):
    def test_normalize_hits_edges(self):
        self.assertEqual(normalize_click(0, 0, 1920, 1080), (0, 0))
        self.assertEqual(normalize_click(1919, 1079, 1920, 1080), (65535, 65535))
        self.assertEqual(normalize_click(960, 540, 1920, 1080), (32785, 32798))

    def test_normalize_clamps_out_of_bounds(self):
        self.assertEqual(normalize_click(-1, 10, 1920, 1080), normalize_click(0, 10, 1920, 1080))
        self.assertEqual(normalize_click(1920, 10, 1920, 1080), normalize_click(1919, 10, 1920, 1080))
        clamped = clamp_click(-5, 9999, 1920, 1080)
        self.assertEqual(clamped, (0, 1079, True))

    def test_virtual_screen_rect_reads_sm_constants(self):
        values = {76: -1920, 77: -200, 78: 6400, 79: 1440}
        fake_user32 = mock.Mock()
        fake_user32.GetSystemMetrics.side_effect = lambda index: values[index]
        with mock.patch("vhcu_daemon.input.IS_WINDOWS", True),              mock.patch("vhcu_daemon.input.ctypes.windll.user32", fake_user32):
            self.assertEqual(virtual_screen_rect(), (-1920, -200, 6400, 1440))

    def test_virtual_origin_normalizes_negative_monitor(self):
        self.assertEqual(
            normalize_click(-1920, 0, 6400, 1080, -1920, 0),
            (0, 0),
        )
        self.assertEqual(
            normalize_click(0, 0, 6400, 1080, -1920, 0),
            (19664, 0),
        )
        self.assertEqual(
            clamp_click(-2000, 0, 6400, 1080, -1920, 0),
            (-1920, 0, True),
        )
        self.assertTrue(ABSOLUTE_MOVE & MOUSEEVENTF_VIRTUALDESK)

    def test_corner_failsafe(self):
        self.assertTrue(in_corner(0, 0, 1920, 1080))
        self.assertTrue(in_corner(1919, 1079, 1920, 1080))
        self.assertTrue(in_corner(5, 1075, 1920, 1080))
        self.assertFalse(in_corner(100, 100, 1920, 1080))
        self.assertFalse(in_corner(960, 540, 1920, 1080))


class HotkeyTest(unittest.TestCase):
    def test_ctrl_s(self):
        self.assertEqual(parse_hotkey(["ctrl", "s"]), [0x11, 0x53])

    def test_win_d_and_alt_tab(self):
        self.assertEqual(parse_hotkey(["win", "d"]), [0x5B, 0x44])
        self.assertEqual(parse_hotkey(["alt", "tab"]), [0x12, 0x09])

    def test_case_insensitive_and_single_key(self):
        self.assertEqual(parse_hotkey(["CTRL", "S"]), [0x11, 0x53])
        self.assertEqual(parse_hotkey(["enter"]), [0x0D])

    def test_invalid_names_and_shapes(self):
        for bad in (["ctrl", "nope"], ["ctrl"], [], ["ctrl", "shift"], ["ctrl", "s", "x", "y", "z"]):
            with self.assertRaises(InputError):
                parse_hotkey(bad)


class TextTest(unittest.TestCase):
    def test_text_limits(self):
        self.assertEqual(validate_text("hello 你好"), "hello 你好")
        self.assertEqual(len(validate_text("x" * MAX_TEXT_CHARS)), MAX_TEXT_CHARS)
        with self.assertRaises(InputError):
            validate_text("")
        with self.assertRaises(InputError):
            validate_text("x" * (MAX_TEXT_CHARS + 1))


@unittest.skipUnless(WINDOWS, "SendInput is Windows-only")
class DryRunControllerTest(unittest.TestCase):
    def setUp(self):
        self.controller = InputController(dry_run=True)

    def test_dry_run_click(self):
        result = self.controller.click(100, 120)
        self.assertTrue(result["dryRun"])
        self.assertEqual((result["x"], result["y"]), (100, 120))
        self.assertEqual(len(result["normalized"]), 2)

    def test_dry_run_type_and_hotkey(self):
        self.assertTrue(self.controller.type_text("hello")["dryRun"])
        combo = self.controller.key_press(["ctrl", "s"])
        self.assertTrue(combo["dryRun"])
        self.assertEqual(combo["keys"], ["ctrl", "s"])

    def test_dry_run_scroll(self):
        result = self.controller.scroll(-3)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["amount"], -3)
        with self.assertRaises(InputError):
            self.controller.scroll(0)
        with self.assertRaises(InputError):
            self.controller.scroll(21)

    def test_p242_key_press_delay_and_scancode_dry_run(self):
        controller = InputController(dry_run=True)
        result = controller.key_press(["shift", "a"], delay_ms=35)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["delayMs"], 35)
        self.assertTrue(result["scancode"])
        with self.assertRaises(InputError):
            controller.key_press(["ctrl", "s"], delay_ms=1001)

    def test_p0_win_key_scancodes_carry_extended_flag(self):
        controller = InputController(dry_run=True)
        win_down = controller._key_scan_input(0x5B, 0)
        self.assertTrue(win_down.ki.dwFlags & KEYEVENTF_EXTENDEDKEY)
        win_up = controller._key_scan_input(0x5B, KEYEVENTF_KEYUP)
        self.assertTrue(win_up.ki.dwFlags & KEYEVENTF_KEYUP)
        self.assertTrue(win_up.ki.dwFlags & KEYEVENTF_EXTENDEDKEY)
        a_down = controller._key_scan_input(0x41, 0)
        self.assertFalse(a_down.ki.dwFlags & KEYEVENTF_EXTENDEDKEY)

    def test_p0_send_combo_tags_win_modifier_extended(self):
        controller = InputController(dry_run=True)
        captured = []
        controller._send = lambda *events: captured.extend(events)
        controller._send_combo([0x5B, 0x52])  # win+r
        self.assertEqual(len(captured), 4)
        self.assertTrue(captured[0].ki.dwFlags & KEYEVENTF_EXTENDEDKEY)  # win down
        self.assertTrue(captured[3].ki.dwFlags & KEYEVENTF_EXTENDEDKEY)  # win up
        self.assertFalse(captured[1].ki.dwFlags & KEYEVENTF_EXTENDEDKEY)  # 'R' is plain

    def test_dry_run_drag(self):
        result = self.controller.drag(100, 100, 300, 240)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["from"], [100, 100])
        self.assertEqual(result["to"], [300, 240])
        clamped = self.controller.drag(-1, 0, self.controller.width + 100, 200)
        self.assertTrue(clamped["dryRun"])
        self.assertEqual(clamped["from"], [0, 0])
        self.assertEqual(clamped["to"], [self.controller.width - 1, 200])
        self.assertIn("坐标越界", clamped["warning"])

    def test_p24_middle_drag_dry_run(self):
        result = self.controller.middle_drag(10, 20, 200, 150)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["action"], "middle_drag")
        self.assertEqual(result["from"], [10, 20])
        self.assertEqual(result["to"], [200, 150])
        clamped = self.controller.middle_drag(-10, -10, self.controller.width + 5, 20)
        self.assertEqual(clamped["from"], [0, 0])
        self.assertIn("坐标越界", clamped["warning"])

    def test_humanized_click_reports_plan_without_injection(self):
        result = self.controller.click(120, 140, humanize_profile="balanced")
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["humanize"]["profile"], "balanced")
        self.assertGreaterEqual(result["humanize"]["steps"], 6)
        self.assertEqual(result["humanize"]["endpoint"], [120, 140])
        precise = self.controller.click(500, 400, humanize_profile="precise")
        self.assertLessEqual(precise["humanize"]["steps"], 6)
        self.assertEqual(precise["humanize"]["jitter_px"], 0.0)

    def test_humanized_invalid_profile_is_protocol_error(self):
        with self.assertRaises(InputError) as raised:
            self.controller.click(10, 10, humanize_profile="robot")
        self.assertEqual(raised.exception.code, "E_PROTO")

    def test_humanized_drag_has_two_segments(self):
        result = self.controller.drag(100, 100, 300, 240, humanize_profile="balanced")
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["humanize"]["segments"], 2)
        self.assertEqual(result["humanize"]["endpoint"], [300, 240])

    def test_macro_reports_humanize_profile(self):
        result = self.controller.run_macro(
            [{"op": "click", "x": 100, "y": 120}],
            humanize_profile="balanced",
        )
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["humanizeProfile"], "balanced")

    def test_status_reports_screen_and_dpi(self):
        status = self.controller.status()
        self.assertGreater(status["screen"]["width"], 0)
        self.assertGreater(status["screen"]["height"], 0)
        self.assertTrue(status["dryRun"])
        self.assertIn(status["dpiMode"], {"per-monitor-v2", "per-monitor", "system", "none"})


@unittest.skipUnless(WINDOWS, "SendInput is Windows-only")
class VirtualInjectionTest(unittest.TestCase):
    class FakeOverlay:
        def __init__(self):
            self.shown = []
            self.hidden = 0
            self.closed = 0

        def show(self, x, y, action="move"):
            self.shown.append((x, y, action))

        def hide(self):
            self.hidden += 1

        def close(self):
            self.closed += 1

    def _controller(self):
        overlay = self.FakeOverlay()
        controller = InputController(dry_run=False, overlay=overlay)
        controller._send = mock.Mock()
        controller._sleep = mock.Mock()
        controller._failsafe = mock.Mock()
        return controller, overlay

    def test_overlay_failure_never_blocks_virtual_injection(self):
        class BadOverlay:
            def show(self, *args, **kwargs):
                raise RuntimeError("overlay exploded")

            def hide(self):
                raise RuntimeError("hide exploded")

            def close(self):
                raise RuntimeError("close exploded")

        controller = InputController(dry_run=False, overlay=BadOverlay())
        controller._send = mock.Mock()
        controller._sleep = mock.Mock()
        controller._failsafe = mock.Mock()
        posted = {"ok": True, "used": "post_message", "hwnd": 77}
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(40, 40)),              mock.patch("vhcu_daemon.input.resolve_window", return_value=77),              mock.patch("vhcu_daemon.input.post_message_click", return_value=posted):
            result = controller.click(320, 260, humanize_profile="off",
                                      cursor_injection_mode="virtual")
        self.assertEqual(result["cursor"]["used"], "post_message")
        self.assertEqual(controller._send.call_count, 0)

    def test_virtual_click_still_clamps_and_runs_failsafe(self):
        controller, _overlay = self._controller()
        posted = {"ok": True, "used": "post_message", "hwnd": 77}
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(40, 40)),              mock.patch("vhcu_daemon.input.resolve_window", return_value=77),              mock.patch("vhcu_daemon.input.post_message_click", return_value=posted) as click_mock:
            controller.click(-10, 99999, humanize_profile="off",
                             cursor_injection_mode="virtual")
        controller._failsafe.assert_called_once()
        args = click_mock.call_args.args
        self.assertEqual(args[0], 77)
        self.assertEqual(args[2], 0)
        self.assertEqual(args[3], controller.height - 1)

    def test_physical_failsafe_blocks_corner(self):
        controller = InputController(dry_run=False)
        controller._rate_limit = lambda: None
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(5, 5)):
            with self.assertRaises(InputError) as raised:
                controller.click(100, 100)
        self.assertEqual(raised.exception.code, "E_DENIED")

    def test_virtual_taskbar_unsupported_surfaces_exact_code(self):
        from vhcu_daemon.virtual_cursor import VirtualCursorUnsupported
        controller = InputController(dry_run=False)
        with mock.patch("vhcu_daemon.input.resolve_window", return_value=42),              mock.patch("vhcu_daemon.input.post_message_click",
                        side_effect=VirtualCursorUnsupported("Shell_TrayWnd unsupported", "use physical")):
            with self.assertRaises(InputError) as raised:
                controller.click(320, 260, cursor_injection_mode="virtual")
        self.assertEqual(raised.exception.code, "E_VIRTUAL_CURSOR_UNSUPPORTED")

    def test_virtual_balanced_uses_80_to_120ms(self):
        controller, _overlay = self._controller()
        posted = {"ok": True, "used": "post_message", "hwnd": 77}
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(40, 40)),              mock.patch("vhcu_daemon.input.resolve_window", return_value=77),              mock.patch("vhcu_daemon.input.post_message_click", return_value=posted):
            result = controller.click(900, 600, humanize_profile="balanced",
                                      cursor_injection_mode="virtual")
        self.assertGreaterEqual(result["humanize"]["duration_ms"], 80)
        self.assertLessEqual(result["humanize"]["duration_ms"], 120)
        self.assertLessEqual(result["humanize"]["duration_ms"], 120)

    def test_virtual_right_and_double_click_actions(self):
        controller, _overlay = self._controller()
        posted = {"ok": True, "used": "post_message", "hwnd": 77}
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(40, 40)),              mock.patch("vhcu_daemon.input.resolve_window", return_value=77),              mock.patch("vhcu_daemon.input.post_message_click", return_value=posted) as post:
            right = controller.click(50, 60, cursor_injection_mode="virtual",
                                     click_type="right_click")
            double = controller.click(50, 60, cursor_injection_mode="virtual",
                                      click_type="double_click")
        self.assertEqual(right["action"], "right_click")
        self.assertEqual(double["action"], "double_click")
        self.assertEqual(post.call_args_list[0].args[1], "right_click")
        self.assertEqual(post.call_args_list[1].args[1], "double_click")

    def test_virtual_scroll_uses_post_message(self):
        controller, _overlay = self._controller()
        posted = {"ok": True, "used": "post_message", "hwnd": 77}
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(40, 40)),              mock.patch("vhcu_daemon.input.resolve_window", return_value=77),              mock.patch("vhcu_daemon.input.post_message_scroll", return_value=posted) as post:
            result = controller.scroll(2, cursor_injection_mode="virtual")
        self.assertEqual(result["action"], "scroll")
        self.assertEqual(result["cursor"]["used"], "post_message")
        post.assert_called_once_with(77, 40, 40, 2)

    def test_draw_shape_virtual_uses_one_continuous_path(self):
        controller, _overlay = self._controller()
        posted = {"ok": True, "used": "post_message", "messages": ["0x0200"]}
        with mock.patch("vhcu_daemon.input.resolve_window", return_value=77),              mock.patch("vhcu_daemon.input.post_message_path", return_value=posted) as post:
            result = controller.draw_shape(
                "sine", 100, 100, 500, 100,
                curve=80, samples=64, cursor_injection_mode="virtual",
            )
        self.assertEqual(result["cursor"]["used"], "post_message")
        self.assertGreaterEqual(result["samples"], 60)
        self.assertGreaterEqual(len(post.call_args.args[1]), 60)
        self.assertEqual(result["messages"], ["0x0200"])

    def test_draw_shape_dry_run_and_path_math(self):
        controller = InputController(dry_run=True)
        result = controller.draw_shape("parabola", 100, 100, 600, 100,
                                       curve=200, samples=160, duration_ms=500)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["action"], "draw_shape")
        self.assertGreaterEqual(result["samples"], 100)
        self.assertEqual(result["points"][0], (100, 100))
        self.assertEqual(result["points"][-1], (600, 100))

    def test_virtual_click_uses_post_message_and_no_sendinput(self):
        controller, overlay = self._controller()
        posted = {"used": "post_message", "hwnd": 77, "messages": ["0x0200"]}
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(40, 40)),              mock.patch("vhcu_daemon.input.resolve_window", return_value=77),              mock.patch("vhcu_daemon.input.post_message_click", return_value=posted) as post:
            result = controller.click(320, 260, humanize_profile="off",
                                      cursor_injection_mode="virtual")
        self.assertFalse(result["dryRun"])
        self.assertEqual(result["cursor"]["used"], "post_message")
        self.assertFalse(result["cursor"]["fallback"])
        self.assertEqual(controller._send.call_count, 0, "virtual path must not call SendInput")
        post.assert_called_once()
        self.assertTrue(overlay.shown)
        self.assertGreaterEqual(overlay.hidden, 0)

    def test_virtual_failure_never_falls_back(self):
        controller, overlay = self._controller()
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(40, 40)),              mock.patch("vhcu_daemon.input.resolve_window", return_value=77),              mock.patch("vhcu_daemon.input.post_message_click",
                        side_effect=VirtualCursorUnsupported("ignored", sent=0)):
            with self.assertRaises(InputError) as raised:
                controller.click(100, 120, cursor_injection_mode="virtual")
        self.assertEqual(raised.exception.code, "E_VIRTUAL_CURSOR_UNSUPPORTED")
        self.assertEqual(controller._send.call_count, 0)
        self.assertGreaterEqual(overlay.hidden, 0)

    def test_hybrid_unsupported_before_send_falls_back_physically(self):
        controller, overlay = self._controller()
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(40, 40)),              mock.patch("vhcu_daemon.input.resolve_window", return_value=77),              mock.patch("vhcu_daemon.input.post_message_click",
                        side_effect=VirtualCursorUnsupported("ignored", sent=0)):
            result = controller.click(320, 260, cursor_injection_mode="hybrid")
        self.assertFalse(result["dryRun"])
        self.assertTrue(result["cursor"]["fallback"])
        self.assertEqual(result["cursor"]["used"], "sendinput")
        self.assertIn("目标软件不支持后台注入", result["cursor"]["warning"])
        self.assertGreater(controller._send.call_count, 0)
        self.assertGreaterEqual(overlay.hidden, 0)

    def test_hybrid_after_first_message_never_replays_physically(self):
        controller, _overlay = self._controller()
        from vhcu_daemon.virtual_cursor import VirtualCursorFailure
        failure = VirtualCursorFailure("post failed", sent=1)
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(40, 40)),              mock.patch("vhcu_daemon.input.resolve_window", return_value=77),              mock.patch("vhcu_daemon.input.post_message_click", side_effect=failure):
            with self.assertRaises(InputError) as raised:
                controller.click(320, 260, cursor_injection_mode="hybrid")
        self.assertEqual(raised.exception.code, "E_VIRTUAL_CURSOR_FAILED")
        self.assertEqual(controller._send.call_count, 0, "must not duplicate after a queued message")

    def test_virtual_drag_uses_post_message_not_sendinput(self):
        controller, overlay = self._controller()
        posted = {"ok": True, "used": "post_message", "hwnd": 77}
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(40, 40)),              mock.patch("vhcu_daemon.input.resolve_window", return_value=77),              mock.patch("vhcu_daemon.input.post_message_drag", return_value=posted) as post:
            result = controller.drag(100, 100, 300, 240, humanize_profile="off",
                                     cursor_injection_mode="virtual")
        self.assertFalse(result["dryRun"])
        self.assertEqual(result["cursor"]["used"], "post_message")
        self.assertEqual(controller._send.call_count, 0)
        post.assert_called_once()
        self.assertTrue(overlay.shown)
        self.assertEqual(overlay.shown[-1][2], "drag")

    def test_virtual_middle_drag_uses_post_message(self):
        controller, _overlay = self._controller()
        posted = {"ok": True, "used": "post_message", "hwnd": 77}
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(40, 40)),              mock.patch("vhcu_daemon.input.resolve_window", return_value=77),              mock.patch("vhcu_daemon.input.post_message_drag", return_value=posted) as post:
            result = controller.middle_drag(
                10, 20, 200, 150, humanize_profile="off",
                cursor_injection_mode="virtual",
            )
        self.assertEqual(result["cursor"]["used"], "post_message")
        self.assertEqual(controller._send.call_count, 0)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs.get("button"), "middle")

    def test_virtual_macro_is_explicitly_unsupported(self):
        controller, _overlay = self._controller()
        with self.assertRaises(InputError) as raised:
            controller.run_macro(
                [{"op": "click", "x": 10, "y": 20}],
                cursor_injection_mode="virtual",
            )
        self.assertEqual(raised.exception.code, "E_VIRTUAL_CURSOR_UNSUPPORTED")
        self.assertEqual(controller._send.call_count, 0)

    def test_invalid_cursor_mode_is_protocol_error(self):
        controller, _overlay = self._controller()
        with self.assertRaises(InputError) as raised:
            controller.click(10, 10, cursor_injection_mode="robot")
        self.assertEqual(raised.exception.code, "E_PROTO")


@unittest.skipUnless(WINDOWS, "SendInput is Windows-only")
class HumanizedInjectionTest(unittest.TestCase):
    def _controller(self):
        controller = InputController(dry_run=False)
        controller._send = mock.Mock()
        controller._sleep = mock.Mock()
        controller._failsafe = mock.Mock()
        return controller

    def test_balanced_click_injects_multiple_moves_then_click(self):
        controller = self._controller()
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(40, 40)):
            result = controller.click(320, 260, humanize_profile="balanced")
        self.assertFalse(result["dryRun"])
        self.assertEqual(result["humanize"]["profile"], "balanced")
        self.assertGreater(result["humanize"]["steps"], 3)
        self.assertGreater(controller._send.call_count, 3)
        self.assertGreater(controller._sleep.call_count, 0)

    def test_precise_drag_injects_and_releases(self):
        controller = self._controller()
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(40, 40)):
            result = controller.drag(100, 100, 260, 220, humanize_profile="precise")
        self.assertFalse(result["dryRun"])
        self.assertEqual(result["humanize"]["segments"], 2)
        self.assertGreater(controller._send.call_count, 4)
        # The final event batch is the release at the exact endpoint.
        self.assertGreater(controller._send.call_count, len(result["humanize"]))

    def test_macro_coordinate_click_uses_humanized_path(self):
        controller = self._controller()
        with mock.patch("vhcu_daemon.input.cursor_position", return_value=(40, 40)):
            result = controller.run_macro(
                [{"op": "click", "x": 300, "y": 220}],
                humanize_profile="balanced",
            )
        self.assertFalse(result["dryRun"])
        self.assertEqual(result["humanizeProfile"], "balanced")
        self.assertGreater(controller._send.call_count, 3)


if __name__ == "__main__":
    unittest.main()


class ImeTest(unittest.TestCase):
    def test_ime_status_is_readonly_and_never_raises(self):
        from vhcu_daemon.input import ime_open_status

        result = ime_open_status()
        self.assertIn(result, (True, False, None))

    def test_dry_run_type_reports_ime_fields(self):
        controller = InputController(dry_run=True)
        result = controller.type_text("中文")
        self.assertIn("imeOpen", result)
        self.assertFalse(result["imeToggled"])

    def test_p242_clipboard_fallback_is_explicit_in_dry_run(self):
        controller = InputController(dry_run=True)
        result = controller.type_text("Blender text", use_clipboard_fallback=True)
        self.assertTrue(result["dryRun"])
        self.assertTrue(result["clipboardFallback"])
        normal = controller.type_text("plain")
        self.assertFalse(normal["clipboardFallback"])


class MacroTest(unittest.TestCase):
    def test_macro_open_notepad_dry_run(self):
        controller = InputController(dry_run=True)
        result = controller.macro_open_notepad_and_type("hello")
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["action"], "macro.open_notepad_and_type")
        self.assertEqual(result["chars"], 5)
        self.assertEqual(result["steps"], ["win+r", "type:notepad", "enter", "wait:1000", "type:text"])

    def test_macro_open_notepad_rejects_empty(self):
        controller = InputController(dry_run=True)
        with self.assertRaises(InputError) as ctx:
            controller.macro_open_notepad_and_type("")
        self.assertEqual(ctx.exception.code, "E_PROTO")

    def test_macro_save_dry_run(self):
        controller = InputController(dry_run=True)
        result = controller.macro_save_file()
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["steps"], ["ctrl+s"])


class MacroRunnerTest(unittest.TestCase):
    def test_run_macro_dry_run_lists_steps(self):
        controller = InputController(dry_run=True)
        result = controller.run_macro(
            [
                {"op": "key", "keys": ["ctrl", "s"]},
                {"op": "wait", "ms": 10},
            ]
        )
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["steps"], ["key", "wait"])
        self.assertEqual(result["count"], 2)

    def test_run_macro_rejects_bad_shape(self):
        controller = InputController(dry_run=True)
        with self.assertRaises(InputError) as ctx:
            controller.run_macro([])
        self.assertEqual(ctx.exception.code, "E_PROTO")

    def test_p6_1280x720_to_1920x1080_is_exact_1_5(self):
        controller = InputController(dry_run=True)
        result = controller.click(100, 50, scale_x=1.5, scale_y=1.5)
        self.assertEqual((result["x"], result["y"]), (150, 75))
        self.assertEqual((result["scaleX"], result["scaleY"]), (1.5, 1.5))
        self.assertEqual(result["scaleFactor"], 1.5)

    def test_p6_independent_scale_axes(self):
        controller = InputController(dry_run=True)
        result = controller.click(100, 50, scale_x=2.0, scale_y=1.5)
        self.assertEqual((result["x"], result["y"]), (200, 75))
        self.assertEqual((result["scaleX"], result["scaleY"]), (2.0, 1.5))
        self.assertIsNone(result["scaleFactor"])

    def test_p6_invalid_scale_is_protocol_error(self):
        controller = InputController(dry_run=True)
        with self.assertRaises(InputError) as raised:
            controller.click(10, 10, scale_x=0, scale_y=1)
        self.assertEqual(raised.exception.code, "E_PROTO")
        with self.assertRaises(InputError):
            controller.click(10, 10, scale_x=1.5, scale_y=-1)

    def test_click_scales_image_coordinates(self):
        controller = InputController(dry_run=True)
        result = controller.click(100, 120, 2.5)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["requested"], [100, 120])
        self.assertEqual(result["scaleFactor"], 2.5)
        self.assertEqual(result["applied"], [250, 300])
        self.assertEqual((result["x"], result["y"]), (250, 300))

    def test_click_rejects_bad_scale(self):
        controller = InputController(dry_run=True)
        with self.assertRaises(InputError) as ctx:
            controller.click(1, 1, 0)
        self.assertEqual(ctx.exception.code, "E_PROTO")

    def test_run_macro_replaces_multiple_variables(self):
        controller = InputController(dry_run=True)
        result = controller.run_macro(
            [{"op": "type", "text": "{{a}}/{{b}}"}, {"op": "wait", "ms": 5}],
            {"a": "D:\\", "b": "VHCU"},
        )
        self.assertEqual(result["texts"], ["D:\/VHCU"])
        self.assertEqual(result["variables"], ["a", "b"])

    def test_run_macro_missing_variable_fails(self):
        controller = InputController(dry_run=True)
        with self.assertRaises(InputError) as ctx:
            controller.run_macro([{"op": "type", "text": "{{missing}}"}], {})
        self.assertEqual(ctx.exception.code, "E_PROTO")

    def test_run_macro_mouse_coords_and_scale(self):
        controller = InputController(dry_run=True)
        result = controller.run_macro(
            [
                {"op": "double_click", "x": "{{x}}", "y": "{{y}}"},
                {"op": "right_click", "x": "{{x}}", "y": "{{y}}"},
                {"op": "drag", "x1": "{{x1}}", "y1": "{{y1}}", "x2": "{{x2}}", "y2": "{{y2}}"},
            ],
            {"x": "100", "y": "50", "x1": "10", "y1": "20", "x2": "30", "y2": "40"},
            2.0,
        )
        self.assertEqual(result["coords"], [[200, 100], [200, 100], [20, 40, 60, 80]])
        self.assertEqual(result["scaleFactor"], 2.0)
        self.assertEqual(result["steps"], ["double_click", "right_click", "drag"])

    def test_p24_run_macro_supports_middle_drag(self):
        controller = InputController(dry_run=True)
        result = controller.run_macro(
            [{"op": "middle_drag", "x1": 10, "y1": 20, "x2": 100, "y2": 200}]
        )
        self.assertEqual(result["steps"], ["middle_drag"])
        self.assertEqual(result["coords"], [[10, 20, 100, 200]])

    def test_run_macro_bad_coordinate_variable(self):
        controller = InputController(dry_run=True)
        with self.assertRaises(InputError) as ctx:
            controller.run_macro([{"op": "double_click", "x": "{{x}}", "y": "{{y}}"}], {"x": "abc", "y": "10"})
        self.assertEqual(ctx.exception.code, "E_PROTO")

    def test_snap_and_close_hotkeys_parse(self):
        self.assertEqual(len(parse_hotkey(["win", "left"])), 2)
        self.assertEqual(len(parse_hotkey(["win", "right"])), 2)
        self.assertEqual(len(parse_hotkey(["alt", "f4"])), 2)

    def test_dry_run_click_clamps_and_warns(self):
        controller = InputController(dry_run=True)
        result = controller.click(-10, controller.height + 50)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["applied"], [0, controller.height - 1])
        self.assertIn("坐标越界", result["warning"])

    def test_macro_coordinates_clamp_with_warning(self):
        controller = InputController(dry_run=True)
        result = controller.run_macro(
            [{"op": "click", "x": -10, "y": controller.height + 50}]
        )
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["coords"], [[0, controller.height - 1]])
        self.assertIn("坐标越界", result["warnings"][0])


class FocusLostTest(unittest.TestCase):
    def test_changed_foreground_raises_focus_lost_before_send(self):
        from unittest import mock

        controller = InputController(dry_run=True)
        controller.dry_run = False
        with mock.patch(
            "vhcu_daemon.input.foreground_identity",
            return_value={"hwnd": 99, "pid": 1, "title": "Other"},
        ):
            with self.assertRaises(InputError) as ctx:
                controller.assert_foreground({"hwnd": 42, "pid": 2, "title": "Expected"})
        self.assertEqual(ctx.exception.code, "E_FOCUS_LOST")
        self.assertIn("vhcu_get_full_frame", ctx.exception.hint or "")

    def test_same_foreground_is_allowed(self):
        from unittest import mock

        controller = InputController(dry_run=True)
        controller.dry_run = False
        with mock.patch(
            "vhcu_daemon.input.foreground_identity",
            return_value={"hwnd": 42, "pid": 2, "title": "Same"},
        ):
            controller.assert_foreground({"hwnd": 42, "pid": 2, "title": "Expected"})
