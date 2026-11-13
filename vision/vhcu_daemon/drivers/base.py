"""Platform backend contracts (P1).

These Protocols are intentionally small: they describe the capabilities the
daemon consumes, not every helper a concrete driver may expose. The Windows
driver is the only implementation in this phase; the factory refuses unknown
platforms rather than pretending a POSIX backend exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class InputBackend(Protocol):
    dry_run: bool

    def status(self) -> dict: ...
    def assert_foreground(self, expected: dict | None) -> None: ...
    def click(self, x: int, y: int, scale_factor: float | None = None) -> dict: ...
    def type_text(self, text: str, ime_shortcut: str = "ctrl+space",
                  use_clipboard_fallback: bool = False) -> dict: ...
    def key_press(self, keys: list[str], delay_ms: int = 0) -> dict: ...
    def scroll(self, amount: int) -> dict: ...
    def drag(self, x1: int, y1: int, x2: int, y2: int) -> dict: ...
    def middle_drag(self, x1: int, y1: int, x2: int, y2: int) -> dict: ...
    def macro_open_notepad_and_type(self, text: str, ime_shortcut: str = "ctrl+space") -> dict: ...
    def macro_save_file(self) -> dict: ...
    def run_macro(self, steps: list[dict], variables: dict | None = None,
                  scale_factor: float | None = None) -> dict: ...


class CaptureBackend(Protocol):
    def available_sources(self) -> list[str]: ...
    def create_source(self, name: str, **options: Any) -> Any: ...


class AudioBackend(Protocol):
    def start(self) -> dict: ...
    def stop(self) -> dict: ...
    def status(self) -> dict: ...
    def configure(self, level: str | None = None, disable_filter: bool | None = None) -> dict: ...


@dataclass(frozen=True)
class Drivers:
    input: InputBackend
    capture: CaptureBackend
    audio: AudioBackend


class DriverUnavailable(RuntimeError):
    code = "E_UNAVAILABLE"

    def __init__(self, platform: str):
        super().__init__(f"no VHCU input/capture/audio driver for platform {platform!r}")
        self.platform = platform
        self.hint = (
            "当前版本只提供 Windows 驱动；macOS 驱动将在后续阶段实现。"
            "Windows 端行为保持不变。"
        )


__all__ = ["AudioBackend", "CaptureBackend", "DriverUnavailable", "Drivers", "InputBackend"]
