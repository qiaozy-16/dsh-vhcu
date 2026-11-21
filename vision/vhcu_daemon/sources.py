"""Frame sources: a deterministic fake (CI/tests) and mss (real capture)."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Frame:
    width: int
    height: int
    channels: int  # BGRA
    data: bytes
    scale: float = 1.0
    backend: str = "unknown"
    blank: bool = False
    occluded: bool = False
    frame_seq: int = 0
    target_hwnd: Optional[int] = None
    origin_x: int = 0
    origin_y: int = 0

    @property
    def nbytes(self) -> int:
        return len(self.data)


class SourceUnavailable(RuntimeError):
    def __init__(self, message: str, hint: str):
        super().__init__(message)
        self.code = "E_UNAVAILABLE"
        self.hint = hint


class CaptureError(RuntimeError):
    def __init__(self, message: str, code: str = "E_CAPTURE", hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.hint = hint


class FakeSource:
    """Moving 32x32 block on a dark canvas.

    The block moves on two out of three frames; the third repeat produces an
    identical frame, which is what proves "no change => no event".
    """

    name = "fake"

    def __init__(self, width: int = 320, height: int = 180, block: int = 32):
        self.width = max(32, int(width))
        self.height = max(32, int(height))
        self.block = max(8, min(int(block), min(self.width, self.height) // 2))
        self.tick = 0

    def grab(self) -> Frame:
        width, height, block = self.width, self.height, self.block
        buf = bytearray(b"\x18\x18\x18\xff" * (width * height))
        # frameNo/3 drives the motion so every third grab is identical.
        step = self.tick // 3
        x = 4 + (step * 6) % max(1, width - block - 8)
        y = 4 + (step * 4) % max(1, height - block - 8)
        for row in range(y, min(y + block, height)):
            start = (row * width + x) * 4
            end = (row * width + min(x + block, width)) * 4
            buf[start:end] = b"\xf0\xf0\xf0\xff" * ((end - start) // 4)
        self.tick += 1
        return Frame(
            width=width,
            height=height,
            channels=4,
            data=bytes(buf),
            scale=1.0,
            backend="fake",
            frame_seq=0,
        )


class MssSource:
    """Virtual-desktop or monitor capture via mss (BitBlt). Optional dependency."""

    name = "mss"

    def __init__(self, monitor: int = 0, **_ignored):
        # mss must see physical pixels. The daemon normally becomes DPI-aware
        # when InputController is constructed, but a direct source/CLI use
        # must not silently capture a 1536-wide logical frame instead of 1920.
        try:
            from .win_capture import ensure_dpi_awareness
            ensure_dpi_awareness()
        except Exception:  # noqa: BLE001 - non-Windows/fake tests
            pass
        try:
            import mss  # type: ignore
        except Exception as error:  # noqa: BLE001
            raise SourceUnavailable(
                f"mss is not installed ({error})",
                "在 VHCU 使用的 Python venv 里执行: pip install mss",
            ) from error
        self._mss = mss.mss()
        try:
            self._monitor = self._mss.monitors[monitor]
        except IndexError as error:
            raise SourceUnavailable(
                f"monitor {monitor} does not exist",
                "检查显示器连接或改用 --monitor 1",
            ) from error
        self.width = int(self._monitor["width"])
        self.height = int(self._monitor["height"])
        self.origin_x = int(self._monitor.get("left", 0))
        self.origin_y = int(self._monitor.get("top", 0))

    def grab(self) -> Frame:
        try:
            shot = self._mss.grab(self._monitor)
        except Exception as error:  # noqa: BLE001
            raise CaptureError(f"mss grab failed: {error}") from error
        # mss returns BGRA in .raw
        return Frame(
            width=shot.width,
            height=shot.height,
            channels=4,
            data=bytes(shot.raw),
            scale=1.0,
            backend="mss",
            frame_seq=0,
            origin_x=self.origin_x,
            origin_y=self.origin_y,
        )

    def close(self) -> None:
        try:
            self._mss.close()
        except Exception:  # noqa: BLE001
            pass



class WindowSource:
    """Adapter from a target HWND to a Frame; implemented in win_capture."""

    name = "window"

    def __init__(self, *, target_hwnd: int, **options):
        from .win_capture import capture_window
        self._capture_window = capture_window
        self.target_hwnd = int(target_hwnd)
        self.strict_occlusion = bool(options.get("strict_occlusion", False))
        self.width = 0
        self.height = 0

    def grab(self) -> Frame:
        try:
            image = self._capture_window(
                self.target_hwnd,
                strict_occlusion=self.strict_occlusion,
            )
        except Exception as error:  # noqa: BLE001
            code = getattr(error, "code", "E_CAPTURE")
            hint = getattr(error, "hint", None)
            raise CaptureError(str(error), code=code, hint=hint) from error
        self.width = image.width
        self.height = image.height
        return Frame(
            width=image.width,
            height=image.height,
            channels=4,
            data=image.data,
            scale=1.0,
            backend=image.backend,
            blank=bool(image.blank),
            occluded=bool(image.occluded),
            frame_seq=0,
            target_hwnd=self.target_hwnd,
        )

    def close(self) -> None:
        return None


def create_source(name: str, *, target_hwnd: Optional[int] = None, **options) -> "FakeSource | MssSource | WindowSource":
    if target_hwnd is not None or name == "window":
        if target_hwnd is None:
            raise ValueError("window source requires target_hwnd")
        # Lazy import keeps non-Windows daemon startup free of user32 imports.
        return WindowSource(target_hwnd=int(target_hwnd), **options)
    if name == "fake":
        return FakeSource(**options)
    if name == "mss":
        return MssSource(**options)
    raise ValueError(f"unknown source {name!r} (expected fake|mss|window)")


def privacy_frame(mode: str = "warning_image", width: int = 640,
                  height: int = 360) -> Frame:
    """Synthetic privacy placeholder; never contains real screen pixels.

    warning_image: amber/black diagonal hazard pattern.
    black_image:   pure black image, intentionally supplied by policy.
    """
    width = max(64, min(1920, int(width or 640)))
    height = max(64, min(1080, int(height or 360)))
    data = bytearray(width * height * 4)
    black = str(mode) == "black_image"
    if not black:
        for y in range(height):
            for x in range(width):
                stripe = ((x + y) // 24) % 2 == 0
                border = x < 6 or y < 6 or x >= width - 6 or y >= height - 6
                if border or stripe:
                    b, g, r = 0x24, 0x9D, 0xF5  # amber in BGRA order below
                else:
                    b, g, r = 0x08, 0x0B, 0x12
                offset = (y * width + x) * 4
                data[offset] = b
                data[offset + 1] = g
                data[offset + 2] = r
                data[offset + 3] = 255
        # Correct the amber tuple to BGRA: B=0x24, G=0x9D, R=0xF5 is above.
    return Frame(
        width=width,
        height=height,
        channels=4,
        data=bytes(data),
        scale=1.0,
        backend="privacy_guard_black" if black else "privacy_guard_warning",
        blank=False,
        occluded=False,
    )


def available_sources() -> list[str]:
    sources = ["fake"]
    if os.name == "nt":
        sources.append("window")
    try:
        import mss  # noqa: F401
    except Exception:  # noqa: BLE001
        return sources
    sources.append("mss")
    return sources


def default_step(width: int, height: int, max_cells: int = 100) -> int:
    longest = max(width, height)
    return max(2, int(math.ceil(longest / max_cells)))


__all__ = [
    "CaptureError",
    "FakeSource",
    "Frame",
    "MssSource",
    "SourceUnavailable",
    "privacy_frame",
    "WindowSource",
    "available_sources",
    "create_source",
    "default_step",
]
