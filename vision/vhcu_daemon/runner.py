"""Vision runtime: owns the capture thread, stats and the latest frame."""

from __future__ import annotations

import threading
import time
from typing import Optional

from .diff import compare
from .privacy import (
    DEFAULT_BLACKLIST,
    foreground_identity,
    foreground_title,
    is_sensitive_title,
    sanitize_blacklist,
)
from .protocol import Protocol, ProtocolError
from .sources import CaptureError, Frame, SourceUnavailable, available_sources, create_source

MAX_FPS = 15
PRIVACY_BLOCKED_MESSAGE = (
    "当前处于敏感隐私窗口，VHCU 已强制拉闸。请立刻停止一切视觉和操作，原地等待用户处理！"
)
PRIVACY_BLOCKED_HINT = (
    "隐私安全高于一切：禁止使用 vp_* 或任何外部截图/视觉工具兜底；"
    "请直接向用户报告并暂停任务，等待用户切走敏感窗口。"
)


class Runner:
    def __init__(self, proto: Protocol, *, width: int = 320, height: int = 180,
                 source: str = "fake", fps: float = 8.0, threshold: int = 14,
                 source_factory=None):
        self.proto = proto
        self._source_factory = source_factory or create_source
        self.defaults = {
            "source": source,
            "fps": max(0.5, min(float(fps), MAX_FPS)),
            "width": width,
            "height": height,
            "threshold": threshold,
        }
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._source = None
        self.state = "stopped"
        self.started_at = 0.0
        self.frames = 0
        self.diffs = 0
        self.dropped = 0
        self.capture_ms = 0.0
        self.diff_ms = 0.0
        self.fps_actual = 0.0
        self.loop_lag_ms = 0.0
        self._latest: Optional[Frame] = None
        self._snapshot_source = None
        self.active_source: Optional[str] = None
        self._last_emit = 0.0
        self._frame_times: list[float] = []
        self.privacy_blacklist = sanitize_blacklist(DEFAULT_BLACKLIST)
        self.privacy_blocked = False
        self.privacy_title = ""
        self._privacy_last_emit = 0.0
        self.last_foreground = {"hwnd": 0, "pid": 0, "title": ""}

    # ── public API ───────────────────────────────────────────────────────

    def start(self, source_name: Optional[str] = None, fps: Optional[float] = None,
              privacy_blacklist=None) -> dict:
        with self._lock:
            if self.state in ("running", "starting"):
                raise ProtocolError("E_STATE", f"vision is already {self.state}", "先 vision.stop 再 start")
            if privacy_blacklist is not None:
                self.privacy_blacklist = sanitize_blacklist(privacy_blacklist)
            self.privacy_blocked = False
            self.privacy_title = ""
            self._privacy_last_emit = 0.0
            name = source_name or self.defaults["source"]
            rate = float(fps if fps is not None else self.defaults["fps"])
            if not (0.5 <= rate <= MAX_FPS):
                raise ProtocolError("E_PROTO", f"fps must be within 0.5..{MAX_FPS}")
            try:
                source = self._source_factory(
                    name,
                    width=self.defaults["width"],
                    height=self.defaults["height"],
                )
            except SourceUnavailable as error:
                raise ProtocolError(error.code, str(error), error.hint) from error
            except (ValueError, TypeError) as error:
                raise ProtocolError("E_PROTO", str(error)) from error

            self._source = source
            self.active_source = name
            if self._snapshot_source is not None:
                try:
                    self._snapshot_source.close()
                except Exception:  # noqa: BLE001
                    pass
                self._snapshot_source = None
            self._stop = threading.Event()
            self._frames = 0
            self.diffs = 0
            self.frames = 0
            self.capture_ms = 0.0
            self.diff_ms = 0.0
            self.fps_actual = 0.0
            self._frame_times = []
            self.started_at = time.time()
            self.state = "running"
            self._thread = threading.Thread(
                target=self._loop, args=(rate,), name="vhcu-vision", daemon=True
            )
            self._thread.start()
            self.proto.event("vision.started", source=name, fps=rate)
            return {"source": name, "fps": rate}

    def stop(self, reason: str = "cmd") -> dict:
        with self._lock:
            if self.state not in ("running", "starting"):
                raise ProtocolError("E_STATE", f"vision is not running (state={self.state})")
            self.state = "stopping"
            self._stop.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        with self._lock:
            self._thread = None
            self.state = "stopped"
            self.active_source = None
            self._latest = None
        if self._snapshot_source is not None:
            try:
                self._snapshot_source.close()
            except Exception:  # noqa: BLE001
                pass
            self._snapshot_source = None
        self.proto.event("vision.stopped", reason=reason)
        return {"stopped": True}

    def status(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "source": getattr(self._source, "name", None),
                "frames": self.frames,
                "diffs": self.diffs,
                "dropped": self.dropped,
                "fps": round(self.fps_actual, 2),
                "captureMs": round(self.capture_ms, 3),
                "diffMs": round(self.diff_ms, 3),
                "uptimeMs": int((time.time() - self.started_at) * 1000) if self.started_at else 0,
                "privacy": {
                    "blocked": self.privacy_blocked,
                    "title": self.privacy_title,
                    "blacklist": list(self.privacy_blacklist),
                },
            }

    def configure(self, privacy_blacklist=None) -> dict:
        """Live-update the privacy blacklist without restarting capture."""
        with self._lock:
            if privacy_blacklist is not None:
                self.privacy_blacklist = sanitize_blacklist(privacy_blacklist)
            return {
                "privacyBlacklist": list(self.privacy_blacklist),
                "blocked": self.privacy_blocked,
                "title": self.privacy_title,
            }

    def _privacy_should_block(self) -> bool:
        """Return True and emit vision.blocked while a sensitive title is active."""
        if not self.privacy_blacklist:
            if self.privacy_blocked:
                self.privacy_blocked = False
                self.privacy_title = ""
                self.proto.event("vision.blocked", blocked=False, title="", reason="resumed")
                self._privacy_last_emit = time.time()
            return False
        title = foreground_title()
        blocked = is_sensitive_title(title, self.privacy_blacklist)
        now = time.time()
        if blocked:
            changed = title != self.privacy_title or not self.privacy_blocked
            if changed or now - self._privacy_last_emit >= 5.0:
                self.proto.event("vision.blocked", blocked=True, title=title, reason="sensitive-title")
                self._privacy_last_emit = now
            self.privacy_blocked = True
            self.privacy_title = title
            return True
        if self.privacy_blocked:
            self.privacy_blocked = False
            self.privacy_title = title
            self.proto.event("vision.blocked", blocked=False, title=title, reason="resumed")
            self._privacy_last_emit = now
        return False

    def snapshot_values(self) -> dict:
        values = self.status()
        values["vision"] = self.state
        return values

    def latest_frame(self) -> Optional[Frame]:
        with self._lock:
            return self._latest

    # ── capture loop ─────────────────────────────────────────────────────

    def capture_now(self, allow_unprotected: bool = False) -> dict:
        """Grab one fresh full frame (even at 1fps) and publish it as the latest frame.

        Used by vhcu_get_full_frame: a new window can appear between two 1fps
        diff ticks, so the model needs a ground-truth snapshot on demand.

        Sizing rule: capture at the ACTIVE source's native resolution. The
        runner's `defaults` are 320x180 (the diff canvas), and using them here
        was the P6 bug that cropped full frames to the top-left corner.

        P22.4 privacy: when a sensitive window is foreground and the caller has
        NOT enabled the high-risk bypass, never grab its pixels and never build
        any image frame. Invalidate the cached latest frame and raise a fatal
        E_PRIVACY_BLOCKED so the model cannot mistake a black image for a
        working screenshot and fall back to an external screenshot tool.
        """
        blocked = self._privacy_should_block()
        with self._lock:
            name = self.active_source or self.defaults["source"]
            latest = self._latest
            source = self._snapshot_source
        if blocked and not allow_unprotected:
            with self._lock:
                # Hard privacy: no old frame may be served by /frame either.
                self._latest = None
                self.last_foreground = foreground_identity()
            raise ProtocolError(
                "E_PRIVACY_BLOCKED",
                PRIVACY_BLOCKED_MESSAGE,
                PRIVACY_BLOCKED_HINT,
                fatal=True,
            )
        width = getattr(latest, "width", None) or getattr(self._source, "width", None) or 1920
        height = getattr(latest, "height", None) or getattr(self._source, "height", None) or 1080
        started = time.perf_counter()
        if source is not None and getattr(source, "name", None) != name:
            try:
                source.close()
            except Exception:  # noqa: BLE001
                pass
            source = None
        if source is None:
            # fake honours width/height; mss always captures the real monitor.
            width = latest.width if latest is not None else self.defaults["width"]
            height = latest.height if latest is not None else self.defaults["height"]
            try:
                source = create_source(name, width=width, height=height)
            except SourceUnavailable as error:
                raise ProtocolError(error.code, str(error), error.hint) from error
            with self._lock:
                self._snapshot_source = source
        try:
            frame = source.grab()
        except CaptureError as error:
            raise ProtocolError("E_CAPTURE", str(error)) from error
        foreground = foreground_identity()
        with self._lock:
            self._latest = frame
            self.last_foreground = foreground
        return {
            "width": frame.width,
            "height": frame.height,
            "source": name,
            "captureMs": round((time.perf_counter() - started) * 1000.0, 3),
            "capturedAt": int(time.time() * 1000),
            "privacyBlocked": bool(blocked),
            "masked": False,
            "unprotected": bool(blocked and allow_unprotected),
            "foreground": foreground,
        }

    def _loop(self, fps: float) -> None:
        interval = 1.0 / fps
        previous: Optional[Frame] = None
        consecutive_errors = 0
        next_at = time.perf_counter()
        while not self._stop.is_set():
            started = time.perf_counter()
            if self._privacy_should_block():
                # Hard privacy: do not grab pixels and invalidate any cached
                # frame so /frame cannot serve the previous sensitive screen.
                previous = None
                with self._lock:
                    self._latest = None
                time.sleep(interval)
                continue
            try:
                frame = self._source.grab()
            except CaptureError as error:
                consecutive_errors += 1
                self.proto.error("E_CAPTURE", str(error), fatal=False)
                if consecutive_errors >= 3:
                    with self._lock:
                        self.state = "stopped"
                    self.proto.event("vision.stopped", reason="capture-error")
                    return
                time.sleep(min(1.0, interval * consecutive_errors))
                continue
            capture_ms = (time.perf_counter() - started) * 1000.0
            consecutive_errors = 0
            foreground = foreground_identity()

            result = compare(previous, frame, threshold=self.defaults["threshold"])
            previous = frame
            with self._lock:
                self.frames += 1
                self.capture_ms = capture_ms
                self.diff_ms = result.diff_ms
                self._latest = frame
                self.last_foreground = foreground
                if result.rois:
                    self.diffs += 1
                self._frame_times.append(started)
                if len(self._frame_times) > 60:
                    self._frame_times = self._frame_times[-60:]
                if len(self._frame_times) >= 2:
                    span = self._frame_times[-1] - self._frame_times[0]
                    if span > 0:
                        self.fps_actual = (len(self._frame_times) - 1) / span

            if result.rois:
                rois = [roi.as_dict(index + 1) for index, roi in enumerate(result.rois)]
                try:
                    self.proto.event(
                        "vision.diff",
                        screen={"width": frame.width, "height": frame.height, "scale": frame.scale},
                        rois=rois,
                        foreground=foreground,
                        stats={
                            "frame": self.frames,
                            "captureMs": round(capture_ms, 3),
                            "diffMs": round(result.diff_ms, 3),
                            "changedCells": result.changed_cells,
                            "cellTotal": result.cell_total,
                        },
                    )
                except Exception as error:  # noqa: BLE001
                    with self._lock:
                        self.dropped += 1
                    print(f"vhcu-daemon: diff emit failed: {error}", flush=True)

            next_at += interval
            now = time.perf_counter()
            with self._lock:
                self.loop_lag_ms = max(0.0, (now - next_at) * 1000.0)
            sleep_for = next_at - now
            if sleep_for > 0:
                self._stop.wait(sleep_for)
            else:
                next_at = now  # fell behind: do not accumulate debt


__all__ = ["MAX_FPS", "Runner", "available_sources"]
