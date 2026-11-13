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
import os
import re
import threading
import time
from ctypes import wintypes

from .interrupts import InterruptController, InterruptError
from .privacy import foreground_identity
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
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
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


def cursor_position() -> tuple[int, int]:
    if not IS_WINDOWS:
        return (0, 0)
    point = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return (int(point.x), int(point.y))


def in_corner(x: int, y: int, width: int, height: int, margin: int = CORNER_PX) -> bool:
    """pyautogui.FAILSAFE equivalent; implementation lives in safety.py."""
    return in_screen_corner(x, y, width, height, margin)


def clamp_click(x: int, y: int, width: int, height: int) -> tuple[int, int, bool]:
    """P14: clamp any coordinate into the primary screen instead of failing."""
    try:
        return clamp_point(x, y, width, height)
    except InvalidScreen as error:
        raise InputError("E_UNAVAILABLE", "primary screen size is unavailable") from error


def normalize_click(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    try:
        cx, cy = normalize_point(x, y, width, height)
    except InvalidScreen as error:
        raise InputError("E_UNAVAILABLE", "primary screen size is unavailable") from error
    # SendInput absolute coordinates are normalized to 0..65535 over the screen.
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
    def __init__(self, dry_run: bool | None = None, interrupt: InterruptController | None = None):
        self.dry_run = (os.environ.get("VHCU_INPUT_DRY_RUN") == "1") if dry_run is None else dry_run
        self.dpi_mode = enable_dpi_awareness()
        self.width, self.height = primary_size()
        self._lock = threading.RLock()
        self._sent: list[float] = []
        self.interrupt = interrupt or InterruptController()

    def status(self) -> dict:
        return {
            "dryRun": self.dry_run,
            "dpiMode": self.dpi_mode,
            "screen": {"width": self.width, "height": self.height},
            "interrupt": self.interrupt.status(),
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

    # ── public actions ───────────────────────────────────────────────

    def click(self, x: int, y: int, scale_factor: float | None = None) -> dict:
        try:
            raw_x = int(x)
            raw_y = int(y)
        except (TypeError, ValueError) as error:
            raise InputError("E_PROTO", "click x/y must be integers") from error
        scale = 1.0
        if scale_factor is not None:
            try:
                scale = float(scale_factor)
            except (TypeError, ValueError) as error:
                raise InputError("E_PROTO", "scaleFactor must be a number") from error
            if not (0.1 <= scale <= 10.0):
                raise InputError("E_PROTO", "scaleFactor must be within 0.1..10")
        real_x = int(round(raw_x * scale))
        real_y = int(round(raw_y * scale))
        real_x, real_y, clamped = clamp_click(real_x, real_y, self.width, self.height)
        warning = "坐标越界，已修正" if clamped else None
        with self._lock:
            self._rate_limit()
            self._failsafe()
            nx, ny = normalize_click(real_x, real_y, self.width, self.height)
            if self.dry_run:
                return {
                    "dryRun": True,
                    "action": "click",
                    "x": real_x,
                    "y": real_y,
                    "requested": [raw_x, raw_y],
                    "scaleFactor": scale,
                    "applied": [real_x, real_y],
                    "normalized": [nx, ny],
                    **({"warning": warning} if warning else {}),
                }
            self._send(
                self._mouse_input(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE),
                self._mouse_input(nx, ny, MOUSEEVENTF_LEFTDOWN),
                self._mouse_input(nx, ny, MOUSEEVENTF_LEFTUP),
            )
            return {
                "dryRun": False,
                "action": "click",
                "x": real_x,
                "y": real_y,
                "requested": [raw_x, raw_y],
                "scaleFactor": scale,
                "applied": [real_x, real_y],
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
        mod_down = [self._key_scan_input(vk, 0) for vk in modifiers]
        main_events = [
            self._key_scan_input(main, 0),
            self._key_scan_input(main, KEYEVENTF_KEYUP),
        ]
        mod_up = [self._key_scan_input(vk, KEYEVENTF_KEYUP) for vk in reversed(modifiers)]
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

    def scroll(self, amount: int) -> dict:
        """Wheel scroll. Positive = up, negative = down; one unit = one notch."""
        try:
            notches = int(amount)
        except (TypeError, ValueError) as error:
            raise InputError("E_PROTO", "scroll amount must be an integer") from error
        if notches == 0:
            raise InputError("E_PROTO", "scroll amount must not be zero")
        if abs(notches) > 20:
            raise InputError("E_PROTO", "scroll amount is limited to 20 notches per call")
        with self._lock:
            self._rate_limit()
            self._failsafe()
            if self.dry_run:
                return {"dryRun": True, "action": "scroll", "amount": notches}
            self._send(
                self._mouse_input(0, 0, MOUSEEVENTF_WHEEL, mouse_data=notches * WHEEL_DELTA)
            )
            return {"dryRun": False, "action": "scroll", "amount": notches}

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> dict:
        """Press at (x1,y1), move in small absolute steps, release at (x2,y2)."""
        cx1, cy1, c1 = clamp_click(int(x1), int(y1), self.width, self.height)
        cx2, cy2, c2 = clamp_click(int(x2), int(y2), self.width, self.height)
        warning = "坐标越界，已修正" if (c1 or c2) else None
        start = normalize_click(cx1, cy1, self.width, self.height)
        end = normalize_click(cx2, cy2, self.width, self.height)
        with self._lock:
            self._rate_limit()
            self._failsafe()
            if self.dry_run:
                return {
                    "dryRun": True,
                    "action": "drag",
                    "from": [cx1, cy1],
                    "to": [cx2, cy2],
                    **({"warning": warning} if warning else {}),
                }
            try:
                self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE))
                self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_LEFTDOWN))
                steps = 12
                for index in range(1, steps + 1):
                    nx = start[0] + round((end[0] - start[0]) * index / steps)
                    ny = start[1] + round((end[1] - start[1]) * index / steps)
                    self._send(self._mouse_input(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE))
                    self._sleep(0.01)
            finally:
                self._send(self._mouse_input(end[0], end[1], MOUSEEVENTF_LEFTUP))
            return {
                "dryRun": False,
                "action": "drag",
                "from": [cx1, cy1],
                "to": [cx2, cy2],
                **({"warning": warning} if warning else {}),
            }

    def middle_drag(self, x1: int, y1: int, x2: int, y2: int) -> dict:
        """P24: hold middle button and smoothly move — Blender/CAD orbit."""
        cx1, cy1, c1 = clamp_click(int(x1), int(y1), self.width, self.height)
        cx2, cy2, c2 = clamp_click(int(x2), int(y2), self.width, self.height)
        warning = "坐标越界，已修正" if (c1 or c2) else None
        start = normalize_click(cx1, cy1, self.width, self.height)
        end = normalize_click(cx2, cy2, self.width, self.height)
        with self._lock:
            self._rate_limit()
            self._failsafe()
            if self.dry_run:
                return {
                    "dryRun": True,
                    "action": "middle_drag",
                    "from": [cx1, cy1],
                    "to": [cx2, cy2],
                    **({"warning": warning} if warning else {}),
                }
            try:
                self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE))
                self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_MIDDLEDOWN))
                steps = 16
                for index in range(1, steps + 1):
                    nx = start[0] + round((end[0] - start[0]) * index / steps)
                    ny = start[1] + round((end[1] - start[1]) * index / steps)
                    self._send(self._mouse_input(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE))
                    self._sleep(0.008)
            finally:
                self._send(self._mouse_input(end[0], end[1], MOUSEEVENTF_MIDDLEUP))
            return {
                "dryRun": False,
                "action": "middle_drag",
                "from": [cx1, cy1],
                "to": [cx2, cy2],
                **({"warning": warning} if warning else {}),
            }

    # ── generic JSON macro executor ───────────────────────────────────

    def run_macro(self, steps, variables=None, scale_factor=None) -> dict:
        """Execute steps; replace {{name}} placeholders and optional image scale."""
        if not isinstance(steps, list) or not (1 <= len(steps) <= 100):
            raise InputError("E_PROTO", "macro steps must be an array of 1..100 operations")
        resolved_vars = self._validate_variables(variables)
        scale = 1.0
        if scale_factor is not None:
            try:
                scale = float(scale_factor)
            except (TypeError, ValueError) as error:
                raise InputError("E_PROTO", "scaleFactor must be a number") from error
            if not (0.1 <= scale <= 10.0):
                raise InputError("E_PROTO", "scaleFactor must be within 0.1..10")
        resolved = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise InputError("E_PROTO", f"macro step {index + 1} is not an object")
            resolved.append(self._prepare_step(step, resolved_vars, index, scale))
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
                    "scaleFactor": scale,
                    "elapsedMs": 0.0,
                    **({"warnings": warnings} if warnings else {}),
                }
            for index, step in enumerate(resolved):
                op = str(step.get("op") or "")
                try:
                    self.interrupt.check()
                    self._run_step(op, step)
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
                "scaleFactor": scale,
                "elapsedMs": round((time.perf_counter() - started) * 1000.0, 1),
                **({"warnings": warnings} if warnings else {}),
            }

    _MOUSE_COORDS = {
        "click": ("x", "y"),
        "double_click": ("x", "y"),
        "right_click": ("x", "y"),
        "drag": ("x1", "y1", "x2", "y2"),
        "middle_drag": ("x1", "y1", "x2", "y2"),
    }

    def _prepare_step(self, step: dict, variables: dict, index: int, scale: float) -> dict:
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
            scaled = int(round(value * scale))
            limit = self.width if field.startswith("x") else self.height
            clamped = min(max(0, scaled), limit - 1)
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

    def _run_step(self, op: str, step: dict) -> None:
        if op == "click":
            x = int(step.get("x"))
            y = int(step.get("y"))
            nx, ny = normalize_click(x, y, self.width, self.height)
            self._send(
                self._mouse_input(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE),
                self._mouse_input(nx, ny, MOUSEEVENTF_LEFTDOWN),
                self._mouse_input(nx, ny, MOUSEEVENTF_LEFTUP),
            )
            return
        if op == "double_click":
            x = int(step.get("x"))
            y = int(step.get("y"))
            nx, ny = normalize_click(x, y, self.width, self.height)
            self._send(self._mouse_input(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE))
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
            nx, ny = normalize_click(x, y, self.width, self.height)
            self._send(
                self._mouse_input(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE),
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
            start = normalize_click(x1, y1, self.width, self.height)
            end = normalize_click(x2, y2, self.width, self.height)
            try:
                self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE))
                self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_LEFTDOWN))
                for index in range(1, 13):
                    nx = start[0] + round((end[0] - start[0]) * index / 12)
                    ny = start[1] + round((end[1] - start[1]) * index / 12)
                    self._send(self._mouse_input(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE))
                    self._sleep(0.01)
            finally:
                self._send(self._mouse_input(end[0], end[1], MOUSEEVENTF_LEFTUP))
            return
        if op == "middle_drag":
            x1 = int(step.get("x1"))
            y1 = int(step.get("y1"))
            x2 = int(step.get("x2"))
            y2 = int(step.get("y2"))
            start = normalize_click(x1, y1, self.width, self.height)
            end = normalize_click(x2, y2, self.width, self.height)
            try:
                self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE))
                self._send(self._mouse_input(start[0], start[1], MOUSEEVENTF_MIDDLEDOWN))
                for index in range(1, 17):
                    nx = start[0] + round((end[0] - start[0]) * index / 16)
                    ny = start[1] + round((end[1] - start[1]) * index / 16)
                    self._send(self._mouse_input(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE))
                    self._sleep(0.008)
            finally:
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
        if in_corner(x, y, self.width, self.height):
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
