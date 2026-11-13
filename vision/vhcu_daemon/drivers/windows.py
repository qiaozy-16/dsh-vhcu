"""Windows backends: adapters over the existing SendInput/mss/soundcard code.

The low-level Win32 structs and calls stay in ``input.py`` on purpose: this P1
refactor is structural, and moving every ctypes declaration would change a
working Windows path. The driver boundary is the contract future platforms use.
"""

from __future__ import annotations

from typing import Any

from ..hear import HearService
from ..input import InputController
from ..interrupts import InterruptController
from ..sources import available_sources, create_source


class WindowsInputBackend:
    def __init__(self, *, interrupt: InterruptController | None = None,
                 dry_run: bool | None = None) -> None:
        self._controller = InputController(dry_run=dry_run, interrupt=interrupt)

    @property
    def dry_run(self) -> bool:
        return self._controller.dry_run

    def __getattr__(self, name: str) -> Any:
        # Preserve the complete InputController surface (status, actions, tests)
        # while keeping the driver identity explicit at the factory boundary.
        return getattr(self._controller, name)


class WindowsCaptureBackend:
    def available_sources(self) -> list[str]:
        return available_sources()

    def create_source(self, name: str, **options: Any):
        return create_source(name, **options)


class WindowsAudioBackend:
    def __init__(self, emit, log=None, interrupt=None) -> None:
        self._service = HearService(emit, log=log, interrupt=interrupt)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)

    def start(self) -> dict:
        return self._service.start()

    def stop(self) -> dict:
        return self._service.stop()

    def status(self) -> dict:
        return self._service.status()

    def configure(self, level: str | None = None, disable_filter: bool | None = None) -> dict:
        return self._service.configure(level, disable_filter)


WindowsInputDriver = WindowsInputBackend
WindowsCaptureDriver = WindowsCaptureBackend
WindowsAudioDriver = WindowsAudioBackend

__all__ = [
    "WindowsAudioBackend",
    "WindowsAudioDriver",
    "WindowsCaptureBackend",
    "WindowsCaptureDriver",
    "WindowsInputBackend",
    "WindowsInputDriver",
]
