"""P3 UIA worker: semantic element discovery in an isolated COM thread.

The daemon's stdin command loop is latency-sensitive. UIA COM calls can block
for seconds on a busy application, so this module owns one dedicated thread and
serializes every request through a bounded queue. No COM object ever leaves the
worker thread.

P3 is discovery-only: it returns semantic properties, physical bounds and
pattern availability. It never invokes a pattern and never reads Value text
(``ValuePattern.Value`` is not called at all; password fields therefore cannot
leak their content).
"""

from __future__ import annotations

import math
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Optional

REQUEST_TIMEOUT_MS = 2500
MAX_DEPTH_LIMIT = 10
MAX_LIMIT = 200
MAX_NODE_BUDGET = 5000
DEFAULT_DEPTH = 6
DEFAULT_LIMIT = 50
DEFAULT_NODE_BUDGET = 500

LOCATOR_KEYS = {"name", "name_match", "automation_id", "control_type", "class_name"}
OPTION_KEYS = {"max_depth", "limit", "node_budget", "include_offscreen", "allow_value_read"}


class UiaError(RuntimeError):
    code = "E_UIA_FAILED"
    hint: Optional[str] = None

    def __init__(self, message: str, hint: Optional[str] = None):
        super().__init__(message)
        self.hint = hint


class UiaUnavailable(UiaError):
    code = "E_UIA_UNAVAILABLE"


class UiaTimeout(UiaError):
    code = "E_UIA_TIMEOUT"


class UiaFailed(UiaError):
    code = "E_UIA_FAILED"


class UiaWindowGone(UiaError):
    code = "E_WINDOW_GONE"


class UiaPrivacyBlocked(UiaError):
    code = "E_PRIVACY_BLOCKED"


def _control_hwnd(control: Any) -> int:
    """Best-effort top-level HWND for a UIA control; 0 means unknown."""
    try:
        top = control.GetTopLevelControl()
        value = int(getattr(top, "NativeWindowHandle", 0) or 0)
        if value > 0:
            return value
    except Exception:  # noqa: BLE001
        pass
    try:
        value = int(getattr(control, "NativeWindowHandle", 0) or 0)
        return value if value > 0 else 0
    except Exception:  # noqa: BLE001
        return 0


def json_safe(value: Any, _depth: int = 0) -> Any:
    """Recursively project values to lossless JSON primitives.

    COM objects, bytes, NaN/Infinity and other Python-only values must never
    reach the NDJSON writer or the tool response. Unknown objects are reduced
    to their string form so a single odd UIA property cannot poison the whole
    element list.
    """
    if _depth > 10:
        return str(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            return bytes(value).hex()
        except Exception:  # noqa: BLE001
            return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item, _depth + 1) for item in value]
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return None


def normalize_find_request(hwnd: Any, locator: Any = None,
                           options: Any = None) -> tuple[int, dict, dict]:
    """Validate and normalize one uia.find request. Raises ValueError."""
    if isinstance(hwnd, bool):
        raise ValueError("hwnd must be a positive integer")
    try:
        target = int(hwnd)
    except (TypeError, ValueError) as error:
        raise ValueError("hwnd must be a positive integer") from error
    if target <= 0:
        raise ValueError("hwnd must be a positive integer")

    raw_locator = {} if locator is None else locator
    if not isinstance(raw_locator, dict):
        raise ValueError("locator must be an object")
    unknown = set(raw_locator) - LOCATOR_KEYS
    if unknown:
        raise ValueError(f"unknown locator field: {sorted(unknown)[0]}")

    name = raw_locator.get("name")
    if name is not None and not isinstance(name, str):
        raise ValueError("locator.name must be a string")
    name_match = str(raw_locator.get("name_match") or "exact").strip().lower()
    if name_match not in ("exact", "contains"):
        raise ValueError("locator.name_match must be exact or contains")
    automation_id = raw_locator.get("automation_id")
    if automation_id is not None and not isinstance(automation_id, str):
        raise ValueError("locator.automation_id must be a string")
    class_name = raw_locator.get("class_name")
    if class_name is not None and not isinstance(class_name, str):
        raise ValueError("locator.class_name must be a string")
    control_type = raw_locator.get("control_type")
    if control_type is not None and not isinstance(control_type, (str, int)):
        raise ValueError("locator.control_type must be a string or integer")
    if isinstance(control_type, bool):
        raise ValueError("locator.control_type must be a string or integer")
    if isinstance(control_type, str) and not control_type.strip():
        raise ValueError("locator.control_type must not be empty")

    normalized_locator = {
        "name": name,
        "name_match": name_match,
        "automation_id": automation_id,
        "control_type": control_type,
        "class_name": class_name,
    }

    raw_options = {} if options is None else options
    if not isinstance(raw_options, dict):
        raise ValueError("options must be an object")
    unknown = set(raw_options) - OPTION_KEYS
    if unknown:
        raise ValueError(f"unknown option: {sorted(unknown)[0]}")

    def _int_option(key: str, default: int, low: int, high: int) -> int:
        value = raw_options.get(key, default)
        if isinstance(value, bool):
            raise ValueError(f"options.{key} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"options.{key} must be an integer") from error
        if parsed < low or parsed > high:
            raise ValueError(f"options.{key} must be within {low}..{high}")
        return parsed

    normalized_options = {
        "max_depth": _int_option("max_depth", DEFAULT_DEPTH, 1, MAX_DEPTH_LIMIT),
        "limit": _int_option("limit", DEFAULT_LIMIT, 1, MAX_LIMIT),
        "node_budget": _int_option("node_budget", DEFAULT_NODE_BUDGET, 1, MAX_NODE_BUDGET),
        "include_offscreen": bool(raw_options.get("include_offscreen", True)),
        "allow_value_read": bool(raw_options.get("allow_value_read", False)),
    }
    return target, normalized_locator, normalized_options


def _safe_read(control: Any, name: str, default: Any) -> Any:
    try:
        value = getattr(control, name)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else value


def _control_type_id(value: Any, auto: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text, 0)
    except ValueError:
        pass
    names = getattr(auto, "ControlTypeNames", {}) or {}
    wanted = text.casefold()
    for code, full_name in names.items():
        full = str(full_name)
        base = full[:-7] if full.endswith("Control") else full
        if wanted in (full.casefold(), base.casefold()):
            return int(code)
    raise UiaFailed(f"unknown control_type {value!r}")


def _type_name(code: int, auto: Any) -> str:
    names = getattr(auto, "ControlTypeNames", {}) or {}
    full = str(names.get(code, f"ControlType{code}"))
    return full[:-7] if full.endswith("Control") else full


def _within(raw: tuple[int, int, int, int], window: tuple[int, int, int, int],
            margin: int = 4) -> bool:
    left, top, right, bottom = raw
    wl, wt, ww, wh = window
    wr, wb = wl + ww, wt + wh
    return (
        left >= wl - margin
        and top >= wt - margin
        and right <= wr + margin
        and bottom <= wb + margin
    )


def _normalize_bounds(raw: tuple[int, int, int, int], window: tuple[int, int, int, int],
                      dpi: int) -> tuple[tuple[int, int, int, int], str, bool]:
    if raw[2] <= raw[0] or raw[3] <= raw[1]:
        return (0, 0, 0, 0), "unknown", False
    if _within(raw, window):
        return raw, "screen_physical", True
    scale = max(1, int(dpi or 96)) / 96.0
    if scale > 1.0:
        scaled = (
            int(round(raw[0] * scale)),
            int(round(raw[1] * scale)),
            int(round(raw[2] * scale)),
            int(round(raw[3] * scale)),
        )
        if _within(scaled, window):
            return scaled, "screen_physical", True
    return raw, "unknown", False


class UiaAutomationAdapter:
    """Real uiautomation 2.x adapter. Constructed inside the worker thread."""

    name = "uiautomation"

    def __init__(self) -> None:
        self._auto = None
        self._initialized = False

    def initialize(self) -> None:
        try:
            import uiautomation as auto  # type: ignore
        except Exception as error:  # noqa: BLE001
            raise UiaUnavailable(
                f"uiautomation is not installed: {error}",
                "安装 requirements.txt 中的 uiautomation>=2.0.29（会带入 comtypes）",
            ) from error
        try:
            auto.InitializeUIAutomationInCurrentThread()
        except Exception as error:  # noqa: BLE001
            raise UiaUnavailable(f"UIA COM initialization failed: {error}") from error
        self._auto = auto
        self._initialized = True

    def uninitialize(self) -> None:
        auto, self._auto = self._auto, None
        if not self._initialized or auto is None:
            return
        self._initialized = False
        try:
            auto.UninitializeUIAutomationInCurrentThread()
        except Exception:  # noqa: BLE001
            pass

    def find(self, hwnd: int, locator: dict, options: dict) -> dict:
        auto = self._auto
        if auto is None:
            raise UiaUnavailable("UIA adapter is not initialized")
        try:
            from .win_capture import (
                ensure_dpi_awareness,
                window_dpi,
                window_pid,
                window_rect,
                window_title,
            )
        except Exception as error:  # noqa: BLE001
            raise UiaUnavailable(f"Windows UIA helpers unavailable: {error}") from error
        ensure_dpi_awareness()
        try:
            root = auto.ControlFromHandle(hwnd)
        except Exception as error:  # noqa: BLE001
            raise UiaFailed(f"ControlFromHandle failed: {error}") from error
        if root is None:
            raise UiaWindowGone(f"window {hwnd} no longer exists")
        try:
            win_rect = window_rect(hwnd)
        except Exception as error:  # noqa: BLE001
            code = getattr(error, "code", "")
            if code == "E_CAPTURE_TARGET_GONE":
                raise UiaWindowGone(str(error)) from error
            raise UiaFailed(f"GetWindowRect failed: {error}") from error
        dpi = window_dpi(hwnd)
        window = {
            "hwnd": int(hwnd),
            "pid": int(window_pid(hwnd)),
            "title": window_title(hwnd),
        }

        target_type = _control_type_id(locator.get("control_type"), auto)
        name_query = locator.get("name")
        name_query_cf = name_query.casefold() if isinstance(name_query, str) else None
        name_match = locator.get("name_match", "exact")
        automation_id = locator.get("automation_id")
        class_name = locator.get("class_name")
        include_offscreen = bool(options.get("include_offscreen", True))
        max_depth = int(options["max_depth"])
        limit = int(options["limit"])
        node_budget = int(options["node_budget"])

        queue_: deque[tuple[Any, int]] = deque([(root, 0)])
        visited = 0
        truncated = False
        elements: list[dict] = []
        all_physical = True

        while queue_:
            if len(elements) >= limit:
                truncated = True
                break
            if visited >= node_budget:
                truncated = True
                break
            control, depth = queue_.popleft()
            visited += 1
            if depth > 0:
                name = str(_safe_read(control, "Name", "") or "")
                aid = str(_safe_read(control, "AutomationId", "") or "")
                cls = str(_safe_read(control, "ClassName", "") or "")
                try:
                    ctype = int(_safe_read(control, "ControlType", 0) or 0)
                except Exception:  # noqa: BLE001
                    ctype = 0
                matches = True
                if name_query_cf is not None:
                    actual = name.casefold()
                    matches = name_query_cf in actual if name_match == "contains" else name_query_cf == actual
                if matches and automation_id is not None:
                    matches = aid == str(automation_id)
                if matches and class_name is not None:
                    matches = cls == str(class_name)
                if matches and target_type is not None:
                    matches = ctype == target_type
                if matches:
                    is_offscreen = bool(_safe_read(control, "IsOffscreen", False))
                    if include_offscreen or not is_offscreen:
                        element, physical = self._element(
                            control, auto, hwnd, win_rect, dpi, ctype,
                            name, aid, cls, is_offscreen,
                            bool(options.get("allow_value_read", False)),
                        )
                        all_physical = all_physical and physical
                        elements.append(element)
            if depth >= max_depth:
                continue
            try:
                children = control.GetChildren()
            except Exception:  # noqa: BLE001
                children = []
            for child in children or []:
                if child is not None:
                    queue_.append((child, depth + 1))

        if queue_:
            truncated = True
        return {
            "backend": self.name,
            "hwnd": int(hwnd),
            "window": window,
            "bounds_space": "screen_physical" if all_physical else "mixed",
            "dpi": int(dpi),
            "window_rect": {
                "x": int(win_rect[0]),
                "y": int(win_rect[1]),
                "width": int(win_rect[2]),
                "height": int(win_rect[3]),
            },
            "count": len(elements),
            "truncated": bool(truncated),
            "visited": visited,
            "elements": elements,
        }

    def invoke(self, hwnd: int, locator: dict, target: dict, options: dict) -> dict:
        """Resolve the current control and invoke it; no raw mouse click here."""
        auto = self._auto
        if auto is None:
            raise UiaUnavailable("UIA adapter is not initialized")
        try:
            root = auto.ControlFromHandle(hwnd)
        except Exception as error:  # noqa: BLE001
            raise UiaFailed(f"ControlFromHandle failed: {error}") from error
        if root is None:
            raise UiaWindowGone(f"window {hwnd} no longer exists")
        max_depth = int(options.get("max_depth", DEFAULT_DEPTH))
        node_budget = int(options.get("node_budget", DEFAULT_NODE_BUDGET))
        target_name = str(target.get("name") or "")
        target_id = str(target.get("automation_id") or "")
        target_type = str(target.get("control_type") or "")
        target_bounds = target.get("bounds") or {}
        target_cx = int(target_bounds.get("x", 0)) + int(target_bounds.get("width", 0)) // 2
        target_cy = int(target_bounds.get("y", 0)) + int(target_bounds.get("height", 0)) // 2
        queue_: deque[tuple[Any, int]] = deque([(root, 0)])
        visited = 0
        best = None
        best_score = None
        while queue_ and visited < node_budget:
            control, depth = queue_.popleft()
            visited += 1
            if depth > 0:
                name = str(_safe_read(control, "Name", "") or "")
                aid = str(_safe_read(control, "AutomationId", "") or "")
                try:
                    ctype = int(_safe_read(control, "ControlType", 0) or 0)
                except Exception:  # noqa: BLE001
                    ctype = 0
                ctype_name = _type_name(ctype, auto)
                name_ok = not target_name or name == target_name
                id_ok = not target_id or aid == target_id
                type_ok = not target_type or ctype_name == target_type or str(ctype) == target_type
                if name_ok and id_ok and type_ok:
                    try:
                        rect = control.BoundingRectangle
                        b = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
                        cx = (b[0] + b[2]) // 2
                        cy = (b[1] + b[3]) // 2
                        score = abs(cx - target_cx) + abs(cy - target_cy)
                    except Exception:  # noqa: BLE001
                        score = 1_000_000
                    if best_score is None or score < best_score:
                        best = control
                        best_score = score
            if depth >= max_depth:
                continue
            try:
                children = control.GetChildren()
            except Exception:  # noqa: BLE001
                children = []
            for child in children or []:
                if child is not None:
                    queue_.append((child, depth + 1))
        if best is None:
            raise UiaFailed("element no longer exists in the current window")
        try:
            pattern = best.GetInvokePattern()
        except Exception:  # noqa: BLE001
            pattern = None
        if pattern is not None:
            try:
                invoked = bool(pattern.Invoke())
            except Exception as error:  # noqa: BLE001
                raise UiaFailed(f"InvokePattern failed: {error}") from error
            return {
                "method": "invoke",
                "pattern": "Invoke",
                "invoked": invoked,
                "element": dict(target),
            }
        try:
            rect = best.BoundingRectangle
            bounds = {
                "x": int(rect.left),
                "y": int(rect.top),
                "width": int(rect.right - rect.left),
                "height": int(rect.bottom - rect.top),
                "right": int(rect.right),
                "bottom": int(rect.bottom),
            }
        except Exception:  # noqa: BLE001
            bounds = dict(target_bounds)
        return {
            "method": "coordinate",
            "pattern": None,
            "invoked": False,
            "center": {
                "x": int(bounds.get("x", 0)) + int(bounds.get("width", 0)) // 2,
                "y": int(bounds.get("y", 0)) + int(bounds.get("height", 0)) // 2,
            },
            "element": {**dict(target), "bounds": bounds},
        }

    def click_point(self, x: int, y: int, click_type: str = "click",
                    privacy_check: Optional[Callable[[int], bool]] = None) -> dict:
        """Resolve the control under a screen point and invoke it.

        Only a plain left click can use InvokePattern; any other click type (or
        an element without Invoke) returns a coordinate plan so the daemon can
        perform the real click through InputController.
        """
        auto = self._auto
        if auto is None:
            raise UiaUnavailable("UIA adapter is not initialized")
        try:
            from .win_capture import ensure_dpi_awareness
        except Exception as error:  # noqa: BLE001
            raise UiaUnavailable(f"Windows UIA helpers unavailable: {error}") from error
        ensure_dpi_awareness()
        try:
            control = auto.ControlFromPoint(int(x), int(y))
        except Exception as error:  # noqa: BLE001
            raise UiaFailed(f"ControlFromPoint failed: {error}") from error
        if control is None:
            raise UiaFailed("no UIA element exists at the given screen point")
        if privacy_check is not None:
            try:
                blocked = bool(privacy_check(_control_hwnd(control)))
            except Exception as error:  # noqa: BLE001 - fail closed
                raise UiaPrivacyBlocked(f"UIA point privacy check failed: {error}") from error
            if blocked:
                raise UiaPrivacyBlocked("UIA point target window is privacy-blocked")
        name = str(_safe_read(control, "Name", "") or "")
        aid = str(_safe_read(control, "AutomationId", "") or "")
        cls = str(_safe_read(control, "ClassName", "") or "")
        try:
            ctype = int(_safe_read(control, "ControlType", 0) or 0)
        except Exception:  # noqa: BLE001
            ctype = 0
        bounds: dict = {}
        try:
            rect = control.BoundingRectangle
            bounds = {
                "x": int(rect.left),
                "y": int(rect.top),
                "width": int(rect.right - rect.left),
                "height": int(rect.bottom - rect.top),
                "right": int(rect.right),
                "bottom": int(rect.bottom),
            }
        except Exception:  # noqa: BLE001
            bounds = {}
        element = {
            "name": name,
            "automation_id": aid,
            "class_name": cls,
            "control_type": ctype,
            "bounds": bounds,
        }
        center = {
            "x": int(bounds.get("x", 0)) + int(bounds.get("width", 0)) // 2,
            "y": int(bounds.get("y", 0)) + int(bounds.get("height", 0)) // 2,
        } if bounds else {"x": int(x), "y": int(y)}
        if click_type == "click":
            try:
                pattern = control.GetInvokePattern()
            except Exception:  # noqa: BLE001
                pattern = None
            if pattern is not None:
                try:
                    invoked = bool(pattern.Invoke())
                except Exception as error:  # noqa: BLE001
                    raise UiaFailed(f"InvokePattern failed: {error}") from error
                return {
                    "method": "invoke",
                    "pattern": "Invoke",
                    "invoked": invoked,
                    "center": center,
                    "element": element,
                }
        return {
            "method": "coordinate",
            "pattern": None,
            "invoked": False,
            "center": center,
            "element": element,
        }

    @staticmethod
    def _element(control: Any, auto: Any, hwnd: int,
                 win_rect: tuple[int, int, int, int], dpi: int, ctype: int,
                 name: str, aid: str, cls: str, is_offscreen: bool,
                 allow_value_read: bool = False) -> tuple[dict, bool]:
        is_password = bool(_safe_read(control, "IsPassword", False))
        is_enabled = bool(_safe_read(control, "IsEnabled", False))
        raw_bounds = (0, 0, 0, 0)
        try:
            rect = control.BoundingRectangle
            raw_bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        except Exception:  # noqa: BLE001
            pass
        bounds, bounds_space, physical = _normalize_bounds(raw_bounds, win_rect, dpi)
        left, top, right, bottom = bounds
        value = None
        if allow_value_read and not is_password:
            value_id = getattr(getattr(auto, "PatternId", None), "ValuePattern", None)
            if value_id is not None:
                try:
                    value_pattern = control.GetPattern(value_id)
                    if value_pattern is not None:
                        raw_value = value_pattern.Value
                        value = str(raw_value)[:2000] if raw_value is not None else None
                except Exception:  # noqa: BLE001
                    value = None
        patterns = []
        for label, attr in (
            ("Invoke", "InvokePattern"),
            ("Value", "ValuePattern"),
            ("Toggle", "TogglePattern"),
            ("SelectionItem", "SelectionItemPattern"),
            ("ExpandCollapse", "ExpandCollapsePattern"),
            ("ScrollItem", "ScrollItemPattern"),
            ("Text", "TextPattern"),
            ("RangeValue", "RangeValuePattern"),
        ):
            # Never even probe Value on a password field. No Value property is
            # read in P3 under any circumstances.
            if label == "Value" and is_password:
                continue
            pattern_id = getattr(getattr(auto, "PatternId", None), attr, None)
            if pattern_id is None:
                continue
            try:
                if control.GetPattern(pattern_id) is not None:
                    patterns.append(label)
            except Exception:  # noqa: BLE001
                continue
        return {
            "name": name,
            "automation_id": aid,
            "control_type": _type_name(ctype, auto),
            "class_name": cls,
            "bounds": {
                "x": left,
                "y": top,
                "width": max(0, right - left),
                "height": max(0, bottom - top),
                "right": right,
                "bottom": bottom,
            },
            "center": {"x": (left + right) // 2, "y": (top + bottom) // 2},
            "is_offscreen": is_offscreen,
            "is_enabled": is_enabled,
            "is_password": is_password,
            "patterns": patterns,
            "value": value,
            "value_redacted": bool(is_password),
            "bounds_space": bounds_space,
            "bounds_valid": physical,
        }, physical


class UiaWorker:
    """One serialized UIA request thread owned by the daemon."""

    def __init__(self, adapter_factory: Optional[Callable[[], Any]] = None):
        self._adapter_factory = adapter_factory or UiaAutomationAdapter
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._degraded = False
        # P1 隐患 6: a caller timeout means the COM call is still running and
        # cannot be cancelled. Mark the worker unhealthy instead of waiting for
        # it to recover; only a daemon restart can guarantee a clean COM state.
        self._unhealthy = False
        self._init_error: Optional[Exception] = None
        self._adapter: Any = None
        self._requests = 0
        self._errors = 0
        self._last_error: Optional[str] = None

    def start(self, timeout: float = 3.0) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._closed.clear()
            self._degraded = False
            self._unhealthy = False
            self._init_error = None
            self._thread = threading.Thread(target=self._run, name="vhcu-uia", daemon=True)
            thread = self._thread
        thread.start()
        if not self._ready.wait(max(0.1, float(timeout))):
            raise UiaUnavailable("UIA worker did not initialize in time")
        if self._init_error is not None:
            error = self._init_error
            if isinstance(error, UiaError):
                raise error
            raise UiaUnavailable(f"UIA worker failed to initialize: {error}")

    def _run(self) -> None:
        adapter = None
        try:
            try:
                adapter = self._adapter_factory()
                adapter.initialize()
            except Exception as error:  # noqa: BLE001
                with self._lock:
                    self._init_error = error
                self._ready.set()
                return
            with self._lock:
                self._adapter = adapter
            self._ready.set()
            while not self._closed.is_set():
                try:
                    job = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if job is None:
                    break
                future, kind, hwnd, locator, options, target = job
                try:
                    if kind == "invoke":
                        result = adapter.invoke(hwnd, locator, target, options)
                    elif kind == "point":
                        point_kwargs = {}
                        privacy_check = target.get("privacy_check")
                        if privacy_check is not None:
                            point_kwargs["privacy_check"] = privacy_check
                        result = adapter.click_point(
                            int(target.get("x", 0)),
                            int(target.get("y", 0)),
                            str(target.get("click_type") or "click"),
                            **point_kwargs,
                        )
                    else:
                        result = adapter.find(hwnd, locator, options)
                    future.set_result(json_safe(result))
                except UiaError as error:
                    with self._lock:
                        self._errors += 1
                        self._last_error = str(error)
                    future.set_exception(error)
                except Exception as error:  # noqa: BLE001
                    with self._lock:
                        self._errors += 1
                        self._last_error = f"{type(error).__name__}: {error}"
                    future.set_exception(UiaFailed(f"{type(error).__name__}: {error}"))
                finally:
                    with self._lock:
                        self._degraded = False
        finally:
            if adapter is not None:
                try:
                    adapter.uninitialize()
                except Exception:  # noqa: BLE001
                    pass
            with self._lock:
                self._adapter = None

    def find(self, hwnd: Any, locator: Any = None, options: Any = None,
             timeout_ms: int = REQUEST_TIMEOUT_MS) -> dict:
        target, normalized_locator, normalized_options = normalize_find_request(
            hwnd, locator, options
        )
        self.start()
        with self._lock:
            if self._unhealthy:
                raise UiaUnavailable(
                    "UIA worker became unhealthy after a request timeout",
                    "重启 daemon 以恢复 UIA；该状态不可重试",
                )
            if self._degraded:
                raise UiaTimeout("UIA worker is still completing the previous request")
        started = time.perf_counter()
        future: Future = Future()
        try:
            self._queue.put_nowait((future, "find", target, normalized_locator, normalized_options, None))
        except queue.Full as error:
            raise UiaTimeout("UIA worker queue is full") from error
        with self._lock:
            self._requests += 1
        try:
            result = future.result(timeout=max(0.1, float(timeout_ms) / 1000.0))
        except FutureTimeout as error:
            with self._lock:
                self._degraded = True
                self._unhealthy = True
            raise UiaTimeout(
                "UIA query timed out; the target application may be busy",
                "底层 COM 调用可能已僵死：重启 daemon 以恢复 UIA",
            ) from error
        except UiaError:
            raise
        except Exception as error:  # noqa: BLE001
            raise UiaFailed(f"{type(error).__name__}: {error}") from error
        if isinstance(result, dict):
            result.setdefault("elapsed_ms", round((time.perf_counter() - started) * 1000.0, 3))
        return result

    def invoke(self, hwnd: Any, locator: Any, target: dict,
               options: Any = None, timeout_ms: int = REQUEST_TIMEOUT_MS) -> dict:
        """Invoke the target element in the COM thread.

        The adapter is responsible for resolving the current control and
        choosing InvokePattern only; the worker never performs a raw mouse
        click. If InvokePattern is unavailable, the adapter returns a
        coordinate plan for the Node input layer.
        """
        target_hwnd, normalized_locator, normalized_options = normalize_find_request(
            hwnd, locator, options
        )
        if not isinstance(target, dict):
            raise UiaFailed("target element must be an object")
        self.start()
        with self._lock:
            if self._unhealthy:
                raise UiaUnavailable(
                    "UIA worker became unhealthy after a request timeout",
                    "重启 daemon 以恢复 UIA；该状态不可重试",
                )
            if self._degraded:
                raise UiaTimeout("UIA worker is still completing the previous request")
        future: Future = Future()
        try:
            self._queue.put_nowait((
                future, "invoke", target_hwnd, normalized_locator,
                normalized_options, dict(target),
            ))
        except queue.Full as error:
            raise UiaTimeout("UIA worker queue is full") from error
        with self._lock:
            self._requests += 1
        try:
            return future.result(timeout=max(0.1, float(timeout_ms) / 1000.0))
        except FutureTimeout as error:
            with self._lock:
                self._degraded = True
                self._unhealthy = True
            raise UiaTimeout(
                "UIA invoke timed out",
                "底层 COM 调用可能已僵死：重启 daemon 以恢复 UIA",
            ) from error
        except UiaError:
            raise
        except Exception as error:  # noqa: BLE001
            raise UiaFailed(f"{type(error).__name__}: {error}") from error

    def invoke_point(self, x: int, y: int, click_type: str = "click",
                     timeout_ms: int = REQUEST_TIMEOUT_MS,
                     privacy_check: Optional[Callable[[int], bool]] = None) -> dict:
        """Invoke the UIA element under one screen point (uia injection mode)."""
        self.start()
        with self._lock:
            if self._unhealthy:
                raise UiaUnavailable(
                    "UIA worker became unhealthy after a request timeout",
                    "重启 daemon 以恢复 UIA；该状态不可重试",
                )
            if self._degraded:
                raise UiaTimeout("UIA worker is still completing the previous request")
        started = time.perf_counter()
        future: Future = Future()
        try:
            self._queue.put_nowait((
                future, "point", 0, None, None,
                {
                    "x": int(x),
                    "y": int(y),
                    "click_type": str(click_type or "click"),
                    "privacy_check": privacy_check,
                },
            ))
        except queue.Full as error:
            raise UiaTimeout("UIA worker queue is full") from error
        with self._lock:
            self._requests += 1
        try:
            result = future.result(timeout=max(0.1, float(timeout_ms) / 1000.0))
        except FutureTimeout as error:
            with self._lock:
                self._degraded = True
                self._unhealthy = True
            raise UiaTimeout(
                "UIA point click timed out",
                "底层 COM 调用可能已僵死：重启 daemon 以恢复 UIA",
            ) from error
        except UiaError:
            raise
        except Exception as error:  # noqa: BLE001
            raise UiaFailed(f"{type(error).__name__}: {error}") from error
        if isinstance(result, dict):
            result.setdefault("elapsed_ms", round((time.perf_counter() - started) * 1000.0, 3))
        return result

    def status(self) -> dict:
        with self._lock:
            thread = self._thread
            return {
                "backend": "uiautomation",
                "started": bool(thread is not None and thread.is_alive()),
                "degraded": bool(self._degraded),
                "unhealthy": bool(self._unhealthy),
                "requests": self._requests,
                "errors": self._errors,
                "last_error": self._last_error,
            }

    def close(self, timeout: float = 2.0) -> None:
        with self._lock:
            self._closed.set()
            thread = self._thread
        if thread is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread.join(timeout=max(0.1, float(timeout)))


__all__ = [
    "DEFAULT_DEPTH",
    "DEFAULT_LIMIT",
    "DEFAULT_NODE_BUDGET",
    "MAX_DEPTH_LIMIT",
    "MAX_LIMIT",
    "MAX_NODE_BUDGET",
    "REQUEST_TIMEOUT_MS",
    "UiaAutomationAdapter",
    "UiaError",
    "UiaFailed",
    "UiaTimeout",
    "UiaUnavailable",
    "UiaWindowGone",
    "UiaWorker",
    "json_safe",
    "normalize_find_request",
]
