"""Pure humanized mouse trajectory tests (no OS/SendInput)."""

from __future__ import annotations

import math
import unittest

from vhcu_daemon.human_input import (
    BALANCED_MAX_MS,
    BALANCED_MIN_MS,
    END_SNAP_PX,
    HumanizeError,
    PRECISE_MAX_MS,
    PRECISE_MIN_MS,
    VIRTUAL_MAX_MS,
    VIRTUAL_MIN_MS,
    build_trajectory,
    normalize_profile,
    virtual_duration_ms,
)


class ProfileTest(unittest.TestCase):
    def test_profiles_and_invalid(self):
        self.assertEqual(normalize_profile(None), "off")
        self.assertEqual(normalize_profile("BALANCED"), "balanced")
        self.assertEqual(normalize_profile("precise"), "precise")
        with self.assertRaises(HumanizeError):
            normalize_profile("robot")


class TrajectoryTest(unittest.TestCase):
    start = (100, 100)
    end = (900, 620)

    def test_off_returns_none(self):
        self.assertIsNone(build_trajectory(self.start, self.end, "off"))

    def test_precise_is_short_straight_and_exact(self):
        trajectory = build_trajectory(self.start, self.end, "precise", seed=3)
        self.assertIsNotNone(trajectory)
        self.assertGreaterEqual(trajectory.duration_ms, PRECISE_MIN_MS)
        self.assertLessEqual(trajectory.duration_ms, PRECISE_MAX_MS)
        self.assertGreaterEqual(len(trajectory.points), 4)
        self.assertLessEqual(len(trajectory.points), 6)
        self.assertEqual(trajectory.points[-1].x, self.end[0])
        self.assertEqual(trajectory.points[-1].y, self.end[1])
        self.assertEqual(trajectory.jitter_px, 0.0)
        dx = self.end[0] - self.start[0]
        dy = self.end[1] - self.start[1]
        distance = math.hypot(dx, dy)
        for point in trajectory.points[:-1]:
            cross = abs((point.x - self.start[0]) * dy - (point.y - self.start[1]) * dx) / distance
            self.assertLessEqual(cross, 1, "precise profile must be spatially straight")

    def test_balanced_duration_steps_and_endpoint(self):
        trajectory = build_trajectory(self.start, self.end, "balanced", seed=7)
        self.assertIsNotNone(trajectory)
        self.assertGreaterEqual(trajectory.duration_ms, BALANCED_MIN_MS)
        self.assertLessEqual(trajectory.duration_ms, BALANCED_MAX_MS)
        self.assertGreaterEqual(len(trajectory.points), 6)
        self.assertLessEqual(len(trajectory.points), 24)
        self.assertEqual(trajectory.points[-1].x, self.end[0])
        self.assertEqual(trajectory.points[-1].y, self.end[1])
        self.assertGreater(trajectory.jitter_px, 0.0)

    def test_balanced_is_fast_clamped_formula(self):
        near = build_trajectory((0, 0), (100, 0), "balanced", seed=1)
        mid = build_trajectory((0, 0), (1000, 0), "balanced", seed=1)
        far = build_trajectory((0, 0), (5000, 0), "balanced", seed=1)
        self.assertEqual(near.duration_ms, BALANCED_MIN_MS)
        self.assertEqual(mid.duration_ms, 230)
        self.assertEqual(far.duration_ms, BALANCED_MAX_MS)
        self.assertLessEqual(far.duration_ms, 350)

    def test_virtual_duration_is_80_to_120(self):
        self.assertEqual(virtual_duration_ms(0), VIRTUAL_MIN_MS)
        self.assertEqual(virtual_duration_ms(1000), 100)
        self.assertEqual(virtual_duration_ms(10000), VIRTUAL_MAX_MS)
        for distance in (0, 10, 100, 500, 1000, 5000):
            self.assertGreaterEqual(virtual_duration_ms(distance), 80)
            self.assertLessEqual(virtual_duration_ms(distance), 120)

    def test_balanced_seed_is_deterministic(self):
        one = build_trajectory(self.start, self.end, "balanced", seed=11)
        two = build_trajectory(self.start, self.end, "balanced", seed=11)
        self.assertEqual([(p.x, p.y) for p in one.points], [(p.x, p.y) for p in two.points])

    def test_time_is_monotonic(self):
        trajectory = build_trajectory(self.start, self.end, "balanced", seed=5)
        times = [point.at_ms for point in trajectory.points]
        self.assertEqual(times, sorted(times))
        self.assertEqual(times[-1], float(trajectory.duration_ms))
        intervals = [b - a for a, b in zip(times, times[1:])]
        self.assertLess(max(intervals) - min(intervals), 0.002,
                        "sleep budget must be evenly distributed")

    def test_screen_clamp(self):
        trajectory = build_trajectory((-100, -100), (9999, 9999), "balanced",
                                      seed=2, screen=(1920, 1080))
        self.assertTrue(all(0 <= point.x < 1920 for point in trajectory.points))
        self.assertTrue(all(0 <= point.y < 1080 for point in trajectory.points))
        self.assertEqual((trajectory.points[-1].x, trajectory.points[-1].y), (1919, 1079))

    def test_same_point_has_single_exact_point(self):
        trajectory = build_trajectory((50, 50), (50, 50), "balanced")
        self.assertEqual(trajectory.duration_ms, 0)
        self.assertEqual(len(trajectory.points), 1)
        self.assertEqual((trajectory.points[0].x, trajectory.points[0].y), (50, 50))

    def test_jitter_fades_at_end(self):
        trajectory = build_trajectory(self.start, self.end, "balanced", seed=9)
        # The final END_SNAP_PX approach must be the exact requested point and
        # should not be moved by the low-frequency wobble.
        self.assertLessEqual(
            math.hypot(trajectory.points[-1].x - self.end[0],
                       trajectory.points[-1].y - self.end[1]),
            0.0,
        )
        self.assertGreaterEqual(END_SNAP_PX, 8.0)


if __name__ == "__main__":
    unittest.main()
