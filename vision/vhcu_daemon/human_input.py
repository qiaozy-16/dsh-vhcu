"""Pure-humanization trajectory generator for coordinate mouse actions.

No Win32/ctypes imports here: this module only maps a start/end point to a
sequence of physical-screen points. InputController is responsible for
SendInput and interruptible sleeps. Keeping it pure makes the curve/jitter
logic deterministic and unit-testable.

Profiles:
  off       caller bypasses this module (legacy instant movement)
  precise   short, low-step straight line; no jitter
  balanced  cubic Bezier + ease-in-out, adaptive timing, low-frequency jitter
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Optional, Sequence

PROFILES = ("off", "precise", "balanced")
DEFAULT_PROFILE = "balanced"
END_SNAP_PX = 10.0
BALANCED_MIN_MS = 100
BALANCED_MAX_MS = 350
PRECISE_MIN_MS = 30
PRECISE_MAX_MS = 80
VIRTUAL_MIN_MS = 80
VIRTUAL_MAX_MS = 100


class HumanizeError(ValueError):
    """Invalid profile/coordinates for the pure trajectory generator."""

    code = "E_PROTO"


@dataclass(frozen=True)
class PathPoint:
    x: int
    y: int
    at_ms: float


@dataclass(frozen=True)
class Trajectory:
    profile: str
    start: tuple[int, int]
    end: tuple[int, int]
    points: tuple[PathPoint, ...]
    duration_ms: int
    distance_px: float
    jitter_px: float

    def metadata(self) -> dict:
        return {
            "profile": self.profile,
            "start": [self.start[0], self.start[1]],
            "endpoint": [self.end[0], self.end[1]],
            "duration_ms": self.duration_ms,
            "steps": len(self.points),
            "distance_px": round(self.distance_px, 1),
            "jitter_px": round(self.jitter_px, 2),
        }


def normalize_profile(value: Optional[str], default: str = "off") -> str:
    """Validate a profile name; None/empty uses the supplied default."""
    if value is None or value == "":
        value = default
    profile = str(value).strip().lower()
    if profile not in PROFILES:
        raise HumanizeError(
            f"humanizeProfile must be one of {', '.join(PROFILES)}"
        )
    return profile


def _clamp_point(x: float, y: float, screen: Optional[Sequence[int]]) -> tuple[int, int]:
    xi, yi = int(round(x)), int(round(y))
    if screen and len(screen) >= 2:
        width, height = int(screen[0]), int(screen[1])
        if width > 1:
            xi = max(0, min(width - 1, xi))
        if height > 1:
            yi = max(0, min(height - 1, yi))
    return xi, yi


def _ease_in_out(t: float) -> float:
    t = max(0.0, min(1.0, float(t)))
    return t * t * (3.0 - 2.0 * t)


def virtual_duration_ms(distance: float) -> int:
    """High-performance virtual cursor: 80ms short, at most 100ms far."""
    return int(round(min(VIRTUAL_MAX_MS, max(VIRTUAL_MIN_MS, 80.0 + max(0.0, float(distance)) * 0.02))))


def _duration_ms(profile: str, distance: float,
                 duration_override_ms: Optional[float] = None) -> int:
    if duration_override_ms is not None:
        return max(1, int(round(float(duration_override_ms))))
    if profile == "precise":
        return int(round(min(PRECISE_MAX_MS, max(PRECISE_MIN_MS, 30.0 + distance * 0.05))))
    return int(round(min(
        BALANCED_MAX_MS,
        max(BALANCED_MIN_MS, 80.0 + distance * 0.15),
    )))


def _step_count(profile: str, duration_ms: int, distance: float) -> int:
    if profile == "precise":
        return max(4, min(6, int(round(duration_ms / 18.0)) + (1 if distance > 900 else 0)))
    # Keep the path visually smooth without burning 12+ event-loop turns on
    # a 100-350ms humanized movement.
    return max(6, min(24, int(round(duration_ms / 24.0)) + 2))


def _bezier(p0, p1, p2, p3, progress: float) -> tuple[float, float]:
    t = max(0.0, min(1.0, progress))
    u = 1.0 - t
    x = (u ** 3) * p0[0] + 3 * (u ** 2) * t * p1[0] + 3 * u * (t ** 2) * p2[0] + (t ** 3) * p3[0]
    y = (u ** 3) * p0[1] + 3 * (u ** 2) * t * p1[1] + 3 * u * (t ** 2) * p2[1] + (t ** 3) * p3[1]
    return x, y


def build_trajectory(
    start: tuple[int, int],
    end: tuple[int, int],
    profile: str = DEFAULT_PROFILE,
    *,
    seed: Optional[int] = None,
    screen: Optional[Sequence[int]] = None,
    duration_override_ms: Optional[float] = None,
) -> Optional[Trajectory]:
    """Build one humanized path from start to end.

    Returns None for profile='off'. The final point is always exactly `end`
    (clamped to the screen when screen dimensions are supplied).
    """
    name = normalize_profile(profile)
    if name == "off":
        return None

    try:
        sx, sy = int(start[0]), int(start[1])
        ex, ey = int(end[0]), int(end[1])
    except (TypeError, ValueError, IndexError) as error:
        raise HumanizeError("start/end must be integer x,y pairs") from error
    ex, ey = _clamp_point(ex, ey, screen)
    sx, sy = _clamp_point(sx, sy, screen)
    start_point = (sx, sy)
    end_point = (ex, ey)
    dx, dy = ex - sx, ey - sy
    distance = math.hypot(dx, dy)
    duration_ms = _duration_ms(name, distance, duration_override_ms)
    if distance <= 0.0:
        return Trajectory(
            profile=name,
            start=start_point,
            end=end_point,
            points=(PathPoint(ex, ey, 0.0),),
            duration_ms=0,
            distance_px=0.0,
            jitter_px=0.0,
        )

    rng = random.Random(seed)
    steps = _step_count(name, duration_ms, distance)
    points: list[PathPoint] = []
    jitter_px = 0.0

    if name == "precise":
        # Spatial straight line, eased timing, no lateral noise.
        for index in range(1, steps + 1):
            t = index / steps
            e = _ease_in_out(t)
            x, y = _clamp_point(sx + dx * e, sy + dy * e, screen)
            points.append(PathPoint(x, y, round(duration_ms * t, 3)))
        # Always land exactly, independent of rounding.
        points[-1] = PathPoint(ex, ey, float(duration_ms))
        return Trajectory(
            profile=name,
            start=start_point,
            end=end_point,
            points=tuple(points),
            duration_ms=duration_ms,
            distance_px=distance,
            jitter_px=0.0,
        )

    # balanced: a gentle cubic Bezier arc, then an eased approach to target.
    jitter_px = 1.8
    nx, ny = dx / distance, dy / distance
    perp_x, perp_y = -ny, nx
    side = 1.0 if rng.random() < 0.5 else -1.0
    curve1 = distance * rng.uniform(0.06, 0.15) * side
    curve2 = distance * rng.uniform(0.04, 0.11) * side
    p1 = (sx + dx * 0.33 + perp_x * curve1,
          sy + dy * 0.33 + perp_y * curve1)
    p2 = (ex - dx * 0.33 + perp_x * curve2,
          ey - dy * 0.33 + perp_y * curve2)
    if screen and len(screen) >= 2:
        p1 = _clamp_point(p1[0], p1[1], screen)
        p2 = _clamp_point(p2[0], p2[1], screen)

    phase1 = rng.uniform(0.0, math.tau)
    phase2 = rng.uniform(0.0, math.tau)
    snap_px = min(END_SNAP_PX, max(8.0, distance * 0.12))
    for index in range(1, steps + 1):
        t = index / steps
        e = _ease_in_out(t)
        x, y = _bezier(start_point, p1, p2, end_point, e)
        remaining = math.hypot(ex - x, ey - y)
        if remaining > snap_px:
            fade = max(0.0, 1.0 - e) ** 1.6
            wobble = 0.62 * math.sin(math.tau * 2.3 * t + phase1)
            wobble += 0.38 * math.sin(math.tau * 4.1 * t + phase2)
            x += perp_x * jitter_px * wobble * fade
            y += perp_y * jitter_px * wobble * fade
        px, py = _clamp_point(x, y, screen)
        points.append(PathPoint(px, py, round(duration_ms * t, 3)))
    # Exact endpoint; no jitter/rounding may change the final click point.
    points[-1] = PathPoint(ex, ey, float(duration_ms))
    return Trajectory(
        profile=name,
        start=start_point,
        end=end_point,
        points=tuple(points),
        duration_ms=duration_ms,
        distance_px=distance,
        jitter_px=jitter_px,
    )


__all__ = [
    "BALANCED_MAX_MS",
    "BALANCED_MIN_MS",
    "DEFAULT_PROFILE",
    "END_SNAP_PX",
    "HumanizeError",
    "PRECISE_MAX_MS",
    "PRECISE_MIN_MS",
    "PROFILES",
    "PathPoint",
    "Trajectory",
    "VIRTUAL_MAX_MS",
    "VIRTUAL_MIN_MS",
    "virtual_duration_ms",
    "build_trajectory",
    "normalize_profile",
]
