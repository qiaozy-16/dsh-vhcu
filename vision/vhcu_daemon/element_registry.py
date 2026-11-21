"""P4 Element Registry + dHash fingerprints + stale-aware action preparation."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .uia_worker import UiaError, UiaFailed, UiaWorker, json_safe

HASH_BITS = 64
BOUNDS_TOLERANCE = 2
HASH_HAMMING_MAX = 10
DEFAULT_TTL_SECONDS = 300.0
MAX_ENTRIES = 512


class ElementNotFound(UiaError):
    code = "E_ELEMENT_NOT_FOUND"


class ElementStale(UiaError):
    code = "E_ELEMENT_STALE"


class ElementCaptureFailed(UiaError):
    """A screenshot/fingerprint prerequisite failed; this is NOT a stale token."""
    code = "E_CAPTURE"


@dataclass
class ElementRecord:
    element_id: str
    hwnd: int
    locator: dict
    options: dict
    element: dict
    bounds: dict
    fingerprint: Optional[str]
    window_rect: Optional[dict]
    created_at: float
    expires_at: float

    def to_public(self) -> dict:
        return {
            "element_id": self.element_id,
            "hwnd": self.hwnd,
            "element": dict(self.element),
            "bounds": dict(self.bounds),
            "fingerprint": self.fingerprint,
            "fingerprint_available": self.fingerprint is not None,
        }


@dataclass
class _Stored:
    record: ElementRecord
    touched_at: float = field(default_factory=time.monotonic)


def dhash_bgra(data: bytes, width: int, height: int, bounds: dict,
               window_rect: Optional[dict] = None) -> Optional[str]:
    """64-bit dHash of one physical ROI inside a BGRA window capture.

    The ROI bounds are screen_physical; window_rect maps them into the frame.
    Returns None when the ROI is too small or outside the frame.
    """
    if width <= 0 or height <= 0 or len(data) < width * height * 4:
        return None
    x = int(bounds.get("x", 0))
    y = int(bounds.get("y", 0))
    w = int(bounds.get("width", 0))
    h = int(bounds.get("height", 0))
    if window_rect:
        x -= int(window_rect.get("x", 0))
        y -= int(window_rect.get("y", 0))
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + w), min(height, y + h)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    bits = 0
    previous: Optional[int] = None
    for gy in range(8):
        sy = y0 + max(0, min((y1 - y0) - 1, int((gy + 0.5) * (y1 - y0) / 8)))
        previous = None
        for gx in range(9):
            sx = x0 + max(0, min((x1 - x0) - 1, int((gx + 0.5) * (x1 - x0) / 9)))
            offset = (sy * width + sx) * 4
            b, g, r = data[offset], data[offset + 1], data[offset + 2]
            lum = (b * 114 + g * 587 + r * 299) // 1000
            if previous is not None:
                bits = (bits << 1) | (1 if lum > previous else 0)
            previous = lum
    return f"{bits:016x}"


def hamming(a: Optional[str], b: Optional[str]) -> int:
    if not a or not b:
        return HASH_BITS
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError:
        return HASH_BITS


def _bounds_delta(a: dict, b: dict) -> int:
    keys = ("x", "y", "width", "height")
    return max(abs(int(a.get(k, 0)) - int(b.get(k, 0))) for k in keys)


class ElementRegistry:
    """Thread-safe in-memory element token registry with TTL pruning."""

    def __init__(self, ttl: float = DEFAULT_TTL_SECONDS, max_entries: int = MAX_ENTRIES):
        self._ttl = float(ttl)
        self._max_entries = int(max_entries)
        self._lock = threading.Lock()
        self._items: dict[str, _Stored] = {}

    def _prune_locked(self, now: float) -> None:
        for key, stored in list(self._items.items()):
            if stored.record.expires_at <= now:
                self._items.pop(key, None)
        if len(self._items) > self._max_entries:
            ordered = sorted(self._items.items(), key=lambda item: item[1].touched_at)
            for key, _ in ordered[: len(self._items) - self._max_entries]:
                self._items.pop(key, None)

    def register_result(self, result: dict, locator: dict, options: dict,
                        capture_fn: Optional[Callable[[int], Any]] = None) -> dict:
        """Attach opaque element tokens and initial dHash fingerprints."""
        if capture_fn is None:
            from .win_capture import capture_window
            capture_fn = capture_window
        hwnd = int(result.get("hwnd") or 0)
        window_rect = result.get("window_rect")
        frame = None
        if hwnd > 0:
            try:
                frame = capture_fn(hwnd)
            except Exception:  # noqa: BLE001
                frame = None
        elements = []
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            for element in result.get("elements") or []:
                bounds = element.get("bounds") or {}
                fingerprint = None
                if frame is not None:
                    try:
                        fingerprint = dhash_bgra(
                            frame.data, frame.width, frame.height, bounds, window_rect
                        )
                    except Exception:  # noqa: BLE001 - fingerprint must never block a token
                        fingerprint = None
                token = f"el_{secrets.token_urlsafe(12)}"
                element = json_safe(dict(element))
                element["element_id"] = token
                element["fingerprint"] = fingerprint
                element["fingerprint_available"] = fingerprint is not None
                record = ElementRecord(
                    element_id=token,
                    hwnd=hwnd,
                    locator=dict(locator),
                    options=dict(options),
                    element=dict(element),
                    bounds=dict(bounds),
                    fingerprint=fingerprint,
                    window_rect=window_rect,
                    created_at=now,
                    expires_at=now + self._ttl,
                )
                self._items[token] = _Stored(record, now)
                elements.append(element)
            self._prune_locked(now)
        # The frame is used only for dHash sampling and is deliberately not
        # stored in a record; release the local full-window buffer now.
        frame = None
        out = json_safe(dict(result))
        out["elements"] = elements
        out["element_count"] = len(elements)
        return out

    def get(self, element_id: str) -> ElementRecord:
        now = time.monotonic()
        with self._lock:
            stored = self._items.get(str(element_id))
            if stored is None:
                raise ElementNotFound("元素 token 不存在或已过期")
            if stored.record.expires_at <= now:
                self._items.pop(str(element_id), None)
                raise ElementNotFound("元素 token 已过期")
            stored.touched_at = now
            return stored.record

    def update(self, record: ElementRecord) -> None:
        now = time.monotonic()
        with self._lock:
            if record.element_id not in self._items:
                raise ElementNotFound("元素 token 已失效")
            record.created_at = now
            record.expires_at = now + self._ttl
            self._items[record.element_id] = _Stored(record, now)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


class ElementActionService:
    """Resolve stale tokens and produce an invoke/coordinate action plan."""

    def __init__(self, registry: ElementRegistry, worker: UiaWorker,
                 capture_fn: Optional[Callable[[int], Any]] = None):
        self.registry = registry
        self.worker = worker
        self._capture_fn = capture_fn

    def _capture(self, hwnd: int):
        if self._capture_fn is not None:
            return self._capture_fn(hwnd)
        from .win_capture import capture_window
        return capture_window(hwnd)

    @staticmethod
    def _pick_candidate(elements: list[dict], record: ElementRecord) -> Optional[dict]:
        if not elements:
            return None
        target = record.element
        target_id = str(target.get("automation_id") or "")
        target_name = str(target.get("name") or "")
        target_type = str(target.get("control_type") or "")
        candidates = []
        for element in elements:
            if target_id and str(element.get("automation_id") or "") == target_id:
                candidates.append(element)
            elif target_name and str(element.get("name") or "") == target_name:
                candidates.append(element)
        if not candidates:
            candidates = [
                element for element in elements
                if (not target_type or element.get("control_type") == target_type)
            ]
        if not candidates:
            candidates = list(elements)
        old = record.bounds or {}
        old_cx = int(old.get("x", 0)) + int(old.get("width", 0)) // 2
        old_cy = int(old.get("y", 0)) + int(old.get("height", 0)) // 2

        def distance(element: dict) -> int:
            b = element.get("bounds") or {}
            cx = int(b.get("x", 0)) + int(b.get("width", 0)) // 2
            cy = int(b.get("y", 0)) + int(b.get("height", 0)) // 2
            return abs(cx - old_cx) + abs(cy - old_cy)

        return min(candidates, key=distance)

    def prepare(self, element_id: str, retry_max_attempts: int = 3) -> dict:
        """Re-find + fingerprint-check an element.

        Error taxonomy matters here: only an actual bounds/hash mismatch is
        E_ELEMENT_STALE. A missing token is E_ELEMENT_NOT_FOUND, a UIA failure
        stays E_UIA_*, and a capture/prerequisite failure is E_CAPTURE. Internal
        AttributeErrors must never be relabelled as a stale element.
        """
        record = self.registry.get(element_id)
        retries = max(0, min(5, int(retry_max_attempts)))
        stale_seen = False
        last_error: Optional[str] = None

        def retry_pause(attempt: int) -> None:
            if attempt < retries:
                time.sleep(min(0.4, 0.05 * (2 ** attempt)))

        for attempt in range(retries + 1):
            try:
                fresh = self.worker.find(
                    record.hwnd, record.locator, record.options, timeout_ms=4000
                )
            except UiaError as error:
                last_error = str(error)
                if attempt >= retries:
                    raise
                retry_pause(attempt)
                continue
            except Exception as error:  # noqa: BLE001 - unexpected internal/UIA failure
                last_error = f"{type(error).__name__}: {error}"
                if attempt >= retries:
                    raise UiaFailed(
                        f"UIA 查找失败（非 stale）: {last_error}",
                        "检查 UIA worker/COM 状态，或重新调用 vhcu_find_elements",
                    ) from error
                retry_pause(attempt)
                continue

            candidate = self._pick_candidate(list(fresh.get("elements") or []), record)
            if candidate is None:
                last_error = "元素已不在当前窗口中"
                if attempt >= retries:
                    raise ElementNotFound(
                        f"元素已不在当前窗口中，重试 {retries} 次后仍未找到",
                        "重新调用 vhcu_find_elements，获取新的 element_id 后再操作",
                    )
                retry_pause(attempt)
                continue

            try:
                frame = self._capture(record.hwnd)
            except Exception as error:  # noqa: BLE001 - capture failure is not stale
                frame = None
                last_error = f"{type(error).__name__}: {error}"
            if frame is None:
                last_error = f"无法重新截图，不能确认元素视觉指纹: {last_error or 'capture returned None'}"
                if attempt >= retries:
                    raise ElementCaptureFailed(
                        last_error,
                        "检查目标窗口前台/可见状态与截图后端；重新调用 vhcu_find_elements 后再操作",
                    )
                retry_pause(attempt)
                continue

            try:
                new_fingerprint = dhash_bgra(
                    frame.data, frame.width, frame.height,
                    candidate.get("bounds") or {}, fresh.get("window_rect"),
                )
            except Exception as error:  # noqa: BLE001 - fingerprint computation is not stale
                frame = None
                last_error = f"dHash 计算失败（非 stale）: {type(error).__name__}: {error}"
                if attempt >= retries:
                    raise UiaFailed(
                        last_error,
                        "检查截图尺寸/BGRA buffer；重新调用 vhcu_find_elements 后再操作",
                    ) from error
                retry_pause(attempt)
                continue
            # dHash samples the buffer directly; no cropped image or full-frame
            # reference is retained by the record.
            frame = None
            bounds_ok = _bounds_delta(record.bounds, candidate.get("bounds") or {}) <= BOUNDS_TOLERANCE
            hash_ok = (
                record.fingerprint is None
                or new_fingerprint is None
                or hamming(record.fingerprint, new_fingerprint) <= HASH_HAMMING_MAX
            )
            if bounds_ok and hash_ok:
                record.element = dict(candidate)
                record.bounds = dict(candidate.get("bounds") or {})
                record.fingerprint = new_fingerprint or record.fingerprint
                record.window_rect = fresh.get("window_rect") or record.window_rect
                self.registry.update(record)
                return {
                    "record": record,
                    "element": candidate,
                    "retries": attempt,
                    "stale_seen": stale_seen,
                }
            stale_seen = True
            last_error = "视觉指纹或 bounds 已变化"
            if attempt >= retries:
                raise ElementStale(
                    f"元素已失效，重试 {retries} 次后仍未恢复: {last_error}",
                    "重新调用 vhcu_find_elements，获取新的 element_id 后再操作",
                )
            retry_pause(attempt)

        # Defensive fallback; the loop always raises on its final iteration.
        raise ElementStale(
            f"元素已失效，重试 {retries} 次后仍未恢复: {last_error or 'stale'}",
            "重新调用 vhcu_find_elements，获取新的 element_id 后再操作",
        )

    def click(self, element_id: str, retry_max_attempts: int = 3) -> dict:
        prepared = self.prepare(element_id, retry_max_attempts)
        record = prepared["record"]
        method = self.worker.invoke(
            record.hwnd,
            record.locator,
            prepared["element"],
            record.options,
            timeout_ms=4000,
        )
        out = dict(method)
        out["element_id"] = record.element_id
        out["element"] = dict(prepared["element"])
        out["retries"] = prepared["retries"]
        out["stale_seen"] = prepared["stale_seen"]
        return out


__all__ = [
    "BOUNDS_TOLERANCE",
    "DEFAULT_TTL_SECONDS",
    "ElementActionService",
    "ElementCaptureFailed",
    "ElementNotFound",
    "ElementRecord",
    "ElementRegistry",
    "ElementStale",
    "HASH_HAMMING_MAX",
    "dhash_bgra",
    "hamming",
]
