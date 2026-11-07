"""Frame sources: a deterministic fake (CI/tests) and mss (real capture)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Frame:
    width: int
    height: int
    channels: int  # BGRA
    data: bytes
    scale: float = 1.0

    @property
    def nbytes(self) -> int:
        return len(self.data)


class SourceUnavailable(RuntimeError):
    def __init__(self, message: str, hint: str):
        super().__init__(message)
        self.code = "E_UNAVAILABLE"
        self.hint = hint


class CaptureError(RuntimeError):
    def __init__(self, message: str):
        super().__init__(message)
        self.code = "E_CAPTURE"


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
        return Frame(width=width, height=height, channels=4, data=bytes(buf), scale=1.0)


class MssSource:
    """Primary-monitor capture via mss (BitBlt). Optional dependency."""

    name = "mss"

    def __init__(self, monitor: int = 1):
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
        )

    def close(self) -> None:
        try:
            self._mss.close()
        except Exception:  # noqa: BLE001
            pass


def create_source(name: str, **options) -> "FakeSource | MssSource":
    if name == "fake":
        return FakeSource(**options)
    if name == "mss":
        return MssSource()
    raise ValueError(f"unknown source {name!r} (expected fake|mss)")


def available_sources() -> list[str]:
    sources = ["fake"]
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
    "available_sources",
    "create_source",
    "default_step",
]
