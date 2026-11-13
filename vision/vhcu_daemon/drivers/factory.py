"""Driver factory: the only place that decides which OS backend to instantiate."""

from __future__ import annotations

import sys

from .base import DriverUnavailable, Drivers
from .windows import WindowsAudioBackend, WindowsCaptureBackend, WindowsInputBackend


class DriverFactory:
    """Platform selector; the only OS branch in the driver layer."""

    @staticmethod
    def create(*, emit, log=None, interrupt=None, dry_run=None) -> Drivers:
        return create_drivers(emit=emit, log=log, interrupt=interrupt, dry_run=dry_run)


def create_drivers(*, emit, log=None, interrupt=None, dry_run=None) -> Drivers:
    platform = sys.platform
    if platform.startswith("win"):
        return Drivers(
            input=WindowsInputBackend(interrupt=interrupt, dry_run=dry_run),
            capture=WindowsCaptureBackend(),
            audio=WindowsAudioBackend(emit, log=log, interrupt=interrupt),
        )
    # No macOS/Linux implementation is added in P1. Refusing explicitly is
    # safer than importing SendInput modules that do not exist there.
    raise DriverUnavailable(platform)


__all__ = ["DriverFactory", "create_drivers"]
