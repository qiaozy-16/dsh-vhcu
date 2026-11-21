"""Real mouse/keyboard injection (Windows SendInput, stdlib only).

Why not pyautogui:
  - pyautogui pulls Pillow/pyscreeze and is slow per action;
  - SendInput is the native path and lets us declare DPI awareness explicitly.
The safety contract mirrors pyautogui.FAILSAFE:
  - before every action, if the real cursor sits in a screen corner, abort;
  - coordinate bounds are validated against the DPI-aware primary screen;
  - a local rate limiter refuses bursts even if the host guard is bypassed.
Set VHCU_INPUT_DRY_RUN=1 to validate without injecting anything (tests/dev).
"""

from __future__ import annotations

import ctypes
import math
import os
import re
import threading
import time
from ctypes import wintypes

from .cursor_overlay import CursorOverlay
from .human_input import HumanizeError, build_trajectory, normalize_profile, virtual_duration_ms
from .virtual_cursor import (
    VirtualCursorFailure,
    VirtualCursorUnsupported,
    post_message_click,
    post_message_drag,
    post_message_path,
    post_message_scroll,
    resolve_window,
)
from .interrupts import InterruptController, InterruptError
from .privacy import foreground_identity
from .shapes import ShapeError, build_shape
from .safety import InvalidScreen, clamp_point, in_screen_corner, normalize_point

IS_WINDOWS = os.name == "nt"
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

# SendInput constants
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_EXTENDEDKEY = 0x0001

# Win keys and the navigation cluster are extended keys: without
# KEYEVENTF_EXTENDEDKEY some apps/RDP/UIPI paths drop the combo entirely
# (whitepaper 8.9); arrow keys especially become numpad keys in some hosts.
VK_LWIN = 0x5B
VK_RWIN = 0x5C
EXTENDED_VKS = frozenset((
    VK_LWIN, VK_RWIN,
    0x25, 0x26, 0x27, 0x28,  # arrows
    0x24, 0x23, 0x21, 0x22,  # home, end, pageup, pagedown
    0x2D, 0x2E,              # insert, delete
))


def extended_key_flag(vk: int) -> int:
    """Return KEYEVENTF_EXTENDEDKEY for Win keys, 0 for every other key."""
    try:
        value = int(vk)
    except (TypeError, ValueError):
        return 0
    return KEYEVENTF_EXTENDEDKEY if value in EXTENDED_VKS else 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
ABSOLUTE_MOVE = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120

CORNER_PX = 8
MAX_TEXT_CHARS = 2000
MAX_KEYS = 4
RATE_MAX = 12
RATE_WINDOW_S = 5.0
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# P1 隐患 1: modifiers VHCU may leave down after a crash. Both the generic and
# the left/right VKs are released: injection uses the generic ones (VK dict),
# while Windows tracks the sided state separately.
RELEASE_VKS = (
    0x5B, 0x5C,        # LWIN / RWIN
    0x10, 0x11, 0x12,  # SHIFT / CONTROL / MENU (generic)
    0xA0, 0xA1,        # LSHIFT / RSHIFT
    0xA2, 0xA3,        # LCONTROL / RCONTROL
    0xA4, 0xA5,        # LALT / RMENU
)
RELEASE_MOUSE_FLAGS = (
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_MIDDLEUP,
    MOUSEEVENTF_RIGHTUP,
)

VK = {
    "ctrl": 0x11, "control": 0x11,
    "shift": 0x10, "alt": 0x12,
    "win": 0x5B, "meta": 0x5B, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
}
MODIFIERS = {"ctrl", "control", "shift", "alt", "win", "meta", "cmd"}


def _build_vk_table() -> dict:
    table = dict(VK)
    for ch in "abcdefghijklmnopqrstuvwxyz":
        table[ch] = 0x41 + (ord(ch) - ord("a"))
    for digit in "0123456789":
        table[digit] = 0x30 + int(digit)
    for index in range(1, 13):
        table[f"f{index}"] = 0x6F + index
    return table


VK_TABLE = _build_vk_table()


class InputError(RuntimeError):
    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


# ── Win32 clipboard helpers (P24.2 optional paste fallback) ──────────────


def _clipboard_open(user32) -> None:
    for _ in range(6):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.02)
    raise InputError("E_CLIPBOARD", "无法打开 Windows 剪贴板", "关闭占用剪贴板的程序后重试")


def _clipboard_read_text() -> str | None:
    if not IS_WINDOWS:
        return None
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    _clipboard_open(user32)
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(int(pointer))
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _clipboard_write_text(value: str) -> None:
    if not IS_WINDOWS:
        raise InputError("E_UNAVAILABLE", "clipboard fallback requires Windows")
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalFree.argtypes = [wintypes.HANDLE]

    data = (str(value) + "\0").encode("utf-16-le")
    _clipboard_open(user32)
    handle = None
    transferred = False
    try:
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise InputError("E_CLIPBOARD", "GlobalAlloc 失败")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise InputError("E_CLIPBOARD", "GlobalLock 失败")
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.EmptyClipboard():
            raise InputError("E_CLIPBOARD", "EmptyClipboard 失败")
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            raise InputError("E_CLIPBOARD", "SetClipboardData 失败")
        transferred = True
    finally:
        user32.CloseClipboard()
        if handle and not transferred:
            kernel32.GlobalFree(handle)


# ── ctypes plumbing ──────────────────────────────────────────────────────


if IS_WINDOWS:
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]
else:  # pragma: no cover - the plugin targets Windows; keep import-safe elsewhere
    INPUT = object  # type: ignore[assignment]


def enable_dpi_awareness() -> str:
    """Make this process DPI-aware so SendInput/screen metrics use physical px."""
    if not IS_WINDOWS:
        return "non-windows"
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:  # noqa: BLE001
        return "none"


def primary_size() -> tuple[int, int]:
    if not IS_WINDOWS:
        return (0, 0)
    return (int(ctypes.windll.user32.GetSystemMetrics(0)), int(ctypes.windll.user32.GetSystemMetrics(1)))


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """Return the DPI-aware virtual desktop rectangle (x, y, w, h).

    Windows lets monitors sit left/above the primary monitor, so x/y may be
    negative. SendInput needs this full rectangle plus MOUSEEVENTF_VIRTUALDESK.
    """
    if not IS_WINDOWS:
        return (0, 0, 1, 1)
    user32 = ctypes.windll.user32
    x = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    y = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    width = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    height = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    if width <= 1 or height <= 1:
        return (0, 0, 1, 1)
    return (x, y, width, height)


def cursor_position() -> tuple[int, int]:
    if not IS_WINDOWS:
        return (0, 0)
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return (int(point.x), int(point.y))


def in_corner(x: int, y: int, width: int, height: int, margin: int = CORNER_PX) -> bool:
    """pyautogui.FAILSAFE equivalent; implementation lives in safety.py."""
    return in_screen_corner(x, y, width, height, margin)


def clamp_click(x: int, y: int, width: int, height: int,
                origin_x: int = 0, origin_y: int = 0) -> tuple[int, int, bool]:
    """Clamp a screen coordinate into the virtual desktop rectangle."""
    try:
        cx, cy, clamped = clamp_point(
            int(x) - int(origin_x), int(y) - int(origin_y), width, height,
        )
    except InvalidScreen as error:
        raise InputError("E_UNAVAILABLE", "virtual screen size is unavailable") from error
    return cx + int(origin_x), cy + int(origin_y), clamped


def normalize_click(x: int, y: int, width: int, height: int,
                    origin_x: int = 0, origin_y: int = 0) -> tuple[int, int]:
    try:
        cx, cy = normalize_point(
            int(x) - int(origin_x), int(y) - int(origin_y), width, height,
        )
    except InvalidScreen as error:
        raise InputError("E_UNAVAILABLE", "virtual screen size is unavailable") from error
    # SendInput absolute coordinates are normalized over the virtual desktop.
    return (int(round(cx * 65535 / (width - 1))), int(round(cy * 65535 / (height - 1))))


def parse_hotkey(keys: list[str]) -> list[int]:
    if not isinstance(keys, list) or not keys:
        raise InputError("E_PROTO", "keys must be a non-empty array", '例如 ["ctrl","s"]')
    if len(keys) > MAX_KEYS:
        raise InputError("E_PROTO", f"at most {MAX_KEYS} keys are supported")
    normalized = [str(key).strip().lower() for key in keys]
    resolved = []
    for key in normalized:
        if key not in VK_TABLE:
            raise InputError("E_PROTO", f"unsupported key {key!r}", "支持 ctrl/shift/alt/win + 字母/数字/方向/功能键")
        resolved.append(VK_TABLE[key])
    if normalized[-1] in MODIFIERS:
        raise InputError("E_PROTO", "the last key must be a real key, not a modifier", '例如 ["ctrl","s"]')
    return resolved


def validate_text(text: str) -> str:
    value = str(text)
    if not value:
        raise InputError("E_PROTO", "text must not be empty")
    if len(value) > MAX_TEXT_CHARS:
        raise InputError("E_PROTO", f"text is longer than {MAX_TEXT_CHARS} chars", "拆分多次调用")
    return value


def ime_open_status() -> bool | None:
    """Best-effort: is the foreground window's IME open/composing?

    Returns None when the query is unavailable (non-Windows or no imm32).
    Read-only: it never changes IME state by itself.
    """
    if not IS_WINDOWS:
        return None
    try:
        user32 = ctypes.windll.user32
        imm32 = ctypes.windll.imm32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        imm32.ImmGetContext.restype = ctypes.c_void_p
        imm32.ImmGetContext.argtypes = [ctypes.c_void_p]
        himc = imm32.ImmGetContext(hwnd)
        if not himc:
            return False
        try:
            imm32.ImmGetOpenStatus.argtypes = [ctypes.c_void_p]
            imm32.ImmGetOpenStatus.restype = wintypes.BOOL
            return bool(imm32.ImmGetOpenStatus(himc))
        finally:
            imm32.ImmReleaseContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            imm32.ImmReleaseContext(hwnd, himc)
    except Exception:  # noqa: BLE001 - IME APIs vary; never fail a type action
        return None


class InputController:
    def __init__(self, dry_run: bool | None = None, interrupt: InterruptController | None = None,
                 overlay: CursorOverlay | None = None):
        self.dry_run = (os.environ.get("VHCU_INPUT_DRY_RUN") == "1") if dry_run is None else dry_run
        self.dpi_mode = enable_dpi_awareness()
        self.origin_x, self.origin_y, self.width, self.height = virtual_screen_rect()
        self._lock = threading.RLock()
        self._sent: list[float] = []
        self.interrupt = interrupt or InterruptController()
        self.overlay = overlay or CursorOverlay()
        self._virtual_cursor: tuple[int, int] | None = None
        # Set by the daemon; returns True when injection is privacy-blocked.
        self._privacy_check = None

    def set_privacy_check(self, checker) -> None:
        self._privacy_check = checker

    def assert_privacy(self, hwnd: int = 0, allow_unprotected: bool = False) -> None:
        """Fail closed when the daemon's privacy checker cannot clear injection.

        ``allow_unprotected`` is only sent by the Node host when the user has
        explicitly enabled the high-risk privacy-off switch. It does not change
        the default; it makes that existing choice take effect consistently in
        the Python input path too.
        """
        if allow_unprotected:
            return
        checker = self._privacy_check
        if checker is None:
            return
        try:
            blocked = bool(checker(int(hwnd or 0)))
        except Exception as error:  # noqa: BLE001 - a broken check means deny
            raise InputError(
                "E_PRIVACY_BLOCKED",
                f"隐私检查失败，已阻断输入：{error}",
                "请先切离敏感窗口并重启视觉守护",
            ) from error
        if blocked:
            raise InputError(
                "E_PRIVACY_BLOCKED",
                "隐私保护已触发：已禁止键鼠注入",
                "请原地等待用户切走敏感窗口，再重新截图确认",
            )

    def status(self) -> dict:
        return {
            "dryRun": self.dry_run,
            "dpiMode": self.dpi_mode,
            "screen": {
                "originX": self.origin_x,
                "originY": self.origin_y,
                "width": self.width,
                "height": self.height,
                "virtual": True,
            },
            "interrupt": self.interrupt.status(),
        }

    def release_all(self) -> dict:
        """Force-release stuck modifiers and mouse buttons (best effort).

        P1 隐患 1: Windows keeps injected key/button state after the injecting
        process dies, so startup and shutdown must clear it unconditionally.
        Idempotent and safe to call even when nothing is pressed.
        """
        if not IS_WINDOWS:
            return {"released": False, "reason": "windows-only"}
        if self.dry_run:
            return {
                "released": True,
                "dryRun": True,
                "keys": len(RELEASE_VKS),
                "buttons": len(RELEASE_MOUSE_FLAGS),
            }
        events = [
            self._key_input(vk, 0, KEYEVENTF_KEYUP | extended_key_flag(vk))
            for vk in RELEASE_VKS
        ]
        events.extend(self._mouse_input(0, 0, flag) for flag in RELEASE_MOUSE_FLAGS)
        try:
            self._send(*events)
        except BaseException as error:  # noqa: BLE001 - release must never raise
            return {
                "released": False,
                "error": str(error),
                "keys": len(RELEASE_VKS),
                "buttons": len(RELEASE_MOUSE_FLAGS),
            }
        return {
            "released": True,
            "keys": len(RELEASE_VKS),
            "buttons": len(RELEASE_MOUSE_FLAGS),
        }

    def assert_foreground(self, expected: dict | None) -> None:
        """P22.2: physically block injection when the foreground window changed."""
        if self.dry_run or not isinstance(expected, dict):
            return
        expected_hwnd = int(expected.get("hwnd") or 0)
        if not expected_hwnd:
            return
        current = foreground_identity()
        current_hwnd = int(current.get("hwnd") or 0)
        if current_hwnd and current_hwnd != expected_hwnd:
            raise InputError(
                "E_FOCUS_LOST",
                "前台窗口焦点已被用户改变，本次操作已拦截",
                "⚠️ 检测到前台窗口焦点已被用户改变，本次操作已被拦截。"
                "请重新调用 vhcu_get_full_frame 确认当前屏幕状态后再重试。",
            )

    # ── humanized movement ───────────────────────────────────────────

    def _resolve_profile(self, value) -> str:
        try:
            return normalize_profile(value, default="off")
        except HumanizeError as error:
            raise InputError("E_PROTO", str(error), "humanizeProfile 只能是 off/precise/balanced") from error

    @staticmethod
    def _resolve_injection_mode(value) -> str:
        mode = str(value or "physical").strip().lower()
        if mode not in ("physical", "virtual", "hybrid"):
            raise InputError(
                "E_PROTO",
                "cursorInjectionMode must be physical/virtual/hybrid",
                "cursorInjectionMode 只能是 physical/virtual/hybrid",
            )
        return mode

    @staticmethod
    def _resolve_scale(scale_factor, scale_x, scale_y) -> tuple[float, float]:
        """Return independent image->physical scales with defensive bounds."""
        def number(value, name):
            try:
                parsed = float(value)
            except (TypeError, ValueError) as error:
                raise InputError("E_PROTO", f"{name} must be a number") from error
            if not (0.1 <= parsed <= 10.0):
                raise InputError("E_PROTO", f"{name} must be within 0.1..10")
            return parsed

        base = None
        if scale_factor is not None:
            base = number(scale_factor, "scaleFactor")
        sx = number(scale_x, "scaleX") if scale_x is not None else (base if base is not None else 1.0)
        sy = number(scale_y, "scaleY") if scale_y is not None else (base if base is not None else 1.0)
        return sx, sy

    @staticmethod
    def _merge_humanize(segments: list[dict]) -> dict | None:
        if not segments:
            return None
        if len(segments) == 1:
            return dict(segments[0])
        first = segments[0]
        last = segments[-1]
        return {
            "profile": first.get("profile"),
            "start": first.get("start"),
            "endpoint": last.get("endpoint"),
            "duration_ms": sum(int(item.get("duration_ms") or 0) for item in segments),
            "steps": sum(int(item.get("steps") or 0) for item in segments),
            "distance_px": round(sum(float(item.get("distance_px") or 0.0) for item in segments), 1),
            "jitter_px": max(float(item.get("jitter_px") or 0.0) for item in segments),
            "segments": len(segments),
        }

    def _move_humanized(self, x: int, y: int, profile: str) -> dict | None:
        """Move to a physical coordinate using the selected profile.

        profile='off' sends the legacy single absolute MOVE. humanized profiles
        generate a pure trajectory and inject one MOVE per point, with
        interruptible sleeps between points.
        """
        if profile == "off":
            nx, ny = normalize_click(x, y, self.width, self.height, self.origin_x, self.origin_y)
            if not self.dry_run:
                self._send(self._mouse_input(nx, ny, ABSOLUTE_MOVE))
            return None
        start = cursor_position()
        local_start = (start[0] - self.origin_x, start[1] - self.origin_y)
        local_end = (int(x) - self.origin_x, int(y) - self.origin_y)
        try:
            trajectory = build_trajectory(
                local_start,
                local_end,
                profile,
                screen=(self.width, self.height),
            )
        except HumanizeError as error:
            raise InputError("E_PROTO", str(error)) from error
        if trajectory is None:
            nx, ny = normalize_click(x, y, self.width, self.height, self.origin_x, self.origin_y)
            if not self.dry_run:
                self._send(self._mouse_input(nx, ny, ABSOLUTE_MOVE))
            return None
        # Deadline scheduling: the trajectory's at_ms is already evenly
        # distributed over the total duration. Sleep to the absolute deadline
        # so per-point VCS/ctypes overhead cannot accumulate into 2x the budget.
        started = time.monotonic()
        for point in trajectory.points:
            self.interrupt.check()
            nx, ny = normalize_click(
                point.x + self.origin_x, point.y + self.origin_y,
                self.width, self.height, self.origin_x, self.origin_y,
            )
            if not self.dry_run:
                self._send(self._mouse_input(nx, ny, ABSOLUTE_MOVE))
                remaining = started + point.at_ms / 1000.0 - time.monotonic()
                if remaining > 0:
                    self._sleep(remaining)
        # Guarantee the exact requested endpoint after integer normalization.
        nx, ny = normalize_click(x, y, self.width, self.height, self.origin_x, self.origin_y)
        if not self.dry_run:
            self._send(self._mouse_input(nx, ny, ABSOLUTE_MOVE))
        return trajectory.metadata()

    def _click_button_humanized(self, x: int, y: int, button: str,
                                profile: str, count: int = 1) -> dict | None:
        meta = self._move_humanized(x, y, profile)
        nx, ny = normalize_click(x, y, self.width, self.height, self.origin_x, self.origin_y)
        down_flag, up_flag = {
            "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        }[button]
        if not self.dry_run:
            for index in range(max(1, int(count))):
                self._send(
                    self._mouse_input(nx, ny, down_flag),
                    self._mouse_input(nx, ny, up_flag),
                )
                if index + 1 < count:
                    self._sleep(0.05)
        return meta

    def _click_humanized(self, x: int, y: int, profile: str) -> dict | None:
        return self._click_button_humanized(x, y, "left", profile, 1)

    VIRTUAL_FALLBACK_WARNING = "目标软件不支持后台注入，已自动降级为物理光标"

    def _overlay_show(self, x: int, y: int, action: str) -> None:
        try:
            self.overlay.show(x, y, action)
        except BaseException:
            # Overlay is decoration; coordinate injection must continue.
            pass

    def _virtual_animate(self, x: int, y: int, humanize_profile: str,
                         action: str = "click") -> dict | None:
        """Animate the OS overlay without moving the physical cursor."""
        if humanize_profile == "off":
            self._overlay_show(x, y, action)
            self._virtual_cursor = (x, y)
            return None
        start = self._virtual_cursor or cursor_position()
        distance = math.hypot(int(x) - int(start[0]), int(y) - int(start[1]))
        trajectory = build_trajectory(
            start, (int(x), int(y)), humanize_profile,
            screen=(self.width, self.height),
            duration_override_ms=virtual_duration_ms(distance),
        )
        if trajectory is None:
            self._overlay_show(x, y, action)
            self._virtual_cursor = (x, y)
            return None
        started = time.monotonic()
        for point in trajectory.points:
            self.interrupt.check()
            self._overlay_show(point.x, point.y, "move")
            if not self.dry_run:
                remaining = started + point.at_ms / 1000.0 - time.monotonic()
                if remaining > 0:
                    self._sleep(remaining)
        self._virtual_cursor = (x, y)
        self._overlay_show(x, y, action)
        return trajectory.metadata()

    def _animate_safe(self, x: int, y: int, profile: str, action: str):
        try:
            return self._virtual_animate(x, y, profile, action)
        except InterruptError:
            raise
        except BaseException:
            # Decoration is optional; an overlay/trajectory failure must never
            # abort the command or kill the daemon.
            return None

    def _virtual_click(self, x: int, y: int, humanize_profile: str,
                       target_hwnd: int | None = None,
                       action: str = "click") -> dict:
        target = resolve_window(target_hwnd, x, y)
        humanize = self._animate_safe(x, y, humanize_profile, "click" if action == "click" else "click")
        posted = post_message_click(target, action, x, y)
        posted["humanize"] = humanize
        posted["cursor"] = {
            "mode": "post_message",
            "used": "post_message",
            "fallback": False,
            "target_hwnd": int(target),
        }
        return posted

    # ── public actions ───────────────────────────────────────────────

    @staticmethod
    def _resolve_click_type(value) -> str:
        action = str(value or "click").strip().lower()
        if action not in ("click", "double_click", "right_click", "middle_click"):
            raise InputError(
                "E_PROTO",
                "click_type must be click/double_click/right_click/middle_click",
            )
        return action

    def _perform_click(self, nx: int, ny: int, action: str) -> None:
        """Send the physical button sequence for one logical click action."""
        mapping = {
            "click": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 1),
            "double_click": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP, 2),
            "right_click": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, 1),
            "middle_click": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP, 1),
        }
        down, up, count = mapping[action]
        for index in range(count):
            self._send(self._mouse_input(nx, ny, down), self._mouse_input(nx, ny, up))
            if index + 1 < count:
                self._sleep(0.05)

    def click(self, x: int, y: int, scale_factor: float | None = None,
              humanize_profile: str | None = None,
              cursor_injection_mode: str | None = None,
              target_hwnd: int | None = None,
              scale_x: float | None = None,
              scale_y: float | None = None,
              click_type: str | None = None,
              allow_unprotected: bool = False) -> dict:
        self.assert_privacy(target_hwnd or 0, allow_unprotected)
        try:
            raw_x = int(x)
            raw_y = int(y)
        except (TypeError, ValueError) as error:
            raise InputError("E_PROTO", "click x/y must be integers") from error
        scale_x, scale_y = self._resolve_scale(scale_factor, scale_x, scale_y)
        real_x = int(round(raw_x * scale_x))
        real_y = int(round(raw_y * scale_y))
        real_x, real_y, clamped = clamp_click(real_x, real_y, self.width, self.height, self.origin_x, self.origin_y)
        warning = "坐标越界，已修正" if clamped else None
        profile = self._resolve_profile(humanize_profile)
        mode = self._resolve_injection_mode(cursor_injection_mode)
        action = self._resolve_click_type(click_type)
        with self._lock:
            self._rate_limit()
            self._failsafe()
            nx, ny = normalize_click(real_x, real_y, self.width, self.height, self.origin_x, self.origin_y)
            fallback_warning = None
            if mode == "virtual" and self.dry_run:
                return {
                    "dryRun": True,
                    "action": action,
                    "x": real_x,
                    "y": real_y,
                    "requested": [raw_x, raw_y],
                    "scaleFactor": scale_x if abs(scale_x - scale_y) < 1e-9 else None,
                    "scaleX": scale_x,
                    "scaleY": scale_y,
                    "applied": [real_x, real_y],
                    "normalized": [nx, ny],
                    "humanize": None,
                    "cursor": {
                        "mode": mode,
                        "used": "post_message",
                        "fallback": False,
                        "simulated": True,
                    },
                    **({"warning": warning} if warning else {}),
                }
            if mode != "physical" and not self.dry_run:
                try:
                    posted = self._virtual_click(real_x, real_y, profile, target_hwnd, action)
                    return {
                        "dryRun": False,
                        "action": action,
                        "x": real_x,
                        "y": real_y,
                        "requested": [raw_x, raw_y],
                        "scaleFactor": scale_x if abs(scale_x - scale_y) < 1e-9 else None,
                    "scaleX": scale_x,
                    "scaleY": scale_y,
                        "applied": [real_x, real_y],
                        "humanize": posted.get("humanize"),
                        "cursor": posted.get("cursor"),
                        **({"warning": warning} if warning else {}),
                    }
                except (VirtualCursorUnsupported, VirtualCursorFailure) as error:
                    if mode == "virtual" or int(getattr(error, "sent", 0)) > 0:
                        raise InputError(error.code, str(error), error.hint) from error
                    fallback_warning = self.VIRTUAL_FALLBACK_WARNING
            humanize = None
            if profile != "off":
                humanize = self._move_humanized(real_x, real_y, profile)
                if self.dry_run:
                    return {
                        "dryRun": True,
                        "action": action,
                        "x": real_x,
                        "y": real_y,
                        "requested": [raw_x, raw_y],
                        "scaleFactor": scale_x if abs(scale_x - scale_y) < 1e-9 else None,
                    "scaleX": scale_x,
                    "scaleY": scale_y,
                        "applied": [real_x, real_y],
                        "normalized": [nx, ny],
                        "humanize": humanize,
                        **({"warning": warning} if warning else {}),
                    }
                self._perform_click(nx, ny, action)
                return {
                    "dryRun": False,
                    "action": action,
                    "x": real_x,
                    "y": real_y,
                    "requested": [raw_x, raw_y],
                    "scaleFactor": scale_x if abs(scale_x - scale_y) < 1e-9 else None,
                    "scaleX": scale_x,
                    "scaleY": scale_y,
                    "applied": [real_x, real_y],
                    "humanize": humanize,
                    "cursor": {
                        "mode": mode if fallback_warning else "physical",
                        "used": "sendinput",
                        "fallback": bool(fallback_warning),
                        **({"warning": fallback_warning} if fallback_warning else {}),
                    },
                    **({"warning": warning} if warning else {}),
                }
            if self.dry_run:
                return {
                    "dryRun": True,
                    "action": action,
                    "x": real_x,
                    "y": real_y,
                    "requested": [raw_x, raw_y],
                    "scaleFactor": scale_x if abs(scale_x - scale_y) < 1e-9 else None,
                    "scaleX": scale_x,
                    "scaleY": scale_y,
                    "applied": [real_x, real_y],
                    "normalized": [nx, ny],
                    "humanize": None,
                    **({"warning": warning} if warning else {}),
                }
            self._send(self._mouse_input(nx, ny, ABSOLUTE_MOVE))
            self._perform_click(nx, ny, action)
            return {
                "dryRun": False,
                "action": action,
                "x": real_x,
                "y": real_y,
                "requested": [raw_x, raw_y],
                "scaleFactor": scale_x if abs(scale_x - scale_y) < 1e-9 else None,
                    "scaleX": scale_x,
                    "scaleY": scale_y,
                "applied": [real_x, real_y],
                "humanize": None,
                "cursor": {
                    "mode": mode if fallback_warning else "physical",
                    "used": "sendinput",
                    "fallback": bool(fallback_warning),
                    **({"warning": fallback_warning} if fallback_warning else {}),
                },
                **({"warning": warning} if warning else {}),
            }

    def type_text(
        self,
        text: str,
        ime_shortcut: str = "ctrl+space",
        use_clipboard_fallback: bool = False,
    ) -> dict:
        value = validate_text(text)
        clipboard_fallback = bool(use_clipboard_fallback)
        with self._lock:
            self._rate_limit()
            self._failsafe()
            if self.dry_run:
                return {
                    "dryRun": True,
                    "action": "type",
                    "chars": len(value),
                    "imeOpen": None,
                    "imeToggled": False,
                    "clipboardFallback": clipboard_fallback,
                }
            if clipboard_fallback:
                self._paste_clipboard(value)
                return {
                    "dryRun": False,
                    "action": "type",
                    "chars": len(value),
                    "imeOpen": None,
                    "imeAfter": None,
                    "imeToggled": False,
                    "clipboardFallback": True,
                }
            # IME guard: with a Chinese IME open, per-character Unicode injection
            # can be re-ordered into candidates. Ctrl+Space (the Windows default
            # toggle; Shift is only one IME's shortcut) switches to English.
            # If the first toggle does not take effect we retry once.
            toggled, ime_open, ime_after = self._ensure_ascii_input(ime_shortcut)
            self._type_unicode(value)
            return {
                "dryRun": False,
                "action": "type",
                "chars": len(value),
                "imeOpen": ime_open,
                "imeAfter": ime_after,
                "imeToggled": toggled,
                "clipboardFallback": False,
            }

    def _paste_clipboard(self, value: str) -> None:
        """Temporarily own the clipboard, paste physically, then restore it."""
        original = _clipboard_read_text()
        try:
            _clipboard_write_text(value)
            time.sleep(0.03)
            self._send_combo([VK_TABLE["ctrl"], VK_TABLE["v"]])
            time.sleep(0.12)
        finally:
            try:
                _clipboard_write_text(original if original is not None else "")
            except Exception:  # noqa: BLE001 - restore best effort, never mask paste
                pass

    def key_press(self, keys: list[str], delay_ms: int = 0) -> dict:
        vks = parse_hotkey(keys)
        try:
            delay = int(delay_ms or 0)
        except (TypeError, ValueError) as error:
            raise InputError("E_PROTO", "delay_ms must be an integer") from error
        if delay < 0 or delay > 1000:
            raise InputError("E_PROTO", "delay_ms must be within 0..1000")
        with self._lock:
            self._rate_limit()
            self._failsafe()
            if self.dry_run:
                return {
                    "dryRun": True,
                    "action": "hotkey",
                    "keys": [str(k) for k in keys],
                    "delayMs": delay,
                    "scancode": True,
                }
            self._send_combo(vks, delay_ms=delay)
            return {
                "dryRun": False,
                "action": "hotkey",
                "keys": [str(k) for k in keys],
                "delayMs": delay,
                "scancode": True,
            }

    # ── high-level macros ─────────────────────────────────────────────

    def macro_open_notepad_and_type(self, text: str, ime_shortcut: str = "ctrl+space") -> dict:
        """Win+R → notepad → Enter → wait 1s → type text (one round trip)."""
        value = validate_text(text)
        started = time.perf_counter()
        steps = []
        with self._lock:
            self._rate_limit()
            self._failsafe()
            if self.dry_run:
                return {
                    "dryRun": True,
                    "action": "macro.open_notepad_and_type",
                    "chars": len(value),
                    "steps": ["win+r", "type:notepad", "enter", "wait:1000", "type:text"],
                    "elapsedMs": 0.0,
                }
            try:
                self._send_combo([VK_TABLE["win"], VK_TABLE["r"]])
                steps.append("win+r")
                time.sleep(0.35)
                self._ensure_ascii_input(ime_shortcut)
                self._type_unicode("notepad")
                steps.append("type:notepad")
                self._tap_vk(VK_TABLE["enter"])
                steps.append("enter")
                time.sleep(1.0)
                self._ensure_ascii_input(ime_shortcut)
                self._type_unicode(value)
                steps.append("type:text")
            except Exception as error:  # noqa: BLE001 - every failure is a macro failure
                hint = getattr(error, "hint", "") or "检查前台窗口是否被拦截；重试前先回读确认"
                raise InputError("E_MACRO_FAILED", f"macro open_notepad failed after {steps}: {error}", hint) from error
            return {
                "dryRun": False,
                "action": "macro.open_notepad_and_type",
                "chars": len(value),
                "steps": steps,
                "elapsedMs": round((time.perf_counter() - started) * 1000.0, 1),
            }

    def macro_save_file(self) -> dict:
        """Send Ctrl+S as one audited macro."""
        started = time.perf_counter()
        with self._lock:
            self._rate_limit()
            self._failsafe()
            if self.dry_run:
                return {"dryRun": True, "action": "macro.save_file", "steps": ["ctrl+s"], "elapsedMs": 0.0}
            try:
                self._send_combo([VK_TABLE["ctrl"], VK_TABLE["s"]])
            except Exception as error:  # noqa: BLE001
                raise InputError(
                    "E_MACRO_FAILED",
                    f"macro save_file failed: {error}",
                    "确认前台窗口可接收快捷键，并已获得输入焦点",
                ) from error
            return {
                "dryRun": False,
                "action": "macro.save_file",
                "steps": ["ctrl+s"],
                "elapsedMs": round((time.perf_counter() - started) * 1000.0, 1),
            }

    # ── internal input primitives ─────────────────────────────────────

    def _send_combo(self, vks: list[int], delay_ms: int = 0) -> None:
        """Press modifiers + main key with scancodes; optional modifier delay."""
        if not vks:
            return
        modifiers = vks[:-1]
        main = vks[-1]
        mod_down = [self._key_scan_input(vk, extended_key_flag(vk)) for vk in modifiers]
        main_events = [
            self._key_scan_input(main, extended_key_flag(main)),
            self._key_scan_input(main, KEYEVENTF_KEYUP | extended_key_flag(main)),
        ]
        mod_up = [
            self._key_scan_input(vk, KEYEVENTF_KEYUP | extended_key_flag(vk))
            for vk in reversed(modifiers)
        ]
        if delay_ms > 0 and mod_down:
            self._send(*mod_down)
            time.sleep(delay_ms / 1000.0)
            self._send(*main_events, *mod_up)
            return
        self._send(*mod_down, *main_events, *mod_up)

    def _type_unicode(self, value: str) -> None:
        for index, char in enumerate(value):
            if char == "\n":
                self._tap_vk(VK_TABLE["enter"])
            elif char == "\t":
                self._tap_vk(VK_TABLE["tab"])
            else:
                # UTF-16 code units: emoji/surrogates need two events each.
                encoded = char.encode("utf-16-le")
                for offset in range(0, len(encoded), 2):
                    unit = int.from_bytes(encoded[offset:offset + 2], "little")
                    self._send(
                        self._key_input(0, unit, KEYEVENTF_UNICODE),
                        self._key_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
                    )
            if index % 16 == 0:
                time.sleep(0.002)

    def _ensure_ascii_input(self, shortcut: str = "ctrl+space") -> tuple[bool, bool | None, bool | None]:
        """Close the IME using the configured shortcut; retry once if needed.

        shortcut: "ctrl+space" (Windows default) | "shift" | "none".
        Returns (attached_toggle, ime_before, ime_after). If detection is
        unavailable the tuple is (False, None, None) and typing proceeds.
        """
        before = ime_open_status()
        if not before:
            return False, before, before
        if shortcut == "none":
            return False, before, before
        if shortcut == "shift":
            self._tap_vk(VK_TABLE["shift"])
            time.sleep(0.12)
            return True, before, ime_open_status()
        self._send_combo([VK_TABLE["ctrl"], VK_TABLE["space"]])
        time.sleep(0.12)
        after = ime_open_status()
        if after:
            self._send_combo([VK_TABLE["ctrl"], VK_TABLE["space"]])
            time.sleep(0.12)
            after = ime_open_status()
        return True, before, after

    def scroll(self, amount: int, cursor_injection_mode: str | None = None,
               target_hwnd: int | None = None,
               allow_unprotected: bool = False) -> dict:
        """Wheel scroll. Positive = up, negative = down; one unit = one notch."""
        self.assert_privacy(target_hwnd or 0, allow_unprotected)
        try:
            notches = int(amount)
        except (TypeError, ValueError) as error:
            raise InputError("E_PROTO", "scroll amount must be an integer") from error
        if notches == 0:
            raise InputError("E_PROTO", "scroll amount must not be zero")
        if abs(notches) > 20:
            raise InputError("E_PROTO", "scroll amount is limited to 20 notches per call")
        mode = self._resolve_injection_mode(cursor_injection_mode)
        with self._lock:
            self._rate_limit()
            self._failsafe()
            if self.dry_run:
                return {"dryRun": True, "action": "scroll", "amount": notches}
            if mode != "physical":
                x, y = cursor_position()
                try:
                    target = resolve_window(target_hwnd, x, y)
                    posted = post_message_scroll(target, x, y, notches)
                    return {
                        "dryRun": False,
                        "action": "scroll",
                        "amount": notches,
                        "cursor": {
                            "mode": "post_message",
                            "used": "post_message",
                            "fallback": False,
                            "target_hwnd": int(target),
                        },
                    }
                except (VirtualCursorUnsupported, VirtualCursorFailure) as error:
                    if mode == "virtual" or int(getattr(error, "sent", 0)) > 0:
                        raise InputError(error.code, str(error), error.hint) from error
                    # hybrid: physical fallback is safe while no message queued.
            self._send(
                self._mouse_input(0, 0, MOUSEEVENTF_WHEEL, mouse_data=notches * WHEEL_DELTA)
            )
            return {"dryRun": False, "action": "scroll", "amount": notches}

    def draw_shape(self, shape: str, start_x: int, start_y: int,
                   end_x: int, end_y: int, curve: float = 120.0,
                   samples: int = 160, duration_ms: int = 500,
                   humanize_profile: str | None = None,
                   cursor_injection_mode: str | None = None,
                   target_hwnd: int | None = None) -> dict:
        """Draw one continuous mathematical stroke with a held left button."""
        try:
            generated = build_shape(
                shape, start_x, start_y, end_x, end_y,
                curve=curve, samples=samples, duration_ms=duration_ms,
                screen=(self.width, self.height),
            )
        except ShapeError as error:
            raise InputError("E_PROTO", str(error)) from error
        points = generated["points"]
        if not points:
            raise InputError("E_PROTO", "shape generated no points")
        mode = self._resolve_injection_mode(cursor_injection_mode)
        with self._lock:
            self._rate_limit()
            self._failsafe()
            if self.dry_run:
                return {
                    "dryRun": True,
                    "action": "draw_shape",
                    "shape": generated["shape"],
                    "samples": len(points),
                    "points": points[:4] + ([points[-1]] if len(points) > 4 else []),
                    "cursor": {"mode": mode, "used": "post_message" if mode != "physical" else "sendinput",
                               "fallback": False, "simulated": True},
                }
            if mode != "physical":
                try:
                    target = resolve_window(target_hwnd, points[0][0], points[0][1])
                    posted = post_message_path(target, points)
                    self._overlay_show(points[-1][0], points[-1][1], "drag")
                    return {
                        "dryRun": False,
                        "action": "draw_shape",
                        "shape": generated["shape"],
                        "samples": len(points),
                        "points": points[:4] + ([points[-1]] if len(points) > 4 else []),
                        "cursor": {
                            "mode": "post_message",
                            "used": "post_message",
                            "fallback": False,
                            "target_hwnd": int(target),
                        },
                        "messages": posted.get("messages", []),
                    }
                except (VirtualCursorUnsupported, VirtualCursorFailure) as error:
                    if mode == "virtual" or int(getattr(error, "sent", 0)) > 0:
                        raise InputError(error.code, str(error), error.hint) from error
            # Physical high-density stroke: no per-point multi-ms sleeps;
            # Windows coalesces queued MOVE events for a smooth line.
            first_x, first_y = normalize_click(points[0][0], points[0][1], self.width, self.height, self.origin_x, self.origin_y)
            self._send(self._mouse_input(first_x, first_y, ABSOLUTE_MOVE))
            self._send(self._mouse_input(first_x, first_y, MOUSEEVENTF_LEFTDOWN))
            pressed = True
            try:
                for px, py in points[1:]:
                    self.interrupt.check()
                    nx, ny = normalize_click(px, py, self.width, self.height, self.origin_x, self.origin_y)
                    self._send(self._mouse_input(nx, ny, ABSOLUTE_MOVE))
                    self._sleep(0.001)
            finally:
                if pressed:
                    last_x, last_y = normalize_click(points[-1][0], points[-1][1], self.width, self.height, self.origin_x, self.origin_y)
                    self._send(self._mouse_input(last_x, last_y, MOUSEEVENTF_LEFTUP))
            return {
                "dryRun": False,
                "action": "draw_shape",
                "shape": generated["shape"],
                "samples": len(points),
                "points": points[:4] + ([points[-1]] if len(points) > 4 else []),
                "cursor": {"mode": "physical", "used": "sendinput", "fallback": False},
            }

    def drag(self, x1: int, y1: int, x2: int, y2: int,
             humanize_profile: str | None = None,
             cursor_injection_mode: str | None = None,
             target_hwnd: int | None = None,
             allow_unprotected: bool = False) -> dict:
        """Press at (x1,y1), move to (x2,y2), release; optional humanized path."""
        self.assert_privacy(target_hwnd or 0, allow_unprotected)
        cx1, cy1, c1 = clamp_click(int(x1), int(y1), self.width, self.height, self.origin_x, self.origin_y)
        cx2, cy2, c2 = clamp_click(int(x2), int(y2), self.width, self.height, self.origin_x, self.origin_y)
        warning = "坐标越界，已修正" if (c1 or c2) else None
        start = normalize_click(cx1, cy1, self.width, self.height, self.origin_x, self.origin_y)
        end = normalize_click(cx2, cy2, self.width, self.height, self.origin_x, self.origin_y)
        profile = self._resolve_profile(humanize_profile)
        mode = self._resolve_injection_mode(cursor_injection_mode)
        with self._lock:
            self._rate_limit()
            self._failsafe()
            if mode == "virtual" and self.dry_run:
                return {
                    "dryRun": True,
                    "action": "drag",
                    "from": [cx1, cy1],
                    "to": [cx2, cy2],
                    "humanize": None,
                    "cursor": {"mode": "virtual", "used": "post_message",
                               "fallback": False, "simulated": True},
                    **({"warning": warning} if warning else {}),
                }
            if mode != "physical" and not self.dry_run:
                try:
                    target = resolve_window(target_hwnd, cx1, cy1)
                    self._animate_safe(cx2, cy2, profile, "drag")
                    posted = post_message_drag(target, (cx1, cy1), (cx2, cy2))
                    posted["humanize"] = None
                    posted["cursor"] = {
                        "mode": "post_message",
                        "used": "post_message",
                        "fallback": False,
                        "target_hwnd": int(target),
                    }
                    return {
                        "dryRun": False,
                        "action": "drag",
                        "from": [cx1, cy1],
                        "to": [cx2, cy2],
                        "humanize": None,
                        "cursor": posted["cursor"],
                        **({"warning": warning} if warning else {}),
                    }
                except (VirtualCursorUnsupported, VirtualCursorFailure) as error:
                    if mode == "virtual" or int(getattr(error, "sent", 0)) > 0:
                        raise InputError(error.code, str(error), error.hint) from error
                    fallback_warning = self.VIRTUAL_FALLBACK_WARNING
                else:
                    fallback_warning = None
            else:
                fallback_warning = None
            if self.dry_run:
                humanize = None
                if profile != "off":
                    humanize = self._merge_humanize([
                        self._move_humanized(cx1, cy1, profile),
                        self._move_humanized(cx2, cy2, profile),
                    ])
                return {
                    "dryRun": True,
                    "action": "drag",
                    "from": [cx1, cy1],
                    "to": [cx2, cy2],
                    "humanize": humanize,
                    "cursor": {"mode": "physical", "used": "sendinput", "fallback": False},
                    **({"warning": warning} if warning else {}),
                }
            humanize = None
            pressed = False
            try:
                if profile != "off":
                    approach = self._move_humanized(cx1, cy1, profile)
                    self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_LEFTDOWN))
                    pressed = True
                    movement = self._move_humanized(cx2, cy2, profile)
                    humanize = self._merge_humanize([approach, movement])
                else:
                    self._send(self._mouse_input(start[0], start[1], ABSOLUTE_MOVE))
                    self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_LEFTDOWN))
                    pressed = True
                    steps = 12
                    for index in range(1, steps + 1):
                        nx = start[0] + round((end[0] - start[0]) * index / steps)
                        ny = start[1] + round((end[1] - start[1]) * index / steps)
                        self._send(self._mouse_input(nx, ny, ABSOLUTE_MOVE))
                        self._sleep(0.01)
            finally:
                if pressed:
                    self._send(self._mouse_input(end[0], end[1], MOUSEEVENTF_LEFTUP))
            return {
                "dryRun": False,
                "action": "drag",
                "from": [cx1, cy1],
                "to": [cx2, cy2],
                "humanize": humanize,
                "cursor": {
                    "mode": mode if fallback_warning else "physical",
                    "used": "sendinput",
                    "fallback": bool(fallback_warning),
                    **({"warning": fallback_warning} if fallback_warning else {}),
                },
                **({"warning": warning} if warning else {}),
            }

    def middle_drag(self, x1: int, y1: int, x2: int, y2: int,
                    humanize_profile: str | None = None,
                    cursor_injection_mode: str | None = None,
                    target_hwnd: int | None = None,
                    allow_unprotected: bool = False) -> dict:
        """P24: hold middle button and move — Blender/CAD orbit; optional humanize."""
        self.assert_privacy(target_hwnd or 0, allow_unprotected)
        cx1, cy1, c1 = clamp_click(int(x1), int(y1), self.width, self.height, self.origin_x, self.origin_y)
        cx2, cy2, c2 = clamp_click(int(x2), int(y2), self.width, self.height, self.origin_x, self.origin_y)
        warning = "坐标越界，已修正" if (c1 or c2) else None
        start = normalize_click(cx1, cy1, self.width, self.height, self.origin_x, self.origin_y)
        end = normalize_click(cx2, cy2, self.width, self.height, self.origin_x, self.origin_y)
        profile = self._resolve_profile(humanize_profile)
        mode = self._resolve_injection_mode(cursor_injection_mode)
        with self._lock:
            self._rate_limit()
            self._failsafe()
            if mode == "virtual" and self.dry_run:
                return {
                    "dryRun": True,
                    "action": "middle_drag",
                    "from": [cx1, cy1],
                    "to": [cx2, cy2],
                    "humanize": None,
                    "cursor": {"mode": "virtual", "used": "post_message",
                               "fallback": False, "simulated": True},
                    **({"warning": warning} if warning else {}),
                }
            fallback_warning = None
            if mode != "physical" and not self.dry_run:
                try:
                    target = resolve_window(target_hwnd, cx1, cy1)
                    self._animate_safe(cx2, cy2, profile, "drag")
                    posted = post_message_drag(
                        target, (cx1, cy1), (cx2, cy2), button="middle",
                    )
                    return {
                        "dryRun": False,
                        "action": "middle_drag",
                        "from": [cx1, cy1],
                        "to": [cx2, cy2],
                        "humanize": None,
                        "cursor": {
                            "mode": "post_message",
                            "used": "post_message",
                            "fallback": False,
                            "target_hwnd": int(target),
                        },
                        **({"warning": warning} if warning else {}),
                    }
                except (VirtualCursorUnsupported, VirtualCursorFailure) as error:
                    if mode == "virtual" or int(getattr(error, "sent", 0)) > 0:
                        raise InputError(error.code, str(error), error.hint) from error
                    fallback_warning = self.VIRTUAL_FALLBACK_WARNING
            if self.dry_run:
                humanize = None
                if profile != "off":
                    humanize = self._merge_humanize([
                        self._move_humanized(cx1, cy1, profile),
                        self._move_humanized(cx2, cy2, profile),
                    ])
                return {
                    "dryRun": True,
                    "action": "middle_drag",
                    "from": [cx1, cy1],
                    "to": [cx2, cy2],
                    "humanize": humanize,
                    **({"warning": warning} if warning else {}),
                }
            humanize = None
            pressed = False
            try:
                if profile != "off":
                    approach = self._move_humanized(cx1, cy1, profile)
                    self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_MIDDLEDOWN))
                    pressed = True
                    movement = self._move_humanized(cx2, cy2, profile)
                    humanize = self._merge_humanize([approach, movement])
                else:
                    self._send(self._mouse_input(start[0], start[1], ABSOLUTE_MOVE))
                    self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_MIDDLEDOWN))
                    pressed = True
                    steps = 16
                    for index in range(1, steps + 1):
                        nx = start[0] + round((end[0] - start[0]) * index / steps)
                        ny = start[1] + round((end[1] - start[1]) * index / steps)
                        self._send(self._mouse_input(nx, ny, ABSOLUTE_MOVE))
                        self._sleep(0.008)
            finally:
                if pressed:
                    self._send(self._mouse_input(end[0], end[1], MOUSEEVENTF_MIDDLEUP))
            return {
                "dryRun": False,
                "action": "middle_drag",
                "from": [cx1, cy1],
                "to": [cx2, cy2],
                "humanize": humanize,
                "cursor": {
                    "mode": mode if fallback_warning else "physical",
                    "used": "sendinput",
                    "fallback": bool(fallback_warning),
                    **({"warning": fallback_warning} if fallback_warning else {}),
                },
                **({"warning": warning} if warning else {}),
            }

    # ── generic JSON macro executor ───────────────────────────────────

    def run_macro(self, steps, variables=None, scale_factor=None,
                  humanize_profile=None, cursor_injection_mode=None,
                  scale_x=None, scale_y=None,
                  origin_x=None, origin_y=None) -> dict:
        """Execute steps; replace {{name}} placeholders and optional image scale."""
        if not isinstance(steps, list) or not (1 <= len(steps) <= 100):
            raise InputError("E_PROTO", "macro steps must be an array of 1..100 operations")
        resolved_vars = self._validate_variables(variables)
        profile = self._resolve_profile(humanize_profile)
        mode = self._resolve_injection_mode(cursor_injection_mode)
        if mode == "virtual":
            raise InputError(
                "E_VIRTUAL_CURSOR_UNSUPPORTED",
                "virtual macro replay is not supported",
                "宏回放请使用 hybrid/physical；virtual 模式不会抢占物理鼠标",
            )
        scale_x, scale_y = self._resolve_scale(scale_factor, scale_x, scale_y)
        origin_x = self.origin_x if origin_x is None else int(origin_x)
        origin_y = self.origin_y if origin_y is None else int(origin_y)
        resolved = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise InputError("E_PROTO", f"macro step {index + 1} is not an object")
            resolved.append(
                self._prepare_step(step, resolved_vars, index, scale_x, scale_y, origin_x, origin_y)
            )
        texts = [step["text"] for step in resolved if step.get("op") == "type" and isinstance(step.get("text"), str)]
        coords = [step["coords"] for step in resolved if "coords" in step]
        warnings = ["坐标越界，已修正"] if any(step.get("clamped") for step in resolved) else []
        started = time.perf_counter()
        executed = []
        with self._lock:
            self._rate_limit()
            self._failsafe()
            if self.dry_run:
                return {
                    "dryRun": True,
                    "count": len(resolved),
                    "steps": [str(step.get("op")) for step in resolved],
                    "texts": texts,
                    "coords": coords,
                    "variables": sorted(resolved_vars),
                    "scaleFactor": scale_x if abs(scale_x - scale_y) < 1e-9 else None,
                    "scaleX": scale_x,
                    "scaleY": scale_y,
                    "elapsedMs": 0.0,
                    "humanizeProfile": profile,
                    **({"warnings": warnings} if warnings else {}),
                }
            for index, step in enumerate(resolved):
                op = str(step.get("op") or "")
                try:
                    self.interrupt.check()
                    self._run_step(op, step, profile)
                except InputError as error:
                    raise InputError(
                        "E_MACRO_FAILED",
                        f"macro step {index + 1} ({op}) failed: {error}",
                        error.hint,
                    ) from error
                except InterruptError:
                    raise
                except Exception as error:  # noqa: BLE001
                    raise InputError("E_MACRO_FAILED", f"macro step {index + 1} ({op}) failed: {error}") from error
                executed.append(op)
            return {
                "dryRun": False,
                "count": len(resolved),
                "steps": executed,
                "texts": texts,
                "coords": coords,
                "variables": sorted(resolved_vars),
                "scaleFactor": scale_x if abs(scale_x - scale_y) < 1e-9 else None,
                "scaleX": scale_x,
                "scaleY": scale_y,
                "elapsedMs": round((time.perf_counter() - started) * 1000.0, 1),
                "humanizeProfile": profile,
                **({"warnings": warnings} if warnings else {}),
            }

    _MOUSE_COORDS = {
        "click": ("x", "y"),
        "double_click": ("x", "y"),
        "right_click": ("x", "y"),
        "drag": ("x1", "y1", "x2", "y2"),
        "middle_drag": ("x1", "y1", "x2", "y2"),
    }

    def _prepare_step(self, step: dict, variables: dict, index: int,
                      scale_x: float, scale_y: float,
                      origin_x: int = 0, origin_y: int = 0) -> dict:
        """Resolve placeholders in text/coordinates, then scale mouse coordinates."""
        prepared = dict(step)
        for key in ("text", "x", "y", "x1", "y1", "x2", "y2"):
            value = prepared.get(key)
            if isinstance(value, str) and "{{" in value:
                prepared[key] = self._resolve_variables(value, variables, index)
        fields = self._MOUSE_COORDS.get(str(prepared.get("op")))
        if not fields:
            return prepared
        coords = []
        for field in fields:
            try:
                value = int(prepared.get(field))
            except (TypeError, ValueError) as error:
                raise InputError(
                    "E_PROTO",
                    f"macro step {index + 1} ({prepared.get('op')}): {field} must be an integer",
                ) from error
            origin = origin_x if field.startswith("x") else origin_y
            limit = (origin_x + self.width) if field.startswith("x") else (origin_y + self.height)
            scaled = int(round(value * (scale_x if field.startswith("x") else scale_y))) + origin
            clamped = min(max(origin, scaled), limit - 1)
            if clamped != scaled:
                prepared["clamped"] = True
            prepared[field] = clamped
            coords.append(clamped)
        prepared["coords"] = coords
        return prepared

    @staticmethod
    def _validate_variables(variables) -> dict:
        if variables is None:
            return {}
        if not isinstance(variables, dict):
            raise InputError("E_PROTO", "variables must be an object")
        if len(variables) > 20:
            raise InputError("E_PROTO", "at most 20 variables per macro")
        cleaned = {}
        for key, value in variables.items():
            name = str(key)
            if not re.fullmatch(r"[a-zA-Z0-9_]{1,40}", name):
                raise InputError("E_PROTO", f"invalid variable name {name!r}")
            if not isinstance(value, str):
                raise InputError("E_PROTO", f"variable {name} must be a string")
            if len(value) > 2000:
                raise InputError("E_PROTO", f"variable {name} exceeds 2000 chars")
            cleaned[name] = value
        return cleaned

    @staticmethod
    def _resolve_variables(text: str, variables: dict, index: int) -> str:
        missing = []

        def replace(match):
            name = match.group(1)
            if name not in variables:
                missing.append(name)
                return match.group(0)
            return variables[name]

        result = re.sub(r"\{\{([a-zA-Z0-9_]+)\}\}", replace, text)
        if missing:
            names = ", ".join(sorted(set(missing)))
            raise InputError(
                "E_PROTO",
                f"macro step {index + 1}: missing variable(s) {names}",
                "调用 vhcu_run_macro 时传 variables:{name:value}",
            )
        return result

    def _run_step(self, op: str, step: dict, humanize_profile: str = "off") -> None:
        if op == "click":
            x = int(step.get("x"))
            y = int(step.get("y"))
            if humanize_profile != "off":
                self._click_button_humanized(x, y, "left", humanize_profile, 1)
                return
            nx, ny = normalize_click(x, y, self.width, self.height, self.origin_x, self.origin_y)
            self._send(
                self._mouse_input(nx, ny, ABSOLUTE_MOVE),
                self._mouse_input(nx, ny, MOUSEEVENTF_LEFTDOWN),
                self._mouse_input(nx, ny, MOUSEEVENTF_LEFTUP),
            )
            return
        if op == "double_click":
            x = int(step.get("x"))
            y = int(step.get("y"))
            if humanize_profile != "off":
                self._click_button_humanized(x, y, "left", humanize_profile, 2)
                return
            nx, ny = normalize_click(x, y, self.width, self.height, self.origin_x, self.origin_y)
            self._send(self._mouse_input(nx, ny, ABSOLUTE_MOVE))
            for _ in range(2):
                self._send(
                    self._mouse_input(nx, ny, MOUSEEVENTF_LEFTDOWN),
                    self._mouse_input(nx, ny, MOUSEEVENTF_LEFTUP),
                )
                self._sleep(0.05)
            return
        if op == "right_click":
            x = int(step.get("x"))
            y = int(step.get("y"))
            if humanize_profile != "off":
                self._click_button_humanized(x, y, "right", humanize_profile, 1)
                return
            nx, ny = normalize_click(x, y, self.width, self.height, self.origin_x, self.origin_y)
            self._send(
                self._mouse_input(nx, ny, ABSOLUTE_MOVE),
                self._mouse_input(nx, ny, MOUSEEVENTF_RIGHTDOWN),
                self._mouse_input(nx, ny, MOUSEEVENTF_RIGHTUP),
            )
            return
        if op == "type":
            text = str(step.get("text") or "")
            if len(text) > MAX_TEXT_CHARS:
                raise InputError("E_PROTO", f"text is longer than {MAX_TEXT_CHARS} chars")
            if text:
                self._ensure_ascii_input()
                self._type_unicode(text)
            return
        if op == "key":
            self._send_combo(parse_hotkey(step.get("keys")))
            return
        if op == "scroll":
            amount = int(step.get("amount"))
            if amount == 0 or abs(amount) > 20:
                raise InputError("E_PROTO", "scroll amount must be non-zero and within +/-20")
            self._send(self._mouse_input(0, 0, MOUSEEVENTF_WHEEL, mouse_data=amount * WHEEL_DELTA))
            return
        if op == "drag":
            x1 = int(step.get("x1"))
            y1 = int(step.get("y1"))
            x2 = int(step.get("x2"))
            y2 = int(step.get("y2"))
            start = normalize_click(x1, y1, self.width, self.height, self.origin_x, self.origin_y)
            end = normalize_click(x2, y2, self.width, self.height, self.origin_x, self.origin_y)
            pressed = False
            try:
                if humanize_profile != "off":
                    self._move_humanized(x1, y1, humanize_profile)
                    self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_LEFTDOWN))
                    pressed = True
                    self._move_humanized(x2, y2, humanize_profile)
                else:
                    self._send(self._mouse_input(start[0], start[1], ABSOLUTE_MOVE))
                    self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_LEFTDOWN))
                    pressed = True
                    for index in range(1, 13):
                        nx = start[0] + round((end[0] - start[0]) * index / 12)
                        ny = start[1] + round((end[1] - start[1]) * index / 12)
                        self._send(self._mouse_input(nx, ny, ABSOLUTE_MOVE))
                        self._sleep(0.01)
            finally:
                if pressed:
                    self._send(self._mouse_input(end[0], end[1], MOUSEEVENTF_LEFTUP))
            return
        if op == "middle_drag":
            x1 = int(step.get("x1"))
            y1 = int(step.get("y1"))
            x2 = int(step.get("x2"))
            y2 = int(step.get("y2"))
            start = normalize_click(x1, y1, self.width, self.height, self.origin_x, self.origin_y)
            end = normalize_click(x2, y2, self.width, self.height, self.origin_x, self.origin_y)
            pressed = False
            try:
                if humanize_profile != "off":
                    self._move_humanized(x1, y1, humanize_profile)
                    self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_MIDDLEDOWN))
                    pressed = True
                    self._move_humanized(x2, y2, humanize_profile)
                else:
                    self._send(self._mouse_input(start[0], start[1], ABSOLUTE_MOVE))
                    self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_MIDDLEDOWN))
                    pressed = True
                    for index in range(1, 17):
                        nx = start[0] + round((end[0] - start[0]) * index / 16)
                        ny = start[1] + round((end[1] - start[1]) * index / 16)
                        self._send(self._mouse_input(nx, ny, ABSOLUTE_MOVE))
                        self._sleep(0.008)
            finally:
                if pressed:
                    self._send(self._mouse_input(end[0], end[1], MOUSEEVENTF_MIDDLEUP))
            return
        if op == "wait":
            ms = int(step.get("ms"))
            if not (0 <= ms <= 5000):
                raise InputError("E_PROTO", "wait ms must be within 0..5000")
            self._sleep(ms / 1000.0)
            return
        raise InputError("E_PROTO", f"unsupported macro op {op!r}")

    # ── internals ────────────────────────────────────────────────────

    def _failsafe(self) -> None:
        if self.dry_run or not IS_WINDOWS:
            return
        x, y = cursor_position()
        if in_corner(x - self.origin_x, y - self.origin_y, self.width, self.height):
            raise InputError(
                "E_DENIED",
                f"mouse failsafe: cursor is parked in a screen corner at ({x},{y})",
                "把鼠标移离屏幕四角后重试；这是人工急停通道",
            )

    def _sleep(self, seconds: float) -> None:
        self.interrupt.sleep(seconds)

    def _rate_limit(self) -> None:
        self.interrupt.check()
        now = time.monotonic()
        self._sent = [stamp for stamp in self._sent if now - stamp <= RATE_WINDOW_S]
        if len(self._sent) >= RATE_MAX:
            raise InputError("E_BUSY", f"more than {RATE_MAX} input actions in {RATE_WINDOW_S}s", "等待几秒或合并操作")
        self._sent.append(now)

    def _mouse_input(self, dx: int, dy: int, flags: int, mouse_data: int = 0) -> "INPUT":
        event = INPUT()
        event.type = INPUT_MOUSE
        event.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=mouse_data, dwFlags=flags, time=0, dwExtraInfo=0)
        return event

    def _key_input(self, vk: int, scan: int, flags: int) -> "INPUT":
        event = INPUT()
        event.type = INPUT_KEYBOARD
        event.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        return event

    def _key_scan_input(self, vk: int, flags: int) -> "INPUT":
        # Win keys must carry EXTENDEDKEY or SendInput delivers a plain scan
        # code that many apps/RDP/UIPI paths reject (whitepaper 8.9).
        flags |= extended_key_flag(vk)
        scan = 0
        if IS_WINDOWS:
            try:
                scan = int(ctypes.windll.user32.MapVirtualKeyW(int(vk), 0))
            except Exception:  # noqa: BLE001
                scan = 0
        if scan <= 0:
            return self._key_input(vk, 0, flags)
        return self._key_input(0, scan, KEYEVENTF_SCANCODE | flags)

    def _tap_vk(self, vk: int) -> None:
        self._send(self._key_input(vk, 0, 0), self._key_input(vk, 0, KEYEVENTF_KEYUP))

    def _send(self, *events: "INPUT") -> None:
        if not IS_WINDOWS:
            raise InputError("E_UNAVAILABLE", "input injection requires Windows")
        if not events:
            return
        array = (INPUT * len(events))(*events)
        sent = ctypes.windll.user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
        if sent != len(events):
            raise InputError("E_INTERNAL", f"SendInput accepted {sent}/{len(events)} events")
