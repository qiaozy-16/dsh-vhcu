"""
@file shapes.py -- pure geometry point generators for vhcu_draw_shape.

No Win32/SendInput here. The controller turns these high-density points into
a continuous mouse stroke. Keeping the math pure makes the output testable.
"""

from __future__ import annotations

import math
from typing import Iterable

SHAPES = ("line", "parabola", "sine", "cosine")
MAX_SAMPLES = 512
DEFAULT_SAMPLES = 160
MAX_CURVE = 4000
MAX_DURATION_MS = 5000


class ShapeError(ValueError):
    code = "E_PROTO"


def _number(value, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ShapeError(f"{name} must be a number") from error
    if not math.isfinite(parsed):
        raise ShapeError(f"{name} must be finite")
    return parsed


def build_shape(
    shape: str,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    *,
    curve: float = 120.0,
    samples: int = DEFAULT_SAMPLES,
    duration_ms: int = 500,
    screen: tuple[int, int] | None = None,
) -> dict:
    """Generate a smooth, screen-clamped point path.

    `curve` is the signed perpendicular amplitude in pixels:
      parabola: 4*t*(1-t)
      sine/cosine: sin/cos over one full wave.
    """
    kind = str(shape or "").strip().lower()
    if kind not in SHAPES:
        raise ShapeError(f"shape must be one of {', '.join(SHAPES)}")
    sx, sy = _number(start_x, "start_x"), _number(start_y, "start_y")
    ex, ey = _number(end_x, "end_x"), _number(end_y, "end_y")
    amplitude = _number(curve, "curve")
    if abs(amplitude) > MAX_CURVE:
        raise ShapeError(f"curve must be within ±{MAX_CURVE}")
    try:
        count = int(samples)
        duration = int(duration_ms)
    except (TypeError, ValueError) as error:
        raise ShapeError("samples and duration_ms must be integers") from error
    if not (2 <= count <= MAX_SAMPLES):
        raise ShapeError(f"samples must be within 2..{MAX_SAMPLES}")
    if not (16 <= duration <= MAX_DURATION_MS):
        raise ShapeError(f"duration_ms must be within 16..{MAX_DURATION_MS}")

    dx, dy = ex - sx, ey - sy
    distance = math.hypot(dx, dy)
    if distance <= 1e-9:
        return {
            "shape": kind,
            "points": [(int(round(sx)), int(round(sy)))],
            "samples": 1,
            "duration_ms": 0,
            "curve": amplitude,
        }
    nx, ny = dx / distance, dy / distance
    px, py = -ny, nx
    points: list[tuple[int, int]] = []
    for index in range(count):
        t = index / (count - 1)
        offset = 0.0
        if kind == "parabola":
            offset = amplitude * 4.0 * t * (1.0 - t)
        elif kind == "sine":
            offset = amplitude * math.sin(math.tau * t)
        elif kind == "cosine":
            offset = amplitude * (math.cos(math.tau * t) - 1.0) * 0.5
        x = sx + dx * t + px * offset
        y = sy + dy * t + py * offset
        if screen and len(screen) >= 2:
            width, height = int(screen[0]), int(screen[1])
            if width > 1:
                x = min(max(0.0, x), width - 1)
            if height > 1:
                y = min(max(0.0, y), height - 1)
        point = (int(round(x)), int(round(y)))
        if not points or point != points[-1]:
            points.append(point)
    return {
        "shape": kind,
        "points": points,
        "samples": len(points),
        "duration_ms": duration,
        "curve": amplitude,
        "start": [int(round(sx)), int(round(sy))],
        "end": [int(round(ex)), int(round(ey))],
    }


def iter_segments(points: Iterable[tuple[int, int]]) -> Iterable[tuple[tuple[int, int], tuple[int, int]]]:
    iterator = iter(points)
    try:
        previous = next(iterator)
    except StopIteration:
        return
    for current in iterator:
        yield previous, current
        previous = current


__all__ = ["DEFAULT_SAMPLES", "MAX_SAMPLES", "SHAPES", "ShapeError", "build_shape", "iter_segments"]
