"""Virtual-cursor coordinate mapping and Windows PostMessage injection.

This module is deliberately separate from InputController:
  - pure coordinate/message helpers are testable without Windows;
  - the Win32 PostMessage path never imports or calls SendInput;
  - physical fallback remains the caller's decision (hybrid strategy).

Important: PostMessage has no application-level acknowledgement. We only treat
"window missing/hung", "point outside client area", "DPI unsupported" and a
failed PostMessage call as failures. Once the first message is queued we never
replay the click physically, which prevents double-click side effects.
"""

from __future__ import annotations

import ctypes
import functools
import os
import time
from typing import Any

if os.name == "nt":
    from ctypes import wintypes

IS_WINDOWS = os.name == "nt"

E_VIRTUAL_CURSOR_UNSUPPORTED = "E_VIRTUAL_CURSOR_UNSUPPORTED"
E_VIRTUAL_CURSOR_FAILED = "E_VIRTUAL_CURSOR_FAILED"

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WHEEL_DELTA = 120
WM_NULL = 0x0000
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010
SMTO_ABORTIFHUNG = 0x0002
SMTO_BLOCK = 0x0001
GA_ROOT = 2
DPI_AWARENESS_INVALID = -1
DPI_AWARENESS_UNAWARE = 0
DPI_AWARENESS_SYSTEM = 1
DPI_AWARENESS_PER_MONITOR = 2
SHELL_WINDOW_CLASSES = frozenset({
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Progman",
    "WorkerW",
    # Classic Win32 modal dialogs (Save/confirm dialogs) routinely ignore
    # background PostMessage input.
    "#32770",
})
SYSTEM_PROCESS_NAMES = frozenset({
    "explorer.exe",
    # Windows 10/11 shell UI hosts that ignore synthetic background input.
    "startmenuexperiencehost.exe",
    "searchexperiencehost.exe",
    "searchhost.exe",
    "shellexperiencehost.exe",
})
# P1 隐患 7 (relaxed in V1.1): Chromium/Electron main windows DO apply posted
# mouse messages through their child surfaces in real use, so Chrome_WidgetWin_*
# is no longer rejected here. CEF/UWP shells remain unverified and stay blocked.
UNSAFE_POSTMESSAGE_CLASSES = frozenset({
    "CefBrowserWindow",
    "ApplicationFrameWindow",
    "Windows.UI.Core.CoreWindow",
})
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400


_USER32 = None


def _user32():
    """Return user32 with 64-bit-safe prototypes for every call we make."""
    global _USER32
    if _USER32 is not None:
        return _USER32
    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetForegroundWindow.argtypes = []
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.ScreenToClient.restype = wintypes.BOOL
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
    try:
        user32.GetWindowDpiAwarenessContext.argtypes = [wintypes.HWND]
        user32.GetWindowDpiAwarenessContext.restype = wintypes.HANDLE
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [wintypes.HANDLE]
        user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
    except AttributeError:
        pass
    _USER32 = user32
    return user32


def _fail_open(function):
    """Unexpected Win32/ctypes failures become typed cursor errors, never a crash."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except VirtualCursorFailure:
            raise
        except BaseException as error:  # noqa: BLE001
            raise VirtualCursorFailure(f"virtual cursor failure: {error}") from error
    return wrapped


def is_shell_window_class(name: str) -> bool:
    """Taskbar/desktop windows ignore synthetic mouse messages by design."""
    return str(name or "").strip() in SHELL_WINDOW_CLASSES


def is_system_process_name(name: str) -> bool:
    """Explorer and shell UI hosts are not valid PostMessage targets."""
    return str(name or "").strip().lower() in SYSTEM_PROCESS_NAMES


def is_postmessage_unsupported_class(name: str) -> bool:
    """Chromium/Electron/UWP windows enqueue but never apply posted mouse input."""
    return str(name or "").strip() in UNSAFE_POSTMESSAGE_CLASSES


class VirtualCursorFailure(RuntimeError):
    code = E_VIRTUAL_CURSOR_FAILED

    def __init__(self, message: str, hint: str = "", sent: int = 0):
        super().__init__(message)
        self.hint = hint
        self.sent = int(sent)


class VirtualCursorUnsupported(VirtualCursorFailure):
    code = E_VIRTUAL_CURSOR_UNSUPPORTED


def map_client_coordinates(client_x: int, client_y: int, dpi: int,
                           awareness: str) -> tuple[int, int]:
    """Map physical client pixels into the target message coordinate space.

    Per-monitor aware targets consume physical client coordinates. System-aware
    and unaware targets expect logical client pixels (96 DPI baseline), so the
    physical client point is scaled by 96/dpi.
    """
    x, y = int(client_x), int(client_y)
    if awareness in ("per-monitor", "per-monitor-v2"):
        return x, y
    if awareness in ("system", "unaware"):
        scale = 96.0 / max(1.0, float(dpi))
        return int(round(x * scale)), int(round(y * scale))
    raise VirtualCursorUnsupported(
        f"unsupported target DPI awareness: {awareness}",
        "无法确定目标窗口的 DPI 坐标空间；请改用 hybrid/physical 或 UIA",
    )


def mouse_messages(action: str, x: int, y: int) -> list[tuple[int, int]]:
    """Return ordered (message, wParam) pairs for one virtual gesture."""
    if action == "click":
        return [
            (WM_MOUSEMOVE, 0),
            (WM_LBUTTONDOWN, MK_LBUTTON),
            (WM_LBUTTONUP, 0),
        ]
    if action == "double_click":
        # Two complete down/up pairs; the caller inserts the 50ms gap.
        return [
            (WM_MOUSEMOVE, 0),
            (WM_LBUTTONDOWN, MK_LBUTTON),
            (WM_LBUTTONUP, 0),
            (WM_LBUTTONDOWN, MK_LBUTTON),
            (WM_LBUTTONUP, 0),
        ]
    if action == "right_click":
        return [
            (WM_MOUSEMOVE, 0),
            (WM_RBUTTONDOWN, MK_RBUTTON),
            (WM_RBUTTONUP, 0),
        ]
    if action == "middle_click":
        return [
            (WM_MOUSEMOVE, 0),
            (WM_MBUTTONDOWN, MK_MBUTTON),
            (WM_MBUTTONUP, 0),
        ]
    raise VirtualCursorUnsupported(
        f"unsupported virtual action: {action!r}",
        "virtual post_message 只支持 click/double_click/right_click/middle_click",
    )


def _dpi_awareness(user32, hwnd: int) -> str:
    try:
        context = user32.GetWindowDpiAwarenessContext(int(hwnd))
        value = int(user32.GetAwarenessFromDpiAwarenessContext(context))
    except Exception:  # noqa: BLE001 - older Windows or missing API
        return "unknown"
    if value == DPI_AWARENESS_PER_MONITOR:
        return "per-monitor"
    if value == DPI_AWARENESS_SYSTEM:
        return "system"
    if value == DPI_AWARENESS_UNAWARE:
        return "unaware"
    return "unknown"


def _dpi_for_window(user32, hwnd: int) -> int:
    try:
        dpi = int(user32.GetDpiForWindow(int(hwnd)))
        return dpi if dpi > 0 else 96
    except Exception:  # noqa: BLE001
        return 96


@_fail_open
def window_class_name(hwnd: int) -> str:
    if not IS_WINDOWS:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    try:
        length = int(_user32().GetClassNameW(int(hwnd), buf, len(buf)))
    except Exception:  # noqa: BLE001
        return ""
    return buf.value[:length] if length > 0 else ""


def _kernel32():
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def process_image_name(hwnd: int) -> str:
    """Return the target process basename, or "" when Windows denies the query."""
    if not IS_WINDOWS:
        return ""
    user32 = _user32()
    pid = wintypes.DWORD(0)
    try:
        user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    except Exception:  # noqa: BLE001
        return ""
    if not pid.value:
        return ""
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid.value))
    if not handle:
        handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, int(pid.value))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).strip().lower()
    finally:
        kernel32.CloseHandle(handle)


def _reject_shell_window(hwnd: int) -> None:
    name = window_class_name(hwnd)
    if is_shell_window_class(name):
        raise VirtualCursorUnsupported(
            f"{name} does not accept synthetic background mouse messages",
            "系统任务栏/桌面请在 hybrid 或 physical 模式操作",
        )
    # P1 隐患 7: Chromium/Electron/UWP enqueue the message without applying it,
    # which would otherwise surface as a "successful" phantom click.
    if is_postmessage_unsupported_class(name):
        raise VirtualCursorUnsupported(
            f"{name} is a Chromium/Electron/UWP surface; posted mouse messages are not applied",
            "该窗口不支持后台 PostMessage 点击：hybrid 会降级为物理光标，virtual 请改用 physical 或 UIA Invoke",
        )
    process = process_image_name(hwnd)
    if is_system_process_name(process):
        raise VirtualCursorUnsupported(
            f"system process {process} does not accept synthetic background mouse messages",
            "explorer.exe/系统外壳 UI 请在 hybrid 或 physical 模式操作",
        )


@_fail_open
def resolve_window(explicit_hwnd: int | None, x: int, y: int) -> int:
    """Resolve explicit target > WindowFromPoint; never guess an unrelated window."""
    if explicit_hwnd is not None:
        try:
            hwnd = int(explicit_hwnd)
        except (TypeError, ValueError):
            hwnd = 0
        if hwnd > 0:
            return hwnd
    if not IS_WINDOWS:
        raise VirtualCursorUnsupported("PostMessage is Windows-only")
    user32 = _user32()
    point = wintypes.POINT(int(x), int(y))
    hwnd = int(user32.WindowFromPoint(point) or 0)
    if hwnd <= 0:
        # Decision C fallback: explicit > WindowFromPoint > active window.
        hwnd = int(user32.GetForegroundWindow() or 0)
    if hwnd <= 0:
        raise VirtualCursorUnsupported(
            "WindowFromPoint and active window are both unavailable",
            "目标点不在可接收后台鼠标消息的窗口上",
        )
    return hwnd


@_fail_open
def post_message_click(
    hwnd: int,
    action: str,
    screen_x: int,
    screen_y: int,
) -> dict:
    """Post one click sequence to a window. Never calls SendInput."""
    if not IS_WINDOWS:
        raise VirtualCursorUnsupported("PostMessage is Windows-only")
    user32 = _user32()
    if not user32.IsWindow(int(hwnd)):
        raise VirtualCursorUnsupported("target window no longer exists")
    if not user32.IsWindowVisible(int(hwnd)):
        raise VirtualCursorUnsupported("target window is not visible")
    if user32.IsIconic(int(hwnd)):
        raise VirtualCursorUnsupported("target window is minimized")
    _reject_shell_window(int(hwnd))

    # Preflight: avoid waiting forever on a hung target; this is the only
    # "timeout" PostMessage can reliably offer.
    result = ctypes.c_size_t(0)
    try:
        sent = user32.SendMessageTimeoutW(
            int(hwnd), WM_NULL, 0, 0,
            SMTO_ABORTIFHUNG | SMTO_BLOCK, 50, ctypes.byref(result),
        )
    except Exception as error:  # noqa: BLE001
        raise VirtualCursorFailure(f"preflight SendMessageTimeout failed: {error}") from error
    if not sent:
        raise VirtualCursorFailure(
            "target message queue did not answer preflight within 50ms",
            "目标软件可能无响应；hybrid 将降级为物理光标",
        )

    client = wintypes.POINT(int(screen_x), int(screen_y))
    if not user32.ScreenToClient(int(hwnd), ctypes.byref(client)):
        raise VirtualCursorUnsupported("ScreenToClient failed")
    client_rect = wintypes.RECT()
    if not user32.GetClientRect(int(hwnd), ctypes.byref(client_rect)):
        raise VirtualCursorUnsupported("GetClientRect failed")
    if not (client_rect.left <= client.x < client_rect.right and
            client_rect.top <= client.y < client_rect.bottom):
        raise VirtualCursorUnsupported(
            "target point is outside the client area",
            "虚拟注入只支持客户区；标题栏/边框请使用 physical 或 UIA",
        )

    dpi = _dpi_for_window(user32, int(hwnd))
    awareness = _dpi_awareness(user32, int(hwnd))
    msg_x, msg_y = map_client_coordinates(client.x, client.y, dpi, awareness)
    if not (-32768 <= msg_x <= 32767 and -32768 <= msg_y <= 32767):
        raise VirtualCursorUnsupported("client coordinates do not fit WM_* lParam")

    messages = mouse_messages(action, msg_x, msg_y)
    queued: list[str] = []
    sent_count = 0
    lparam = ((msg_y & 0xFFFF) << 16) | (msg_x & 0xFFFF)
    down_to_up = {
        WM_LBUTTONDOWN: WM_LBUTTONUP,
        WM_RBUTTONDOWN: WM_RBUTTONUP,
        WM_MBUTTONDOWN: WM_MBUTTONUP,
    }
    down_sent = False
    active_up = WM_LBUTTONUP
    for message, wparam in messages:
        ok = user32.PostMessageW(int(hwnd), int(message), int(wparam), int(lparam))
        if not ok:
            if down_sent:
                # Never leave the target believing the button is still held.
                try:
                    user32.PostMessageW(int(hwnd), int(active_up), 0, int(lparam))
                except Exception:  # noqa: BLE001 - best effort release
                    pass
            raise VirtualCursorFailure(
                f"PostMessage failed for message 0x{message:04X} after {sent_count} queued",
                "目标窗口拒绝消息；hybrid 仅在尚未投递时降级",
                sent=sent_count,
            )
        sent_count += 1
        queued.append(f"0x{message:04X}")
        if message in down_to_up:
            down_sent = True
            active_up = down_to_up[message]
        elif message in down_to_up.values():
            down_sent = False
        if action == "double_click" and sent_count == 3:
            time.sleep(0.05)
    return {
        "ok": True,
        "used": "post_message",
        "hwnd": int(hwnd),
        "screen": [int(screen_x), int(screen_y)],
        "client": [int(client.x), int(client.y)],
        "message_coords": [int(msg_x), int(msg_y)],
        "target_dpi": int(dpi),
        "dpi_awareness": awareness,
        "messages": queued,
        "sent": sent_count,
    }


@_fail_open
def post_message_drag(
    hwnd: int,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    button: str = "left",
    steps: int = 24,
) -> dict:
    """Post a left-button drag in screen coordinates. Never calls SendInput."""
    if not IS_WINDOWS:
        raise VirtualCursorUnsupported("PostMessage is Windows-only")
    user32 = _user32()
    if not user32.IsWindow(int(hwnd)) or not user32.IsWindowVisible(int(hwnd)):
        raise VirtualCursorUnsupported("target window is not available")
    if user32.IsIconic(int(hwnd)):
        raise VirtualCursorUnsupported("target window is minimized")
    _reject_shell_window(int(hwnd))
    result = ctypes.c_size_t(0)
    if not user32.SendMessageTimeoutW(
        int(hwnd), WM_NULL, 0, 0,
        SMTO_ABORTIFHUNG | SMTO_BLOCK, 50, ctypes.byref(result),
    ):
        raise VirtualCursorFailure("target message queue did not answer preflight within 50ms")
    points = [(int(start[0]), int(start[1])), (int(end[0]), int(end[1]))]
    mapped: list[tuple[int, int]] = []
    dpi = _dpi_for_window(user32, int(hwnd))
    awareness = _dpi_awareness(user32, int(hwnd))
    for screen_x, screen_y in points:
        client = wintypes.POINT(screen_x, screen_y)
        if not user32.ScreenToClient(int(hwnd), ctypes.byref(client)):
            raise VirtualCursorUnsupported("ScreenToClient failed")
        rect = wintypes.RECT()
        if not user32.GetClientRect(int(hwnd), ctypes.byref(rect)):
            raise VirtualCursorUnsupported("GetClientRect failed")
        if not (rect.left <= client.x < rect.right and rect.top <= client.y < rect.bottom):
            raise VirtualCursorUnsupported("drag point is outside the client area")
        mapped.append(map_client_coordinates(client.x, client.y, dpi, awareness))
    sx, sy = mapped[0]
    ex, ey = mapped[1]
    if button == "left":
        down_msg, up_msg, mask = WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON
    elif button == "right":
        down_msg, up_msg, mask = WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON
    elif button == "middle":
        down_msg, up_msg, mask = WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON
    else:
        raise VirtualCursorUnsupported(f"unsupported drag button: {button!r}")
    sequence: list[tuple[int, int, int]] = [
        (WM_MOUSEMOVE, 0, ((sy & 0xFFFF) << 16) | (sx & 0xFFFF)),
        (down_msg, mask, ((sy & 0xFFFF) << 16) | (sx & 0xFFFF)),
    ]
    count = max(1, int(steps))
    for index in range(1, count + 1):
        x = sx + round((ex - sx) * index / count)
        y = sy + round((ey - sy) * index / count)
        sequence.append((WM_MOUSEMOVE, mask, ((y & 0xFFFF) << 16) | (x & 0xFFFF)))
    sequence.append((up_msg, 0, ((ey & 0xFFFF) << 16) | (ex & 0xFFFF)))
    sent_count = 0
    queued: list[str] = []
    down_sent = False
    up_lparam = ((ey & 0xFFFF) << 16) | (ex & 0xFFFF)
    for message, wparam, lparam in sequence:
        if not user32.PostMessageW(int(hwnd), int(message), int(wparam), int(lparam)):
            if down_sent:
                # The DOWN already queued; always try to balance it before
                # returning failure, otherwise the target stays in drag state.
                try:
                    user32.PostMessageW(int(hwnd), int(up_msg), 0, int(up_lparam))
                except Exception:  # noqa: BLE001 - best effort release
                    pass
            raise VirtualCursorFailure(
                f"PostMessage failed for message 0x{message:04X} after {sent_count} queued",
                "hybrid 仅在尚未投递时降级",
                sent=sent_count,
            )
        sent_count += 1
        queued.append(f"0x{message:04X}")
        if message == down_msg:
            down_sent = True
        elif message == up_msg:
            down_sent = False
    return {
        "ok": True,
        "used": "post_message",
        "hwnd": int(hwnd),
        "screen": [list(points[0]), list(points[1])],
        "client": [list(mapped[0]), list(mapped[1])],
        "target_dpi": int(dpi),
        "dpi_awareness": awareness,
        "messages": queued,
        "sent": sent_count,
    }


def path_messages(points: list[tuple[int, int]], *, button: str = "left") -> list[tuple[int, int, int]]:
    """Build a continuous drag sequence; every move after DOWN carries mask."""
    if len(points) < 2:
        raise VirtualCursorUnsupported("path needs at least two points")
    if button == "left":
        down_msg, up_msg, mask = WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON
    elif button == "right":
        down_msg, up_msg, mask = WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON
    elif button == "middle":
        down_msg, up_msg, mask = WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON
    else:
        raise VirtualCursorUnsupported(f"unsupported path button: {button!r}")
    first_x, first_y = int(points[0][0]), int(points[0][1])
    last_x, last_y = int(points[-1][0]), int(points[-1][1])
    sequence = [
        (WM_MOUSEMOVE, 0, ((first_y & 0xFFFF) << 16) | (first_x & 0xFFFF)),
        (down_msg, mask, ((first_y & 0xFFFF) << 16) | (first_x & 0xFFFF)),
    ]
    for px, py in points[1:]:
        x, y = int(px), int(py)
        sequence.append((WM_MOUSEMOVE, mask, ((y & 0xFFFF) << 16) | (x & 0xFFFF)))
    sequence.append((up_msg, 0, ((last_y & 0xFFFF) << 16) | (last_x & 0xFFFF)))
    return sequence


def wheel_message(amount: int, screen_x: int, screen_y: int) -> tuple[int, int, int]:
    notches = int(amount)
    if notches == 0 or abs(notches) > 20:
        raise VirtualCursorUnsupported("scroll amount must be within -20..20 and non-zero")
    x, y = int(screen_x), int(screen_y)
    delta = notches * WHEEL_DELTA
    return (
        WM_MOUSEWHEEL,
        (delta & 0xFFFF) << 16,
        ((y & 0xFFFF) << 16) | (x & 0xFFFF),
    )


@_fail_open
def post_message_scroll(
    hwnd: int,
    screen_x: int,
    screen_y: int,
    amount: int,
) -> dict:
    """Post WM_MOUSEWHEEL; wheel coordinates are screen coordinates."""
    if not IS_WINDOWS:
        raise VirtualCursorUnsupported("PostMessage is Windows-only")
    try:
        notches = int(amount)
    except (TypeError, ValueError) as error:
        raise VirtualCursorUnsupported("scroll amount must be an integer") from error
    if notches == 0 or abs(notches) > 20:
        raise VirtualCursorUnsupported("scroll amount must be within -20..20 and non-zero")
    user32 = _user32()
    if not user32.IsWindow(int(hwnd)) or not user32.IsWindowVisible(int(hwnd)):
        raise VirtualCursorUnsupported("target window is not available")
    if user32.IsIconic(int(hwnd)):
        raise VirtualCursorUnsupported("target window is minimized")
    _reject_shell_window(int(hwnd))
    result = ctypes.c_size_t(0)
    if not user32.SendMessageTimeoutW(
        int(hwnd), WM_NULL, 0, 0,
        SMTO_ABORTIFHUNG | SMTO_BLOCK, 50, ctypes.byref(result),
    ):
        raise VirtualCursorFailure("target message queue did not answer preflight within 50ms")
    x, y = int(screen_x), int(screen_y)
    if not (-32768 <= x <= 32767 and -32768 <= y <= 32767):
        raise VirtualCursorUnsupported("screen coordinates do not fit WM_MOUSEWHEEL lParam")
    message, wparam, lparam = wheel_message(notches, x, y)
    if not user32.PostMessageW(int(hwnd), message, wparam, lparam):
        raise VirtualCursorFailure("PostMessage WM_MOUSEWHEEL failed", sent=0)
    return {
        "ok": True,
        "used": "post_message",
        "hwnd": int(hwnd),
        "screen": [x, y],
        "amount": int(notches),
        "messages": ["0x020A"],
        "sent": 1,
    }


@_fail_open
def post_message_path(
    hwnd: int,
    points: list[tuple[int, int]],
    *,
    button: str = "left",
) -> dict:
    """Post a continuous drag across every point with MK_LBUTTON held."""
    if not IS_WINDOWS:
        raise VirtualCursorUnsupported("PostMessage is Windows-only")
    if not isinstance(points, list) or len(points) < 2 or len(points) > 512:
        raise VirtualCursorUnsupported("path needs 2..512 points")
    user32 = _user32()
    if not user32.IsWindow(int(hwnd)) or not user32.IsWindowVisible(int(hwnd)):
        raise VirtualCursorUnsupported("target window is not available")
    if user32.IsIconic(int(hwnd)):
        raise VirtualCursorUnsupported("target window is minimized")
    _reject_shell_window(int(hwnd))
    result = ctypes.c_size_t(0)
    if not user32.SendMessageTimeoutW(
        int(hwnd), WM_NULL, 0, 0,
        SMTO_ABORTIFHUNG | SMTO_BLOCK, 50, ctypes.byref(result),
    ):
        raise VirtualCursorFailure("target message queue did not answer preflight within 50ms")
    if button == "left":
        down_msg, up_msg, mask = WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON
    elif button == "right":
        down_msg, up_msg, mask = WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON
    elif button == "middle":
        down_msg, up_msg, mask = WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON
    else:
        raise VirtualCursorUnsupported(f"unsupported path button: {button!r}")
    dpi = _dpi_for_window(user32, int(hwnd))
    awareness = _dpi_awareness(user32, int(hwnd))
    mapped: list[tuple[int, int]] = []
    for sx, sy in points:
        client = wintypes.POINT(int(sx), int(sy))
        if not user32.ScreenToClient(int(hwnd), ctypes.byref(client)):
            raise VirtualCursorUnsupported("ScreenToClient failed")
        rect = wintypes.RECT()
        if not user32.GetClientRect(int(hwnd), ctypes.byref(rect)):
            raise VirtualCursorUnsupported("GetClientRect failed")
        if not (rect.left <= client.x < rect.right and rect.top <= client.y < rect.bottom):
            raise VirtualCursorUnsupported("path point is outside the client area")
        mapped.append(map_client_coordinates(client.x, client.y, dpi, awareness))
    sequence = path_messages(mapped, button=button)
    sent_count = 0
    queued: list[str] = []
    down_sent = False
    last_x, last_y = mapped[-1]
    up_lparam = ((last_y & 0xFFFF) << 16) | (last_x & 0xFFFF)
    for message, wparam, lparam in sequence:
        if not user32.PostMessageW(int(hwnd), int(message), int(wparam), int(lparam)):
            if down_sent:
                try:
                    user32.PostMessageW(int(hwnd), int(up_msg), 0, int(up_lparam))
                except Exception:  # noqa: BLE001 - best effort release
                    pass
            raise VirtualCursorFailure(
                f"PostMessage failed for message 0x{message:04X} after {sent_count} queued",
                sent=sent_count,
            )
        sent_count += 1
        queued.append(f"0x{message:04X}")
        if message == down_msg:
            down_sent = True
        elif message == up_msg:
            down_sent = False
    return {
        "ok": True,
        "used": "post_message",
        "hwnd": int(hwnd),
        "points": [[int(x), int(y)] for x, y in mapped],
        "button": button,
        "mask": mask,
        "messages": queued,
        "sent": sent_count,
    }


__all__ = [
    "E_VIRTUAL_CURSOR_FAILED",
    "E_VIRTUAL_CURSOR_UNSUPPORTED",
    "MK_LBUTTON",
    "MK_MBUTTON",
    "MK_RBUTTON",
    "VirtualCursorFailure",
    "VirtualCursorUnsupported",
    "is_shell_window_class",
    "is_system_process_name",
    "process_image_name",
    "map_client_coordinates",
    "mouse_messages",
    "post_message_click",
    "post_message_drag",
    "post_message_path",
    "post_message_scroll",
    "path_messages",
    "wheel_message",
    "resolve_window",
]
