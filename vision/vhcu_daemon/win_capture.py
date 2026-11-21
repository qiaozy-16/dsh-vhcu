"""GDI window capture and content-quality checks for Windows.

This module is deliberately Windows-only. It is imported lazily by
``sources.create_source`` so importing the daemon on a non-Windows machine does
not touch user32/gdi32.

Capture order (all within the target window rect):

1. PrintWindow with PW_RENDERFULLCONTENT
2. PrintWindow without extra flags
3. WM_PRINT
4. BitBlt from the screen DC (only meaningful when the window is visible)

Every candidate is sampled sparsely. A near-uniform/black frame is rejected
and the next backend is tried. If every backend is blank, the caller gets
E_CAPTURE_BLACK instead of a silent black image.
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from ctypes import wintypes
from typing import Optional

_IS_WINDOWS = os.name == "nt"
_MAX_WINDOW_DIM = 8192
_SAMPLE_BUDGET = 4096
_PW_RENDERFULLCONTENT = 0x00000002
_WM_PRINT = 0x0317
_PRF_CHECKVISIBLE = 0x00000001
_PRF_NONCLIENT = 0x00000002
_PRF_CLIENT = 0x00000004
_PRF_ERASEBKGND = 0x00000008
_PRF_CHILDREN = 0x00000010
_PRF_OWNED = 0x00000020
_SRCCOPY = 0x00CC0020
_DIB_RGB_COLORS = 0
_GA_ROOT = 2

_dpi_ready = False


class WindowCaptureError(RuntimeError):
    def __init__(self, code: str, message: str, hint: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.hint = hint


@dataclass(frozen=True)
class CaptureImage:
    width: int
    height: int
    data: bytes
    backend: str
    occluded: bool
    blank: bool


@dataclass(frozen=True)
class Quality:
    blank: bool
    samples: int
    black_ratio: float
    dominant_ratio: float


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER)]


_user32 = None
_gdi32 = None


def _api():
    global _user32, _gdi32
    if _user32 is None or _gdi32 is None:
        if not _IS_WINDOWS:
            raise WindowCaptureError("E_UNAVAILABLE", "window capture requires Windows")
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        _configure_prototypes(_user32, _gdi32)
    return _user32, _gdi32


def _configure_prototypes(user32, gdi32) -> None:
    HWND = wintypes.HWND
    HDC = wintypes.HDC
    RECT = wintypes.RECT
    POINT = wintypes.POINT
    HBITMAP = wintypes.HBITMAP

    user32.GetForegroundWindow.restype = HWND
    user32.IsWindow.argtypes = [HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = HWND
    user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowDC.argtypes = [HWND]
    user32.GetWindowDC.restype = HDC
    user32.GetDC.argtypes = [HWND]
    user32.GetDC.restype = HDC
    user32.ReleaseDC.argtypes = [HWND, HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.WindowFromPoint.argtypes = [POINT]
    user32.WindowFromPoint.restype = HWND
    user32.GetAncestor.argtypes = [HWND, wintypes.UINT]
    user32.GetAncestor.restype = HWND
    user32.PrintWindow.argtypes = [HWND, HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.SendMessageW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM
    user32.GetWindowTextLengthW.argtypes = [HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    try:
        user32.GetDpiForWindow.argtypes = [HWND]
        user32.GetDpiForWindow.restype = wintypes.UINT
    except Exception:  # noqa: BLE001
        pass

    gdi32.CreateCompatibleDC.argtypes = [HDC]
    gdi32.CreateCompatibleDC.restype = HDC
    gdi32.CreateCompatibleBitmap.argtypes = [HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = HBITMAP
    gdi32.SelectObject.argtypes = [HDC, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.BitBlt.argtypes = [
        HDC,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
    ]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        HDC,
        HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int


def _ensure_dpi_aware() -> None:
    """Make rects/Windows coordinates physical before any capture."""
    global _dpi_ready
    if _dpi_ready or not _IS_WINDOWS:
        return
    _dpi_ready = True
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # PER_MONITOR_AWARE_V2 = -4; process must set this before first window.
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness(2)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.WinDLL("user32").SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    user32, _ = _api()
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise WindowCaptureError("E_CAPTURE_TARGET_GONE", f"GetWindowRect failed for hwnd {hwnd}")
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        raise WindowCaptureError("E_CAPTURE_TARGET_GONE", f"window {hwnd} has no usable rectangle")
    if width > _MAX_WINDOW_DIM or height > _MAX_WINDOW_DIM:
        raise WindowCaptureError(
            "E_CAPTURE_UNSUPPORTED",
            f"window {hwnd} is {width}x{height}; max supported is {_MAX_WINDOW_DIM}",
        )
    return int(rect.left), int(rect.top), width, height


def analyze_capture(data: bytes, width: int, height: int,
                    sample_budget: int = _SAMPLE_BUDGET) -> Quality:
    """Sparse BGRA content check; never returns a silent all-black result."""
    if width <= 0 or height <= 0 or len(data) < width * height * 4:
        return Quality(blank=True, samples=0, black_ratio=1.0, dominant_ratio=1.0)
    total = width * height
    budget = max(16, min(int(sample_budget), total))
    step = max(1, total // budget)
    samples = 0
    black = 0
    buckets: dict[int, int] = {}
    offset = 0
    # Stride through rows so one row cannot dominate a pathological sample.
    for index in range(0, total, step):
        px = index * 4
        b, g, r = data[px], data[px + 1], data[px + 2]
        samples += 1
        if b <= 3 and g <= 3 and r <= 3:
            black += 1
        key = ((b >> 3) << 10) | ((g >> 3) << 5) | (r >> 3)
        buckets[key] = buckets.get(key, 0) + 1
        offset += 4
        if samples >= budget:
            break
    black_ratio = black / samples
    dominant_ratio = max(buckets.values()) / samples if buckets else 0.0
    # CUA uses >99.5% black; single-color blank detection is stricter because
    # a legitimate dark UI can contain nearly-black areas.
    blank = black_ratio > 0.995 or dominant_ratio > 0.999
    return Quality(
        blank=blank,
        samples=samples,
        black_ratio=round(black_ratio, 6),
        dominant_ratio=round(dominant_ratio, 6),
    )


def _is_foreground(hwnd: int) -> bool:
    user32, _ = _api()
    target = int(user32.GetAncestor(hwnd, _GA_ROOT) or hwnd)
    foreground = int(user32.GetForegroundWindow() or 0)
    if not foreground:
        return False
    return int(user32.GetAncestor(foreground, _GA_ROOT) or foreground) == target


def _is_occluded(hwnd: int, left: int, top: int, width: int, height: int) -> bool:
    user32, _ = _api()
    target_root = int(user32.GetAncestor(hwnd, _GA_ROOT) or hwnd)
    inset = max(1, min(8, min(width, height) // 20))
    points = [
        (left + width // 2, top + height // 2),
        (left + inset, top + inset),
        (left + width - inset - 1, top + inset),
        (left + inset, top + height - inset - 1),
        (left + width - inset - 1, top + height - inset - 1),
    ]
    for x, y in points:
        hit = int(user32.WindowFromPoint(wintypes.POINT(int(x), int(y))) or 0)
        if not hit:
            return True
        hit_root = int(user32.GetAncestor(hit, _GA_ROOT) or hit)
        if hit_root != target_root:
            return True
    return False


def _dib_from_dc(memdc, bitmap, width: int, height: int) -> bytes:
    _, gdi32 = _api()
    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB
    buffer = ctypes.create_string_buffer(width * height * 4)
    copied = gdi32.GetDIBits(
        memdc,
        bitmap,
        0,
        height,
        ctypes.cast(buffer, ctypes.c_void_p),
        ctypes.byref(bmi),
        _DIB_RGB_COLORS,
    )
    if copied != height:
        raise WindowCaptureError("E_CAPTURE", f"GetDIBits copied {copied}/{height} rows")
    return buffer.raw


def _capture_with_dc(hwnd: int, width: int, height: int, mode: str, left: int = 0, top: int = 0) -> Optional[bytes]:
    user32, gdi32 = _api()
    window_dc = user32.GetWindowDC(hwnd)
    if not window_dc:
        return None
    mem_dc = None
    bitmap = None
    previous = None
    try:
        mem_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        if not mem_dc or not bitmap:
            return None
        previous = gdi32.SelectObject(mem_dc, bitmap)
        if mode == "bitblt":
            screen_dc = user32.GetDC(None)
            try:
                if not screen_dc:
                    return None
                ok = gdi32.BitBlt(mem_dc, 0, 0, width, height, screen_dc, int(left), int(top), _SRCCOPY)
            finally:
                user32.ReleaseDC(None, screen_dc)
        elif mode == "wm_print":
            flags = (
                _PRF_CHECKVISIBLE
                | _PRF_NONCLIENT
                | _PRF_CLIENT
                | _PRF_ERASEBKGND
                | _PRF_CHILDREN
                | _PRF_OWNED
            )
            user32.SendMessageW(hwnd, _WM_PRINT, int(mem_dc), int(flags))
            ok = True
        elif mode == "printwindow_plain":
            ok = bool(user32.PrintWindow(hwnd, mem_dc, 0))
        else:
            ok = bool(user32.PrintWindow(hwnd, mem_dc, _PW_RENDERFULLCONTENT))
        if not ok:
            return None
        return _dib_from_dc(mem_dc, bitmap, width, height)
    except OSError:
        return None
    finally:
        if previous:
            try:
                gdi32.SelectObject(mem_dc, previous)
            except Exception:  # noqa: BLE001
                pass
        if bitmap:
            try:
                gdi32.DeleteObject(bitmap)
            except Exception:  # noqa: BLE001
                pass
        if mem_dc:
            try:
                gdi32.DeleteDC(mem_dc)
            except Exception:  # noqa: BLE001
                pass
        try:
            user32.ReleaseDC(hwnd, window_dc)
        except Exception:  # noqa: BLE001
            pass


def capture_window(hwnd: int, *, strict_occlusion: bool = False) -> CaptureImage:
    """Capture one HWND with PrintWindow/WM_PRINT/BitBlt fallback."""
    if not _IS_WINDOWS:
        raise WindowCaptureError("E_UNAVAILABLE", "window capture requires Windows")
    _ensure_dpi_aware()
    user32, _ = _api()
    hwnd = int(hwnd or 0)
    if hwnd <= 0 or not user32.IsWindow(hwnd):
        raise WindowCaptureError("E_CAPTURE_TARGET_NOT_FOUND", f"invalid or destroyed hwnd {hwnd}")
    root_hwnd = int(user32.GetAncestor(hwnd, _GA_ROOT) or hwnd)
    if user32.IsIconic(hwnd) or (root_hwnd != hwnd and user32.IsIconic(root_hwnd)):
        raise WindowCaptureError(
            "E_CAPTURE_MINIMIZED",
            f"window {hwnd} is minimized and has no rendered frame",
            "恢复目标窗口后再截图；最小化窗口不能后台捕获",
        )
    left, top, width, height = _rect(hwnd)
    occluded = _is_occluded(hwnd, left, top, width, height)
    attempts: list[tuple[str, str]] = [
        ("printwindow_full", "printwindow_full"),
        ("printwindow_plain", "printwindow_plain"),
        ("wm_print", "wm_print"),
    ]
    last_quality: Optional[Quality] = None
    for backend, mode in attempts:
        try:
            data = _capture_with_dc(hwnd, width, height, mode, left, top)
        except Exception:  # noqa: BLE001
            data = None
        if not data:
            continue
        quality = analyze_capture(data, width, height)
        last_quality = quality
        if quality.blank:
            continue
        return CaptureImage(
            width=width,
            height=height,
            data=data,
            backend=backend,
            occluded=bool(occluded),
            blank=False,
        )

    # P4: only a visible, foreground, unoccluded target may use the screen
    # BitBlt fallback. Capturing an occluded/minimized window would leak or
    # return another window's pixels.
    if strict_occlusion and occluded:
        raise WindowCaptureError(
            "E_CAPTURE_OCCLUDED",
            f"window {hwnd} is covered; BitBlt would capture another window",
            "将目标窗口置前，或使用 PrintWindow 可用的窗口",
        )
    if occluded:
        raise WindowCaptureError(
            "E_CAPTURE_OCCLUDED",
            f"window {hwnd} is occluded; screen BitBlt fallback is unavailable",
            "将目标窗口置前并保持可见后重试",
        )
    if not bool(user32.IsWindowVisible(hwnd)) or not _is_foreground(hwnd):
        raise WindowCaptureError(
            "E_CAPTURE_OCCLUDED",
            f"window {hwnd} is not foreground-visible; screen BitBlt fallback is unavailable",
            "激活目标窗口并保持其在最前，再重试",
        )
    try:
        data = _capture_with_dc(hwnd, width, height, "bitblt", left, top)
    except Exception:  # noqa: BLE001
        data = None
    if data:
        quality = analyze_capture(data, width, height)
        last_quality = quality
        if not quality.blank:
            return CaptureImage(
                width=width,
                height=height,
                data=data,
                backend="bitblt",
                occluded=False,
                blank=False,
            )
    detail = ""
    if last_quality is not None:
        detail = f" (samples={last_quality.samples}, black={last_quality.black_ratio}, dominant={last_quality.dominant_ratio})"
    raise WindowCaptureError(
        "E_CAPTURE_BLACK",
        f"all GDI backends returned blank pixels for hwnd {hwnd}{detail}",
        "UWP/WinUI3 或 DirectComposition 窗口可能没有 GDI 后备缓冲区；P4 仅在前台可见时使用屏幕 BitBlt",
    )


def ensure_dpi_awareness() -> None:
    """Public alias used by the UIA worker before COM property reads."""
    _ensure_dpi_aware()


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Return the target's physical (left, top, width, height)."""
    _ensure_dpi_aware()
    return _rect(hwnd)


def window_dpi(hwnd: int) -> int:
    """Return the target window's DPI, falling back to 96."""
    _ensure_dpi_aware()
    if not _IS_WINDOWS:
        return 96
    try:
        user32, _ = _api()
        dpi = int(user32.GetDpiForWindow(int(hwnd)))
        return dpi if dpi > 0 else 96
    except Exception:  # noqa: BLE001
        return 96


def window_pid(hwnd: int) -> int:
    """Return the owning process id, or 0 when unavailable."""
    if not _IS_WINDOWS:
        return 0
    try:
        user32, _ = _api()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:  # noqa: BLE001
        return 0


def window_title(hwnd: int) -> str:
    """Read-only title for privacy checks; never mutates the window."""
    if not _IS_WINDOWS:
        return ""
    _ensure_dpi_aware()
    user32, _ = _api()
    hwnd = int(hwnd or 0)
    if hwnd <= 0 or not user32.IsWindow(hwnd):
        return ""
    try:
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value.strip()
    except Exception:  # noqa: BLE001
        return ""


def window_info(hwnd: int) -> dict:
    """Small diagnostic helper; no capture side effects."""
    _ensure_dpi_aware()
    user32, _ = _api()
    hwnd = int(hwnd or 0)
    if hwnd <= 0 or not user32.IsWindow(hwnd):
        return {"hwnd": hwnd, "exists": False}
    left, top, width, height = _rect(hwnd)
    return {
        "hwnd": hwnd,
        "exists": True,
        "minimized": bool(user32.IsIconic(hwnd)),
        "rect": {"x": left, "y": top, "width": width, "height": height},
        "occluded": _is_occluded(hwnd, left, top, width, height),
        "captured_at": int(time.time() * 1000),
    }


__all__ = [
    "CaptureImage",
    "Quality",
    "WindowCaptureError",
    "analyze_capture",
    "capture_window",
    "ensure_dpi_awareness",
    "window_dpi",
    "window_info",
    "window_pid",
    "window_rect",
    "window_title",
]
