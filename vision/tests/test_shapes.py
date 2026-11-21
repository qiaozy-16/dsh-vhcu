"""Pure geometry tests for vhcu_draw_shape."""

from __future__ import annotations

import unittest

from vhcu_daemon.shapes import ShapeError, build_shape


class ShapeTest(unittest.TestCase):
    def test_parabola_is_dense_and_lands_exactly(self):
        result = build_shape("parabola", 100, 100, 600, 100, curve=200, samples=160)
        self.assertEqual(result["shape"], "parabola")
        self.assertEqual(result["points"][0], (100, 100))
        self.assertEqual(result["points"][-1], (600, 100))
        self.assertGreaterEqual(result["samples"], 100)
        self.assertEqual(len(result["points"]), result["samples"])
        self.assertGreater(max(y for _x, y in result["points"]), 190)

    def test_sine_has_both_sides_of_baseline(self):
        result = build_shape("sine", 0, 500, 1000, 500, curve=80, samples=64)
        ys = [y for _x, y in result["points"]]
        self.assertLess(min(ys), 500)
        self.assertGreater(max(ys), 500)
        self.assertEqual(result["points"][0], (0, 500))
        self.assertEqual(result["points"][-1], (1000, 500))

    def test_screen_clamp_and_validation(self):
        result = build_shape("parabola", 0, 0, 100, 0, curve=100, samples=32, screen=(64, 64))
        self.assertTrue(all(0 <= x < 64 and 0 <= y < 64 for x, y in result["points"]))
        for bad in (
            {"shape": "triangle", "samples": 32},
            {"shape": "line", "samples": 1},
            {"shape": "line", "samples": 513},
            {"shape": "line", "curve": 99999},
        ):
            with self.assertRaises(ShapeError):
                build_shape(bad.pop("shape", "line"), 0, 0, 100, 0, **bad)


if __name__ == "__main__":
    unittest.main()
