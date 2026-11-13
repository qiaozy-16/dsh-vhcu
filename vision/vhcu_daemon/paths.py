"""Absolute resource paths shared by the daemon.

The Node host injects ``VHCU_MODEL_PATH``, ``VHCU_MACROS_PATH`` and
``VHCU_LOG_DIR`` as absolute paths. Python never joins a relative path from
the current working directory: a PyInstaller one-file build has no stable cwd,
and a macOS bundle has a different layout from the Windows repo.

Defaults are still absolute (derived from ``__file__``) so the test suite and a
plain ``python -m vhcu_daemon`` keep working without the host env contract.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class PathConfigError(RuntimeError):
    code = "E_CONFIG"
    hint = "把 VHCU_MODEL_PATH / VHCU_MACROS_PATH / VHCU_LOG_DIR 设置为绝对路径"

    def __init__(self, name: str, value: str):
        super().__init__(f"{name} must be an absolute path, got {value!r}")
        self.name = name
        self.value = value


def _absolute(value: str, name: str, fallback: Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return fallback.expanduser().resolve()
    candidate = Path(raw).expanduser()
    # Accept Windows drive paths even when this file is inspected on POSIX;
    # the daemon itself only runs on the target OS, but tests may cross-check.
    if not (candidate.is_absolute() or _DRIVE_PATH.match(raw)):
        raise PathConfigError(name, raw)
    return candidate.resolve()


def _env_path(name: str, fallback: Path) -> Path:
    return _absolute(os.environ.get(name, ""), name, fallback)


PACKAGE_ROOT = Path(__file__).resolve().parent
VISION_ROOT = PACKAGE_ROOT.parent
REPO_ROOT = VISION_ROOT.parent


def models_dir() -> Path:
    return _env_path("VHCU_MODEL_PATH", REPO_ROOT / "models")


def macros_path() -> Path:
    return _env_path("VHCU_MACROS_PATH", REPO_ROOT / "macros.json")


def log_dir() -> Path:
    return _env_path("VHCU_LOG_DIR", REPO_ROOT / "logs")


def resource_paths() -> dict[str, str]:
    """Wire-safe, absolute path facts for READY/sys.info diagnostics."""
    return {
        "models": str(models_dir()),
        "macros": str(macros_path()),
        "logs": str(log_dir()),
    }


__all__ = [
    "PACKAGE_ROOT",
    "PathConfigError",
    "REPO_ROOT",
    "VISION_ROOT",
    "log_dir",
    "macros_path",
    "models_dir",
    "resource_paths",
]
