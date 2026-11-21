"""P6 overlay contract tests that do not require an interactive desktop."""

from __future__ import annotations

from pathlib import Path
import unittest

from vhcu_daemon.cursor_overlay import (
    ACTION_COLORS,
    ACTION_RGB,
    CURSOR_DIP,
    OUTLINE_ARGB,
    OUTLINE_DIP,
    WINDOW_DIP,
    CursorOverlay,
)

SOURCE = Path(__file__).resolve().parents[1] / "vhcu_daemon" / "cursor_overlay.py"


class CursorOverlayContractTest(unittest.TestCase):
    def test_action_status_colors(self):
        self.assertEqual(ACTION_RGB["move"], (0x00, 0xE5, 0xFF))
        self.assertEqual(ACTION_RGB["click"], (0xFF, 0x3B, 0x30))
        self.assertEqual(ACTION_RGB["drag"], (0x34, 0xC7, 0x59))
        self.assertEqual(ACTION_COLORS["move"] & 0xFF, 0xFF)
        self.assertNotEqual(ACTION_COLORS["move"], ACTION_COLORS["click"])

    def test_small_cursor_and_window(self):
        self.assertEqual(CURSOR_DIP, 24)
        self.assertEqual(WINDOW_DIP, 96)
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("UpdateLayeredWindow", source)
        self.assertIn("WS_EX_TRANSPARENT", source)
        self.assertIn("WS_EX_LAYERED", source)
        self.assertNotIn("GdipDrawString", source)
        self.assertNotIn("TextOut", source)
        self.assertNotIn("DrawText", source)
        self.assertNotIn("SetLayeredWindowAttributes", source)

    def test_tip_is_exact_and_body_points_into_second_quadrant(self):
        overlay = CursorOverlay()
        tip, left, right = overlay._triangle_points(100, 200)
        self.assertEqual(tip, (100.0, 200.0))
        self.assertGreater(left[0], tip[0])
        self.assertGreater(left[1], tip[1])
        self.assertGreater(right[0], tip[0])
        self.assertGreater(right[1], tip[1])

    def test_overlay_is_persistent_until_explicit_hide(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("IDLE_HIDE_SECONDS", source)
        self.assertNotIn("ShowWindow(hwnd, SW_HIDE)", source)
        self.assertIn("self._animate_if_needed()", source)

    def test_png_tint_and_gdi_vector_fallback_are_present(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("assets", source)
        self.assertIn("cursor.png", source)
        self.assertIn("GdipSetImageAttributesColorMatrix", source)
        self.assertIn("GdipFillPath", source)
        self.assertIn("GdipDrawEllipse", source)

    def test_white_rim_is_limited_to_solid_cursor(self):
        self.assertEqual(OUTLINE_DIP, 1.5)
        self.assertEqual(OUTLINE_ARGB, 0xFFFFFFFF)
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("GdipDrawPath.argtypes", source)
        self.assertIn("def _stroke_path", source)
        self.assertIn("def _stroke_ellipse", source)
        self.assertIn("self._stroke_path(gp, path, OUTLINE_ARGB, OUTLINE_DIP * scale)", source)
        # The enlarged halo must not be stroked: that produced a stray white
        # line under the pointer in the field.
        self.assertNotIn("outline=index == 0", source)
        self.assertIn("self._fill_rounded(gp, points, _rgb_argb(rgb, alpha), 4.0 * self._scale)", source)
        self.assertIn("OUTLINE_ARGB, OUTLINE_DIP * self._scale", source)


if __name__ == "__main__":
    unittest.main()
