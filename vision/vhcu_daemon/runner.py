"""Vision runtime: owns the capture thread, stats and the latest frame."""

from __future__ import annotations

import contextlib
import gc
import threading
import time
from dataclasses import replace
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
from .sources import CaptureError, Frame, SourceUnavailable, available_sources, create_source, privacy_frame

MAX_FPS = 15
# P1 8.7: on-demand frames stay addressable by frame_seq long enough for the
# browser to pull the signed URL. Bounded so memory cannot grow with capture.
MAX_RECENT_FRAMES = 4
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
        # Target-window frames keyed by their unique frame_seq. A 1fps full
        # screen tick must never overwrite the frame a capture_target URL binds.
        self._target_frames: dict[int, Frame] = {}
        self._target_frame_order: list[int] = []
        # P1 8.7: recent on-demand frames keyed by frame_seq, so a signed
        # full-frame URL keeps pointing at the pixels it was created for.
        self._recent_frames: dict[int, Frame] = {}
        self._recent_order: list[int] = []
        # P1 8.8: virtual cursor overlay, attached by the daemon after wiring.
        self._overlay = None
        self._snapshot_source = None
        self._capture_seq = 0
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
            alive = thread is not None and thread.is_alive()
            if alive:
                # The grab may still be inside a blocking GDI/mss call. Do not
                # close the source under it; keep the runner in a terminal
                # stopping state so a new start cannot race the old thread.
                self.state = "stopping"
                self._thread = thread
                source = None
            else:
                self._thread = None
                self.state = "stopped"
                self.active_source = None
                self._clear_frame_caches_locked()
                source = self._source
                self._source = None
        if source is not None:
            try:
                source.close()
            except Exception:  # noqa: BLE001
                pass
        if not alive and self._snapshot_source is not None:
            try:
                self._snapshot_source.close()
            except Exception:  # noqa: BLE001
                pass
            self._snapshot_source = None
        self.proto.event("vision.stopped", reason=reason if not alive else f"{reason}:thread-timeout")
        return {"stopped": not alive, **({"reason": "thread-timeout"} if alive else {})}

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
            with self._lock:
                self._clear_frame_caches_locked()
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

    def _clear_frame_caches_locked(self) -> None:
        """Drop every cached pixel frame. Caller must hold ``self._lock``."""
        self._latest = None
        self._target_frames.clear()
        self._target_frame_order = []
        self._recent_frames.clear()
        self._recent_order = []

    def latest_frame(self) -> Optional[Frame]:
        with self._lock:
            return self._latest

    def attach_overlay(self, overlay) -> None:
        """Attach the virtual cursor overlay (decoration; may be None)."""
        self._overlay = overlay

    @contextlib.contextmanager
    def capture_guard(self):
        """Hide the cursor overlay for exactly one capture, then restore it.

        P1 8.8: the overlay is a topmost layered window, so mss/BitBlt and
        win_capture would bake the cursor triangle into the screenshot. Only
        model-facing captures use this guard; the background diff tick does not
        (hiding at 8-15 fps would visibly strobe the cursor).
        """
        overlay = self._overlay
        if overlay is not None:
            try:
                overlay.hide_sync()
            except Exception:  # noqa: BLE001 - overlay must never break capture
                pass
        try:
            yield
        finally:
            if overlay is not None:
                # Always restore: a timed-out hide may still land later, and a
                # permanently invisible cursor would be a UX regression.
                try:
                    overlay.restore_after_capture()
                except Exception:  # noqa: BLE001
                    pass

    def frame_by_seq(self, frame_seq: int) -> Optional[Frame]:
        try:
            seq = int(frame_seq)
        except (TypeError, ValueError):
            return None
        with self._lock:
            frame = self._target_frames.get(seq)
            if frame is None:
                frame = self._recent_frames.get(seq)
            return frame

    def _publish_frame(self, frame: Frame) -> None:
        """Publish latest, retain bounded recent frames and target snapshots."""
        with self._lock:
            seq = int(frame.frame_seq)
            # A capture_now and the background loop can finish out of order.
            # The seq allocator is monotonic; never let an older publish
            # regress latest_frame.
            latest = self._latest
            if latest is None or seq >= int(getattr(latest, "frame_seq", 0) or 0):
                self._latest = frame
            self._recent_frames[seq] = frame
            self._recent_order.append(seq)
            while len(self._recent_order) > MAX_RECENT_FRAMES:
                old = self._recent_order.pop(0)
                self._recent_frames.pop(old, None)
            if getattr(frame, "target_hwnd", None):
                self._target_frames[seq] = frame
                self._target_frame_order.append(seq)
                while len(self._target_frame_order) > 8:
                    old = self._target_frame_order.pop(0)
                    self._target_frames.pop(old, None)

    def trim_resources(self) -> dict:
        """Drop cached frame data and close optional snapshot source on memory pressure."""
        with self._lock:
            had_frame = self._latest is not None
            self._latest = None
            self._target_frames.clear()
            self._target_frame_order = []
            self._recent_frames.clear()
            self._recent_order = []
            self._frame_times = []
            source = self._snapshot_source
            self._snapshot_source = None
        if source is not None:
            try:
                source.close()
            except Exception:  # noqa: BLE001
                pass
        gc.collect()
        return {"dropped_frame": had_frame, "snapshot_source_closed": source is not None}

    # ── capture loop ─────────────────────────────────────────────────────

    def _next_frame_seq(self) -> int:
        # Single allocator for every published frame. Sources must not carry
        # their own sequence: a fresh snapshot source would restart at 1 and
        # collide with the background loop's seq-keyed cache.
        with self._lock:
            self._capture_seq += 1
            return self._capture_seq

    def _capture_result(self, frame: Frame, started: float, foreground: dict,
                        *, source_name: str, target_hwnd: Optional[int] = None,
                        privacy_blocked: bool = False, privacy_mode: Optional[str] = None,
                        privacy_image: bool = False,
                        privacy_title: Optional[str] = None) -> dict:
        return {
            "width": frame.width,
            "height": frame.height,
            "source": source_name,
            "backend": frame.backend,
            "blank": bool(frame.blank),
            "occluded": bool(frame.occluded),
            "frame_seq": int(frame.frame_seq),
            "targetHwnd": int(target_hwnd) if target_hwnd else None,
            "captureMs": round((time.perf_counter() - started) * 1000.0, 3),
            "capturedAt": int(time.time() * 1000),
            "originX": int(getattr(frame, "origin_x", 0) or 0),
            "originY": int(getattr(frame, "origin_y", 0) or 0),
            "privacyBlocked": bool(privacy_blocked),
            "privacyMode": privacy_mode,
            "privacyImage": bool(privacy_image),
            "privacyTitle": privacy_title,
            "masked": False,
            "unprotected": bool(privacy_blocked and getattr(self, "_allow_unprotected", False)),
            "foreground": foreground,
        }

    def capture_window(self, target_hwnd: int, *, allow_unprotected: bool = False,
                       strict_occlusion: bool = False,
                       privacy_block_mode: str = "warning_image") -> dict:
        """Capture one HWND independently of the primary-monitor source."""
        try:
            hwnd = int(target_hwnd)
        except (TypeError, ValueError) as error:
            raise ProtocolError("E_PROTO", "target hwnd must be an integer") from error
        if hwnd <= 0:
            raise ProtocolError("E_PROTO", "target hwnd must be positive")
        blocked = self._privacy_should_block()
        self._allow_unprotected = bool(allow_unprotected)
        try:
            from .win_capture import window_title
            target_title = window_title(hwnd)
        except Exception:  # noqa: BLE001
            target_title = ""
        target_blocked = bool(target_title and is_sensitive_title(target_title, self.privacy_blacklist))
        if not allow_unprotected and (blocked or target_blocked):
            with self._lock:
                self._clear_frame_caches_locked()
            privacy_mode = _privacy_mode(privacy_block_mode)
            width, height = 640, 360
            try:
                from .win_capture import window_rect
                _left, _top, width, height = window_rect(hwnd)
            except Exception:  # noqa: BLE001
                pass
            frame = privacy_frame(privacy_mode, width, height)
            frame = replace(frame, frame_seq=self._next_frame_seq(), target_hwnd=hwnd)
            foreground = foreground_identity()
            self._publish_frame(frame)
            with self._lock:
                self.last_foreground = foreground
            return self._capture_result(
                frame, time.perf_counter(), foreground, source_name="privacy_guard",
                target_hwnd=hwnd, privacy_blocked=True, privacy_mode=privacy_mode,
                privacy_image=True, privacy_title=target_title or foreground.get("title", ""),
            )
        started = time.perf_counter()
        source = None
        try:
            try:
                source = self._source_factory(
                    "window",
                    target_hwnd=hwnd,
                    strict_occlusion=bool(strict_occlusion),
                )
            except SourceUnavailable as error:
                raise ProtocolError(error.code, str(error), error.hint) from error
            except (ValueError, TypeError) as error:
                raise ProtocolError("E_PROTO", str(error)) from error
            try:
                with self.capture_guard():
                    frame = source.grab()
            except CaptureError as error:
                raise ProtocolError(error.code, str(error), getattr(error, "hint", None)) from error
        finally:
            if source is not None:
                try:
                    source.close()
                except Exception:  # noqa: BLE001
                    pass
        frame = replace(frame, frame_seq=self._next_frame_seq())
        foreground = foreground_identity()
        self._publish_frame(frame)
        with self._lock:
            self.last_foreground = foreground
        return self._capture_result(
            frame,
            started,
            foreground,
            source_name="window",
            target_hwnd=hwnd,
            privacy_blocked=blocked,
        )

    def capture_now(self, allow_unprotected: bool = False,
                   privacy_block_mode: str = "warning_image") -> dict:
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
        self._allow_unprotected = bool(allow_unprotected)
        with self._lock:
            name = self.active_source or self.defaults["source"]
            latest = self._latest
            source = self._snapshot_source
        if blocked and not allow_unprotected:
            with self._lock:
                self._clear_frame_caches_locked()
            privacy_mode = _privacy_mode(privacy_block_mode)
            width = getattr(latest, "width", None) or getattr(self._source, "width", None) or 640
            height = getattr(latest, "height", None) or getattr(self._source, "height", None) or 360
            frame = privacy_frame(privacy_mode, width, height)
            frame = replace(frame, frame_seq=self._next_frame_seq())
            foreground = foreground_identity()
            self._publish_frame(frame)
            with self._lock:
                self.last_foreground = foreground
            return self._capture_result(
                frame, time.perf_counter(), foreground, source_name="privacy_guard",
                privacy_blocked=True, privacy_mode=privacy_mode, privacy_image=True,
                privacy_title=foreground.get("title", ""),
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
                source = self._source_factory(name, width=width, height=height)
            except SourceUnavailable as error:
                raise ProtocolError(error.code, str(error), error.hint) from error
            with self._lock:
                self._snapshot_source = source
        try:
            with self.capture_guard():
                frame = source.grab()
        except CaptureError as error:
            raise ProtocolError(error.code, str(error), getattr(error, "hint", None)) from error
        frame = replace(frame, frame_seq=self._next_frame_seq())
        foreground = foreground_identity()
        # P1 8.7: publish through the seq-indexed path so the just-signed
        # full-frame URL can still fetch exactly this frame later.
        self._publish_frame(frame)
        with self._lock:
            self.last_foreground = foreground
        return self._capture_result(
            frame,
            started,
            foreground,
            source_name=name,
            privacy_blocked=blocked,
        )

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
                    self._clear_frame_caches_locked()
                time.sleep(interval)
                continue
            try:
                # P1 8.8: background diff ticks are not model-facing images, so
                # they skip capture_guard() — hiding the overlay at 8-15 fps
                # would visibly strobe the virtual cursor. On-demand captures
                # (capture_now / capture_window) are guarded instead.
                frame = self._source.grab()
            except CaptureError as error:
                consecutive_errors += 1
                self.proto.error("E_CAPTURE", str(error), fatal=False)
                if consecutive_errors >= 3:
                    with self._lock:
                        self.state = "stopped"
                        source = self._source
                        self._source = None
                        self._clear_frame_caches_locked()
                    if source is not None:
                        try:
                            source.close()
                        except Exception:  # noqa: BLE001
                            pass
                    self.proto.event("vision.stopped", reason="capture-error")
                    return
                time.sleep(min(1.0, interval * consecutive_errors))
                continue
            capture_ms = (time.perf_counter() - started) * 1000.0
            consecutive_errors = 0
            frame = replace(frame, frame_seq=self._next_frame_seq())
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


def _privacy_mode(mode: str) -> str:
    return "black_image" if str(mode) == "black_image" else "warning_image"


__all__ = ["MAX_FPS", "Runner", "available_sources"]
