"""Cross-platform input safety primitives.

These functions contain no OS calls and no SendInput code. Windows and future
platform drivers share the same coordinate clamping, corner failsafe and
normalization rules.
"""

from __future__ import annotations

DEFAULT_CORNER_MARGIN = 8


class InvalidScreen(ValueError):
    pass


def clamp_point(x: int, y: int, width: int, height: int) -> tuple[int, int, bool]:
    if width <= 1 or height <= 1:
        raise InvalidScreen("primary screen size is unavailable")
    cx = min(max(0, int(x)), width - 1)
    cy = min(max(0, int(y)), height - 1)
    return cx, cy, (cx != int(x) or cy != int(y))


def normalize_point(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    if width <= 1 or height <= 1:
        raise InvalidScreen("primary screen size is unavailable")
    cx, cy, _ = clamp_point(x, y, width, height)
    return cx, cy


def in_screen_corner(
    x: int,
    y: int,
    width: int,
    height: int,
    margin: int = DEFAULT_CORNER_MARGIN,
) -> bool:
    if width <= 0 or height <= 0:
        return False
    left = x <= margin
    right = x >= width - 1 - margin
    top = y <= margin
    bottom = y >= height - 1 - margin
    return (left or right) and (top or bottom)


__all__ = [
    "DEFAULT_CORNER_MARGIN",
    "InvalidScreen",
    "clamp_point",
    "in_screen_corner",
    "normalize_point",
]
