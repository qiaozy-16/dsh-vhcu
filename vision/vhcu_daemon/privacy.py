"""P14 privacy guard: block vision capture while a sensitive window is active."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os

DEFAULT_BLACKLIST = ["密码", "银行", "隐私", "Password", "Login", "私人"]
MAX_TERMS = 50
MAX_TERM_CHARS = 64

_IS_WINDOWS = os.name == "nt"


def foreground_identity() -> dict:
    """Read-only identity of the foreground window; never mutates the window."""
    if not _IS_WINDOWS:
        return {"hwnd": 0, "pid": 0, "title": ""}
    try:
        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return {"hwnd": 0, "pid": 0, "title": ""}
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        length = int(user32.GetWindowTextLengthW(hwnd))
        title = ""
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
        return {"hwnd": hwnd, "pid": int(pid.value), "title": title}
    except Exception:  # noqa: BLE001
        return {"hwnd": 0, "pid": 0, "title": ""}


def foreground_title() -> str:
    """Best-effort title of the current foreground window (empty if unknown)."""
    return foreground_identity()["title"]


def sanitize_blacklist(terms) -> list[str]:
    """Keep at most 50 non-empty unique terms of <=64 chars."""
    if not isinstance(terms, (list, tuple)):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in terms:
        value = str(raw).strip()
        if not value or len(value) > MAX_TERM_CHARS:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
        if len(cleaned) >= MAX_TERMS:
            break
    return cleaned


def is_sensitive_title(title: str, terms) -> bool:
    """Case-insensitive substring match of any configured blacklist term."""
    text = str(title or "").casefold()
    if not text:
        return False
    return any(str(term).strip().casefold() in text for term in (terms or []) if str(term).strip())
