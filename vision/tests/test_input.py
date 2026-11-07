"""Unit tests for the P5 SendInput layer (no real input is injected).

Run: py -3 -m unittest discover -s vision/tests -t vision -v
"""

from __future__ import annotations

import os
import unittest

from vhcu_daemon.input import (
    MAX_TEXT_CHARS,
    InputController,
    InputError,
    clamp_click,
    in_corner,
    normalize_click,
    parse_hotkey,
    validate_text,
)

WINDOWS = os.name == "nt"


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

    def test_status_reports_screen_and_dpi(self):
        status = self.controller.status()
        self.assertGreater(status["screen"]["width"], 0)
        self.assertGreater(status["screen"]["height"], 0)
        self.assertTrue(status["dryRun"])
        self.assertIn(status["dpiMode"], {"per-monitor-v2", "per-monitor", "system", "none"})


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
