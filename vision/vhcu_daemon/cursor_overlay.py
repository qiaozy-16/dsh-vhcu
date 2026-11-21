"""Windows virtual-cursor overlay: small, per-pixel transparent, click-through.

Complete replacement for the old full-screen colour-key overlay. It uses a
96 DIP layered window around the cursor and UpdateLayeredWindow with a 32bpp
ARGB DIB. No GDI+ text, no background, no taskbar-sized surface.

Rendering:
  * GDI+ vector rounded triangle (default, copyright-safe);
  * optional assets/cursor.png tinted per action with GDI+ ImageAttributes.

Actions:
  move  -> cyan halo
  click -> red + ripple
  drag  -> green halo

The overlay is fail-open: any exception disables drawing and is reported
through ``last_error``, but never escapes into the daemon command loop.
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
from pathlib import Path
from typing import Callable

IS_WINDOWS = os.name == "nt"

WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_NCHITTEST = 0x0084
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
HTTRANSPARENT = -1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
WINDOW_DIP = 96
CURSOR_DIP = 24
RIPPLE_SECONDS = 0.18
# P9: a pure-white rim keeps the cursor visible on same-colour targets.
OUTLINE_DIP = 1.5
OUTLINE_ARGB = 0xFFFFFFFF
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
DIB_RGB_COLORS = 0
BI_RGB = 0
UNIT_PIXEL = 2
SMOOTHING_ANTIALIAS = 4
INTERPOLATION_HIGH = 7
PIXEL_OFFSET_HALF = 2
PIXEL_FORMAT_32BPP_ARGB = 0x0026200A
GDIPLUS_COLOR_ADJUST_BITMAP = 1
GDIPLUS_COLOR_MATRIX_FLAGS_DEFAULT = 0

ACTION_COLORS = {
    "move": 0xFF00E5FF,
    "click": 0xFFFF3B30,
    "drag": 0xFF34C759,
    "hide": 0x00000000,
}
ACTION_RGB = {
    "move": (0x00, 0xE5, 0xFF),
    "click": (0xFF, 0x3B, 0x30),
    "drag": (0x34, 0xC7, 0x59),
}

if IS_WINDOWS:
    from ctypes import wintypes

    LRESULT = ctypes.c_ssize_t
    ULONG_PTR = ctypes.c_size_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.UINT),
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
            ("hIconSm", wintypes.HICON),
        ]

    class GdiplusStartupInput(ctypes.Structure):
        _fields_ = [
            ("GdiplusVersion", wintypes.UINT),
            ("DebugEventCallback", ctypes.c_void_p),
            ("SuppressBackgroundThread", wintypes.BOOL),
            ("SuppressExternalCodecs", wintypes.BOOL),
        ]

    class ColorMatrix(ctypes.Structure):
        _fields_ = [("m", ctypes.c_float * 25)]

    class BITMAPINFOHEADER(ctypes.Structure):
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

    class RGBQUAD(ctypes.Structure):
        _fields_ = [
            ("rgbBlue", wintypes.BYTE),
            ("rgbGreen", wintypes.BYTE),
            ("rgbRed", wintypes.BYTE),
            ("rgbReserved", wintypes.BYTE),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [
            ("BlendOp", wintypes.BYTE),
            ("BlendFlags", wintypes.BYTE),
            ("SourceConstantAlpha", wintypes.BYTE),
            ("AlphaFormat", wintypes.BYTE),
        ]


def _rgb_argb(rgb: tuple[int, int, int], alpha: int = 255) -> int:
    return ((alpha & 0xFF) << 24) | ((rgb[0] & 0xFF) << 16) | ((rgb[1] & 0xFF) << 8) | (rgb[2] & 0xFF)


class _GdiPlus:
    """Small flat-API GDI+ wrapper owned by one overlay thread."""

    def __init__(self) -> None:
        self.lib = ctypes.windll.gdiplus
        self.token = ULONG_PTR(0)
        self._started = False
        self._set_prototypes()

    def _set_prototypes(self) -> None:
        lib = self.lib
        lib.GdiplusStartup.argtypes = [
            ctypes.POINTER(ULONG_PTR), ctypes.POINTER(GdiplusStartupInput), ctypes.c_void_p,
        ]
        lib.GdiplusStartup.restype = ctypes.c_int
        lib.GdiplusShutdown.argtypes = [ULONG_PTR]
        lib.GdipCreateBitmapFromScan0.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.GdipGetImageGraphicsContext.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.GdipGraphicsClear.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.GdipSetSmoothingMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.GdipSetInterpolationMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.GdipSetPixelOffsetMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.GdipFlush.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.GdipCreatePath.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        lib.GdipAddPathBezier.argtypes = [
            ctypes.c_void_p,
            ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float,
            ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float,
        ]
        lib.GdipAddPathLine.argtypes = [
            ctypes.c_void_p, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float,
        ]
        lib.GdipClosePathFigure.argtypes = [ctypes.c_void_p]
        lib.GdipCreateSolidFill.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        lib.GdipFillPath.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.GdipFillEllipse.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float,
        ]
        lib.GdipCreatePen1.argtypes = [
            ctypes.c_uint32, ctypes.c_float, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.GdipDrawEllipse.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float,
        ]
        lib.GdipDrawPath.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.GdipDeletePen.argtypes = [ctypes.c_void_p]
        lib.GdipDeleteBrush.argtypes = [ctypes.c_void_p]
        lib.GdipDeletePath.argtypes = [ctypes.c_void_p]
        lib.GdipDeleteGraphics.argtypes = [ctypes.c_void_p]
        lib.GdipDisposeImage.argtypes = [ctypes.c_void_p]
        lib.GdipCreateBitmapFromFile.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.GdipGetImageWidth.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        lib.GdipGetImageHeight.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        lib.GdipCreateImageAttributes.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.GdipSetImageAttributesColorMatrix.argtypes = [
            ctypes.c_void_p, ctypes.c_int, wintypes.BOOL,
            ctypes.POINTER(ColorMatrix), ctypes.c_void_p, ctypes.c_int,
        ]
        lib.GdipDrawImageRectRectI.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        lib.GdipDisposeImageAttributes.argtypes = [ctypes.c_void_p]

    def start(self) -> bool:
        if self._started:
            return True
        info = GdiplusStartupInput(1, None, 0, 0)
        status = int(self.lib.GdiplusStartup(
            ctypes.byref(self.token), ctypes.byref(info), None,
        ))
        self._started = status == 0
        return self._started

    def stop(self) -> None:
        if self._started:
            try:
                self.lib.GdiplusShutdown(self.token)
            except Exception:  # noqa: BLE001
                pass
            self._started = False


class CursorOverlay:
    """Thread-owned, bounded, fail-open virtual cursor overlay."""

    def __init__(self, on_error: Callable[[str], None] | None = None) -> None:
        self._commands: "queue.Queue[tuple]" = queue.Queue(maxsize=8)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._hwnd = 0
        self._vx = 0
        self._vy = 0
        self._vw = 0
        self._vh = 0
        self._win_px = WINDOW_DIP
        self._scale = 1.0
        self._dpi = 96
        self._x = 0
        self._y = 0
        self._origin_x = 0
        self._origin_y = 0
        self._action = "move"
        self._visible = False
        self._last_update = 0.0
        self._ripple_start = 0.0
        self._last_error: str | None = None
        self._on_error = on_error
        self._disabled = False
        # P1 8.8: capture_guard() needs a synchronous hide, so the overlay
        # thread acknowledges the hide command through this event.
        self._hide_ack = threading.Event()
        self._hide_was_visible = False
        self._hidden_x = 0
        self._hidden_y = 0
        self._hidden_action = "move"
        self._gp: _GdiPlus | None = None
        self._bmp = None
        self._graphics = None
        self._memdc = None
        self._dib = None
        self._old_dib = None
        self._bits = None
        self._bits_array = None
        self._width = 0
        self._height = 0
        self._image = None
        self._image_w = 0
        self._image_h = 0
        self._image_attrs: dict[str, int] = {}
        self._brush_cache: dict[int, ctypes.c_void_p] = {}

    @property
    def available(self) -> bool:
        return IS_WINDOWS

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _report(self, message: str) -> None:
        if self._last_error is None:
            self._last_error = str(message)
        callback = self._on_error
        if callback:
            try:
                callback(str(message))
            except Exception:  # noqa: BLE001 - reporting must never throw
                pass

    def _ensure(self) -> None:
        if not IS_WINDOWS:
            return
        with self._lock:
            if self._disabled:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run, name="vhcu-cursor-overlay", daemon=True,
            )
            self._thread.start()
        self._ready.wait(timeout=1.0)

    def _enqueue(self, command: tuple) -> None:
        try:
            self._commands.put_nowait(command)
        except queue.Full:
            try:
                self._commands.get_nowait()
            except queue.Empty:
                pass
            try:
                self._commands.put_nowait(command)
            except queue.Full:
                pass

    def show(self, x: int, y: int, action: str = "move") -> None:
        if not IS_WINDOWS:
            return
        try:
            self._ensure()
            self._enqueue(("show", int(x), int(y), str(action or "move")))
        except BaseException as error:  # noqa: BLE001 - overlay is fail-open
            self._report(f"overlay.show failed: {error}")

    def hide(self) -> None:
        if not IS_WINDOWS:
            return
        try:
            self._ensure()
            self._enqueue(("hide", 0, 0, "hide"))
        except BaseException as error:  # noqa: BLE001
            self._report(f"overlay.hide failed: {error}")

    def hide_sync(self, timeout: float = 0.3) -> bool:
        """Hide and wait until the overlay thread has really hidden the window.

        P1 8.8: capture_guard() needs the pixels to be free of the cursor before
        it calls mss/win_capture. Returns True when nothing was on screen or the
        hide landed; False only when the acknowledgement timed out.
        """
        if not IS_WINDOWS:
            return True
        try:
            if not self._visible or not self._hwnd:
                return True
            self._hide_was_visible = True
            self._hidden_x = self._x
            self._hidden_y = self._y
            self._hidden_action = self._action
            self._hide_ack.clear()
            self._enqueue(("hide", 0, 0, "hide"))
            return self._hide_ack.wait(timeout)
        except BaseException as error:  # noqa: BLE001 - overlay is fail-open
            self._report(f"overlay.hide_sync failed: {error}")
            return False

    def restore_after_capture(self) -> None:
        """Re-show the overlay at its last position if it was visible before."""
        if not self._hide_was_visible:
            return
        self._hide_was_visible = False
        self.show(self._hidden_x, self._hidden_y, self._hidden_action)

    def close(self) -> None:
        if not IS_WINDOWS:
            return
        try:
            self._stop.set()
            self._enqueue(("stop", 0, 0, "stop"))
            hwnd = self._hwnd
            if hwnd:
                try:
                    ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                except Exception:  # noqa: BLE001
                    pass
            thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.5)
        except BaseException as error:  # noqa: BLE001
            self._report(f"overlay.close failed: {error}")

    def _run(self) -> None:
        try:
            self._run_inner()
        except BaseException as error:  # noqa: BLE001 - never kill daemon
            self._disabled = True
            self._report(f"overlay thread stopped: {error}")
        finally:
            self._cleanup()

    def _run_inner(self) -> None:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        kernel32 = ctypes.windll.kernel32
        self._set_win32_prototypes(user32, gdi32, kernel32)
        gp = _GdiPlus()
        if not gp.start():
            raise RuntimeError("GDI+ startup failed")
        self._gp = gp
        self._vx = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
        self._vy = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
        self._vw = max(1, int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)))
        self._vh = max(1, int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)))
        try:
            self._dpi = int(user32.GetDpiForSystem()) or 96
        except Exception:  # noqa: BLE001
            self._dpi = 96
        self._scale = max(0.75, min(3.0, self._dpi / 96.0))
        self._win_px = max(48, int(round(WINDOW_DIP * self._scale)))
        self._width = self._win_px
        self._height = self._win_px

        hinst = kernel32.GetModuleHandleW(None)
        class_name = "VHCU_CursorOverlay_V2"
        wndproc = WNDPROC(self._wndproc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = wndproc
        wc.hInstance = hinst
        wc.hCursor = user32.LoadCursorW(None, 32512)
        wc.lpszClassName = class_name
        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            raise RuntimeError("RegisterClassExW failed")
        exstyle = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
                   | WS_EX_TOPMOST | WS_EX_NOACTIVATE)
        hwnd = user32.CreateWindowExW(
            exstyle, class_name, "VHCU virtual cursor", 0,
            0, 0, self._win_px, self._win_px,
            None, None, hinst, None,
        )
        if not hwnd:
            user32.UnregisterClassW(class_name, hinst)
            raise RuntimeError("CreateWindowExW failed")

        self._hwnd = int(hwnd)
        self._create_dib(user32, gdi32, self._win_px, self._win_px)
        self._load_png(gp)
        self._ready.set()

        try:
            while not self._stop.is_set():
                self._drain_commands()
                self._animate_if_needed()
                # Persistent while vision is running. Hiding is explicit:
                # vision.stop, virtual fallback failure, or overlay.close.
                msg = wintypes.MSG()
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    if msg.message == 0x0012:
                        return
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.004)
        finally:
            try:
                if self._hwnd:
                    user32.DestroyWindow(self._hwnd)
                    self._hwnd = 0
            except Exception:  # noqa: BLE001
                pass
            try:
                user32.UnregisterClassW(class_name, hinst)
            except Exception:  # noqa: BLE001
                pass
            self._dispose_dib(gdi32)

    def _set_win32_prototypes(self, user32, gdi32, kernel32) -> None:
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]
        user32.RegisterClassExW.restype = ctypes.c_ushort
        user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.LoadCursorW.restype = wintypes.HANDLE
        user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = LRESULT
        user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
            ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
            wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
        ]
        user32.UpdateLayeredWindow.restype = wintypes.BOOL
        user32.GetDC.restype = wintypes.HDC
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        try:
            user32.GetDpiForSystem.argtypes = []
            user32.GetDpiForSystem.restype = wintypes.UINT
        except AttributeError:
            pass
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
        ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

    def _create_dib(self, user32, gdi32, width: int, height: int) -> None:
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        screen = user32.GetDC(None)
        try:
            self._memdc = gdi32.CreateCompatibleDC(screen)
        finally:
            user32.ReleaseDC(None, screen)
        bits = ctypes.c_void_p()
        self._dib = gdi32.CreateDIBSection(
            self._memdc, ctypes.byref(bmi), DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0,
        )
        if not self._memdc or not self._dib or not bits:
            raise RuntimeError("CreateDIBSection failed")
        self._bits = bits
        self._bits_array = (ctypes.c_ubyte * (width * height * 4)).from_address(bits.value)
        self._old_dib = gdi32.SelectObject(self._memdc, self._dib)
        gp = self._gp
        assert gp is not None
        bmp = ctypes.c_void_p()
        status = gp.lib.GdipCreateBitmapFromScan0(
            width, height, width * 4, PIXEL_FORMAT_32BPP_ARGB,
            bits, ctypes.byref(bmp),
        )
        if status != 0 or not bmp:
            raise RuntimeError(f"GdipCreateBitmapFromScan0 failed: {status}")
        self._bmp = bmp
        graphics = ctypes.c_void_p()
        status = gp.lib.GdipGetImageGraphicsContext(
            self._bmp, ctypes.byref(graphics),
        )
        if status != 0 or not graphics:
            raise RuntimeError(f"GdipGetImageGraphicsContext failed: {status}")
        self._graphics = graphics
        gp.lib.GdipSetSmoothingMode(self._graphics, SMOOTHING_ANTIALIAS)
        gp.lib.GdipSetInterpolationMode(self._graphics, INTERPOLATION_HIGH)
        gp.lib.GdipSetPixelOffsetMode(self._graphics, PIXEL_OFFSET_HALF)

    def _dispose_dib(self, gdi32) -> None:
        gp = self._gp
        try:
            if self._graphics and gp:
                gp.lib.GdipDeleteGraphics(self._graphics)
            if self._bmp and gp:
                gp.lib.GdipDisposeImage(self._bmp)
        except Exception:  # noqa: BLE001
            pass
        self._graphics = None
        self._bmp = None
        try:
            if self._memdc and self._old_dib:
                gdi32.SelectObject(self._memdc, self._old_dib)
            if self._dib:
                gdi32.DeleteObject(self._dib)
            if self._memdc:
                gdi32.DeleteDC(self._memdc)
        except Exception:  # noqa: BLE001
            pass
        self._memdc = None
        self._dib = None
        self._bits = None
        self._bits_array = None
        try:
            for attrs in self._image_attrs.values():
                if gp:
                    gp.lib.GdipDisposeImageAttributes(attrs)
            self._image_attrs.clear()
            if self._image and gp:
                gp.lib.GdipDisposeImage(self._image)
        except Exception:  # noqa: BLE001
            pass
        self._image = None
        if gp:
            gp.stop()
            self._gp = None

    def _load_png(self, gp: _GdiPlus) -> None:
        asset = Path(__file__).resolve().parents[2] / "assets" / "cursor.png"
        if not asset.is_file():
            return
        image = ctypes.c_void_p()
        status = gp.lib.GdipCreateBitmapFromFile(str(asset), ctypes.byref(image))
        if status != 0 or not image:
            return
        width = ctypes.c_uint(0)
        height = ctypes.c_uint(0)
        gp.lib.GdipGetImageWidth(image, ctypes.byref(width))
        gp.lib.GdipGetImageHeight(image, ctypes.byref(height))
        if not width.value or not height.value:
            gp.lib.GdipDisposeImage(image)
            return
        self._image = image
        self._image_w = int(width.value)
        self._image_h = int(height.value)
        for action, rgb in ACTION_RGB.items():
            attrs = ctypes.c_void_p()
            if gp.lib.GdipCreateImageAttributes(ctypes.byref(attrs)) != 0 or not attrs:
                continue
            matrix = ColorMatrix()
            matrix.m[0] = 0.0
            matrix.m[6] = 0.0
            matrix.m[12] = 0.0
            matrix.m[18] = 1.0
            matrix.m[4] = rgb[0] / 255.0
            matrix.m[9] = rgb[1] / 255.0
            matrix.m[14] = rgb[2] / 255.0
            matrix.m[24] = 1.0
            status = gp.lib.GdipSetImageAttributesColorMatrix(
                attrs, GDIPLUS_COLOR_ADJUST_BITMAP, True,
                ctypes.byref(matrix), None, GDIPLUS_COLOR_MATRIX_FLAGS_DEFAULT,
            )
            if status != 0:
                gp.lib.GdipDisposeImageAttributes(attrs)
                continue
            self._image_attrs[action] = attrs

    def _drain_commands(self) -> None:
        while True:
            try:
                command, x, y, action = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                if command == "stop":
                    self._stop.set()
                    return
                if command == "hide":
                    self._visible = False
                    try:
                        if self._hwnd:
                            ctypes.windll.user32.ShowWindow(self._hwnd, SW_HIDE)
                    finally:
                        # P1 8.8: only now may the capturing thread grab pixels.
                        self._hide_ack.set()
                    continue
                if command == "show":
                    self._apply_show(x, y, action)
            except BaseException as error:  # noqa: BLE001 - one bad frame only
                self._report(f"overlay command failed: {error}")

    def _apply_show(self, x: int, y: int, action: str) -> None:
        if action not in ACTION_COLORS:
            action = "move"
        self._x = max(self._vx, min(self._vx + self._vw - 1, int(x)))
        self._y = max(self._vy, min(self._vy + self._vh - 1, int(y)))
        self._action = action
        self._last_update = time.monotonic()
        if action == "click":
            self._ripple_start = self._last_update
        else:
            self._ripple_start = 0.0
        # Centre the 96 DIP window on the target so halo/ripple never clip
        # at the window edge (clipping produced a rectangular dark patch in
        # the first UpdateLayeredWindow implementation).
        hotspot = self._win_px // 2
        ox = self._x - hotspot
        oy = self._y - hotspot
        ox = max(self._vx, min(self._vx + self._vw - self._win_px, ox))
        oy = max(self._vy, min(self._vy + self._vh - self._win_px, oy))
        self._origin_x, self._origin_y = ox, oy
        self._visible = True
        user32 = ctypes.windll.user32
        user32.SetWindowPos(
            self._hwnd, HWND_TOPMOST, ox, oy, self._win_px, self._win_px,
            SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
        self._render()

    def _animate_if_needed(self) -> None:
        if not self._visible or not self._ripple_start:
            return
        elapsed = time.monotonic() - self._ripple_start
        if elapsed > RIPPLE_SECONDS:
            self._ripple_start = 0.0
        self._render()

    def _render(self) -> None:
        gp = self._gp
        if not gp or not self._graphics or not self._hwnd:
            return
        try:
            if self._bits:
                ctypes.memset(self._bits, 0, self._width * self._height * 4)
            gp.lib.GdipGraphicsClear(self._graphics, 0x00000000)
            tip_x = float(self._x - self._origin_x)
            tip_y = float(self._y - self._origin_y)
            color = ACTION_COLORS.get(self._action, ACTION_COLORS["move"])
            rgb = ACTION_RGB.get(self._action, ACTION_RGB["move"])
            self._draw_glow(gp, tip_x, tip_y, rgb)
            if self._image and self._image_attrs.get(self._action):
                size = int(round(CURSOR_DIP * self._scale))
                # The asset contract is: its top-left pixel is the tip.
                dst_x = int(round(tip_x))
                dst_y = int(round(tip_y))
                attrs = self._image_attrs[self._action]
                gp.lib.GdipDrawImageRectRectI(
                    self._graphics, self._image,
                    dst_x, dst_y, size, size,
                    0, 0, self._image_w, self._image_h,
                    UNIT_PIXEL, attrs, None, None,
                )
            else:
                self._draw_vector_cursor(gp, tip_x, tip_y, color)
            if self._action == "click" and self._ripple_start:
                self._draw_ripple(gp, tip_x, tip_y, rgb)
            gp.lib.GdipFlush(self._graphics, 1)
            self._premultiply()
            self._update_layered()
        except BaseException as error:  # noqa: BLE001 - never kill thread
            self._report(f"overlay render failed: {error}")

    def _triangle_points(self, tip_x: float, tip_y: float, factor: float = 1.0):
        scale = self._scale * factor
        return [
            (tip_x, tip_y),
            (tip_x + 2.0 * scale, tip_y + 19.0 * scale),
            (tip_x + 15.0 * scale, tip_y + 12.0 * scale),
        ]

    def _brush(self, gp: _GdiPlus, color: int):
        cached = self._brush_cache.get(color)
        if cached:
            return cached
        brush = ctypes.c_void_p()
        if gp.lib.GdipCreateSolidFill(color, ctypes.byref(brush)) != 0 or not brush:
            return None
        self._brush_cache[color] = brush
        return brush

    def _fill_rounded(self, gp: _GdiPlus, points, color: int, radius: float,
                      outline: bool = False) -> None:
        path = ctypes.c_void_p()
        if gp.lib.GdipCreatePath(1, ctypes.byref(path)) != 0 or not path:
            return
        try:
            self._add_rounded_polygon(gp, path, points, radius)
            brush = self._brush(gp, color)
            if brush:
                gp.lib.GdipFillPath(self._graphics, brush, path)
            if outline:
                self._stroke_path(gp, path, OUTLINE_ARGB, OUTLINE_DIP * self._scale)
        finally:
            gp.lib.GdipDeletePath(path)

    def _stroke_path(self, gp: _GdiPlus, path, color: int, width: float) -> None:
        pen = ctypes.c_void_p()
        if gp.lib.GdipCreatePen1(color, width, UNIT_PIXEL, ctypes.byref(pen)) != 0 or not pen:
            return
        try:
            gp.lib.GdipDrawPath(self._graphics, pen, path)
        finally:
            gp.lib.GdipDeletePen(pen)

    def _stroke_ellipse(self, gp: _GdiPlus, center_x: float, center_y: float,
                        radius: float, color: int, width: float) -> None:
        pen = ctypes.c_void_p()
        if gp.lib.GdipCreatePen1(color, width, UNIT_PIXEL, ctypes.byref(pen)) != 0 or not pen:
            return
        try:
            gp.lib.GdipDrawEllipse(
                self._graphics, pen,
                center_x - radius, center_y - radius, radius * 2, radius * 2,
            )
        finally:
            gp.lib.GdipDeletePen(pen)

    def _draw_glow(self, gp: _GdiPlus, tip_x: float, tip_y: float,
                   rgb: tuple[int, int, int]) -> None:
        # A shape-following halo, not an ellipse/background patch. The glow
        # deliberately has no white outline: stroking the enlarged triangle
        # produced a stray white line under the cursor. Only the solid cursor
        # itself keeps the white rim.
        for factor, alpha in ((1.7, 18), (1.3, 34)):
            points = self._triangle_points(tip_x, tip_y, factor)
            self._fill_rounded(gp, points, _rgb_argb(rgb, alpha), 4.0 * self._scale)

    def _draw_vector_cursor(self, gp: _GdiPlus, tip_x: float, tip_y: float,
                            color: int) -> None:
        # Keep the tip vertex sharp and exactly at (tip_x, tip_y); round only
        # the two base corners. This guarantees the visual tip and the injected
        # coordinate share the same point.
        scale = self._scale
        points = self._triangle_points(tip_x, tip_y)
        path = ctypes.c_void_p()
        if gp.lib.GdipCreatePath(1, ctypes.byref(path)) != 0 or not path:
            return
        try:
            r = 3.0 * scale
            p0, p1, p2 = points
            # Starts/ends of the rounded base corners.
            def corner_start(vertex, other):
                dx, dy = other[0] - vertex[0], other[1] - vertex[1]
                length = max(1e-6, (dx * dx + dy * dy) ** 0.5)
                return (vertex[0] + dx / length * min(r, length * 0.4),
                        vertex[1] + dy / length * min(r, length * 0.4))

            s1 = corner_start(p1, p0)
            e1 = corner_start(p1, p2)
            s2 = corner_start(p2, p1)
            e2 = corner_start(p2, p0)
            gp.lib.GdipAddPathLine(path, p0[0], p0[1], s1[0], s1[1])
            gp.lib.GdipAddPathBezier(
                path, s1[0], s1[1],
                p1[0], p1[1], p1[0], p1[1],
                e1[0], e1[1],
            )
            gp.lib.GdipAddPathLine(path, e1[0], e1[1], s2[0], s2[1])
            gp.lib.GdipAddPathBezier(
                path, s2[0], s2[1],
                p2[0], p2[1], p2[0], p2[1],
                e2[0], e2[1],
            )
            gp.lib.GdipAddPathLine(path, e2[0], e2[1], p0[0], p0[1])
            gp.lib.GdipClosePathFigure(path)
            brush = self._brush(gp, color)
            if brush:
                gp.lib.GdipFillPath(self._graphics, brush, path)
            self._stroke_path(gp, path, OUTLINE_ARGB, OUTLINE_DIP * scale)
        finally:
            gp.lib.GdipDeletePath(path)

    @staticmethod
    def _add_rounded_polygon(gp: _GdiPlus, path, points, radius: float) -> None:
        count = len(points)
        starts = []
        for index in range(count):
            prev = points[(index - 1) % count]
            cur = points[index]
            nxt = points[(index + 1) % count]
            v1x, v1y = prev[0] - cur[0], prev[1] - cur[1]
            v2x, v2y = nxt[0] - cur[0], nxt[1] - cur[1]
            l1 = max(1e-6, (v1x * v1x + v1y * v1y) ** 0.5)
            l2 = max(1e-6, (v2x * v2x + v2y * v2y) ** 0.5)
            r = min(radius, l1 * 0.45, l2 * 0.45)
            starts.append((
                cur[0] + v1x / l1 * r, cur[1] + v1y / l1 * r,
                cur[0] + v2x / l2 * r, cur[1] + v2y / l2 * r,
            ))
        first = starts[0]
        gp.lib.GdipAddPathBezier(
            path,
            first[0], first[1],
            first[0] + (points[0][0] - first[0]) * 0.55,
            first[1] + (points[0][1] - first[1]) * 0.55,
            first[2] + (points[0][0] - first[2]) * 0.55,
            first[3] + (points[0][1] - first[3]) * 0.55,
            first[2], first[3],
        )
        for index in range(1, count):
            start = starts[index - 1]
            end = starts[index]
            vertex = points[index]
            gp.lib.GdipAddPathBezier(
                path,
                start[2], start[3],
                start[2] + (vertex[0] - start[2]) * 0.55,
                start[3] + (vertex[1] - start[3]) * 0.55,
                end[0] + (vertex[0] - end[0]) * 0.55,
                end[1] + (vertex[1] - end[1]) * 0.55,
                end[0], end[1],
            )
        gp.lib.GdipClosePathFigure(path)

    def _draw_ripple(self, gp: _GdiPlus, tip_x: float, tip_y: float,
                     rgb: tuple[int, int, int]) -> None:
        elapsed = max(0.0, min(RIPPLE_SECONDS, time.monotonic() - self._ripple_start))
        progress = elapsed / RIPPLE_SECONDS
        for index, base in enumerate((14.0, 22.0)):
            alpha = int(150 * (1.0 - progress) * (1.0 - index * 0.35))
            if alpha <= 0:
                continue
            pen = ctypes.c_void_p()
            color = _rgb_argb(rgb, alpha)
            if gp.lib.GdipCreatePen1(color, 2.0 * self._scale, UNIT_PIXEL, ctypes.byref(pen)) != 0 or not pen:
                continue
            try:
                radius = (base + progress * 24.0) * self._scale
                gp.lib.GdipDrawEllipse(
                    self._graphics, pen,
                    tip_x + 2 * self._scale - radius,
                    tip_y + 4 * self._scale - radius,
                    radius * 2, radius * 2,
                )
            finally:
                gp.lib.GdipDeletePen(pen)
            # A pure-white outer rim makes the ripple visible even when the
            # action colour matches the target pixels.
            self._stroke_ellipse(
                gp, tip_x + 2 * self._scale, tip_y + 4 * self._scale,
                radius + OUTLINE_DIP * 0.5 * self._scale,
                OUTLINE_ARGB, OUTLINE_DIP * self._scale,
            )

    def _premultiply(self) -> None:
        if not self._bits_array:
            return
        data = self._bits_array
        for index in range(0, len(data), 4):
            alpha = data[index + 3]
            if alpha == 0:
                data[index] = 0
                data[index + 1] = 0
                data[index + 2] = 0
            elif alpha != 255:
                data[index] = (data[index] * alpha) // 255
                data[index + 1] = (data[index + 1] * alpha) // 255
                data[index + 2] = (data[index + 2] * alpha) // 255

    def _update_layered(self) -> None:
        user32 = ctypes.windll.user32
        screen = user32.GetDC(None)
        try:
            point_dst = wintypes.POINT(self._origin_x, self._origin_y)
            size = wintypes.SIZE(self._win_px, self._win_px)
            point_src = wintypes.POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            ok = user32.UpdateLayeredWindow(
                self._hwnd, screen, ctypes.byref(point_dst), ctypes.byref(size),
                self._memdc, ctypes.byref(point_src), 0,
                ctypes.byref(blend), ULW_ALPHA,
            )
            if not ok:
                raise RuntimeError("UpdateLayeredWindow failed")
        finally:
            user32.ReleaseDC(None, screen)

    def _cleanup(self) -> None:
        self._ready.set()
        gp = self._gp
        if gp is not None:
            try:
                for brush in self._brush_cache.values():
                    gp.lib.GdipDeleteBrush(brush)
                self._brush_cache.clear()
                for attrs in self._image_attrs.values():
                    gp.lib.GdipDisposeImageAttributes(attrs)
                self._image_attrs.clear()
                if self._image:
                    gp.lib.GdipDisposeImage(self._image)
                    self._image = None
                gp.stop()
            except BaseException:  # noqa: BLE001
                pass
        self._gp = None
        try:
            self._dispose_dib(ctypes.windll.gdi32)
        except BaseException:  # noqa: BLE001
            pass

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            user32 = ctypes.windll.user32
            if msg == WM_NCHITTEST:
                return HTTRANSPARENT
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_PAINT:
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        except BaseException as error:  # noqa: BLE001
            self._report(f"overlay wndproc failed: {error}")
            return 0


__all__ = ["ACTION_COLORS", "ACTION_RGB", "CursorOverlay", "IS_WINDOWS"]
