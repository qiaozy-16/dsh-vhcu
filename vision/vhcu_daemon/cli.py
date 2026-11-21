"""Daemon entry point: wire protocol, heartbeat, watchdog, command loop.

Lifecycle rules:
  - READY is sent only after the image plane is listening.
  - Heartbeats continue even while vision is stopped (the daemon is alive).
  - stdin EOF means the parent is gone or closed the pipe: exit, do not linger.
  - A parent-pid watchdog exists because on Windows a crashed Electron parent
    does NOT kill its children; the child must notice and take itself down.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import signal
import sys
import threading
import time
import traceback
from typing import Optional

from . import PROTOCOL_VERSION, __version__
from .drivers import DriverFactory
from .element_registry import ElementActionService, ElementRegistry
from .hear import HearError
from .httpframe import HttpFrameServer
from .input import InputError
from .interrupts import InterruptController, InterruptError
from .paths import resource_paths
from .pidfile import reclaim_orphans, remove_record, write_record
from .privacy import foreground_identity, foreground_title, is_sensitive_title
from .protocol import MAX_LINE_BYTES, Protocol, ProtocolError, parse_line
from .runner import MAX_FPS, PRIVACY_BLOCKED_HINT, PRIVACY_BLOCKED_MESSAGE, Runner
from .uia_worker import UiaError, UiaWorker


def rss_bytes() -> Optional[int]:
    try:
        if os.name == "nt":
            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(counters),
                counters.cb,
            ):
                return int(counters.WorkingSetSize)
            return None
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(usage) * 1024
    except Exception:  # noqa: BLE001
        return None


def deadline_expired(frame: dict, now_ms: int | None = None) -> bool:
    """True when a command waited in the pipe past its host deadline.

    The host abandons a command after its timeout; executing it later would be a
    ghost action, so the daemon must refuse it before doing any work.
    """
    raw = frame.get("deadlineMs")
    if raw is None:
        return False
    try:
        deadline = int(raw)
    except (TypeError, ValueError):
        return False
    if deadline <= 0:
        return False
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return current > deadline


def parent_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if pid == os.getpid():
        return True
    if os.name == "nt":
        synchronize = 0x00100000
        wait_object_0 = 0
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) != wait_object_0
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Daemon:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.proto = Protocol(PROTOCOL_VERSION)
        self.interrupt = InterruptController()
        self.drivers = DriverFactory.create(
            emit=lambda kind, **fields: self.proto.event(kind, **fields),
            log=lambda message: print(f"vhcu-daemon: {message}", file=sys.stderr, flush=True),
            interrupt=self.interrupt,
        )
        self.runner = Runner(
            self.proto,
            width=args.width,
            height=args.height,
            source=args.source,
            fps=args.fps,
            threshold=args.threshold,
            source_factory=self.drivers.capture.create_source,
        )
        self.token = os.urandom(24).hex()
        self.server = HttpFrameServer(
            self.runner.latest_frame,
            self.token,
            port=0,
            max_width_default=args.max_width,
            on_interrupt=self._on_interrupt,
            frame_by_seq=self.runner.frame_by_seq,
        )
        self.started_at = time.time()
        self._pid_record = None
        self.input = self.drivers.input
        try:
            self.input.set_privacy_check(self._input_privacy_blocked)
        except Exception as error:  # noqa: BLE001 - input still has Node-side gate
            print(f"vhcu-daemon: input privacy hook unavailable: {error}", file=sys.stderr, flush=True)
        try:
            overlay = getattr(self.input, "overlay", None)
            if overlay is not None:
                overlay._on_error = lambda message: self.proto.event(
                    "cursor.overlay_error", message=str(message)
                )
            # P1 8.8: model-facing captures hide this overlay for the grab.
            self.runner.attach_overlay(overlay)
        except Exception:  # noqa: BLE001 - overlay diagnostics are optional
            pass
        self.hear = self.drivers.audio
        self.uia = UiaWorker()
        self.registry = ElementRegistry()
        self.actions = ElementActionService(
            self.registry, self.uia, capture_fn=self._capture_for_fingerprint
        )
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._closing = False
        self._memory_last_action = 0.0
        self._memory_soft = max(256, int(os.environ.get("VHCU_MEMORY_SOFT_MB", "1200"))) * 1024 * 1024
        self._memory_hard = max(self._memory_soft + 128, int(os.environ.get("VHCU_MEMORY_HARD_MB", "1600"))) * 1024 * 1024

    # ── startup ──────────────────────────────────────────────────────────

    def run(self) -> int:
        # P6 隐患 3: reclaim daemons orphaned by a dead host before binding.
        try:
            for outcome in reclaim_orphans():
                print(f"vhcu-daemon: pid reclaim {outcome}", file=sys.stderr, flush=True)
        except Exception as error:  # noqa: BLE001 - reclaim must never block startup
            print(f"vhcu-daemon: pid reclaim failed: {error}", file=sys.stderr, flush=True)
        try:
            self._pid_record = write_record(int(getattr(self.args, "parent_pid", 0) or 0))
        except Exception as error:  # noqa: BLE001
            print(f"vhcu-daemon: pid record failed: {error}", file=sys.stderr, flush=True)
        port = self.server.start()
        sources = self.drivers.capture.available_sources()
        # P1 隐患 1: clear any modifier/mouse button a previous crash left down.
        try:
            self.input.release_all()
        except Exception as error:  # noqa: BLE001 - startup must not fail on release
            print(f"vhcu-daemon: release_all at startup failed: {error}", file=sys.stderr, flush=True)
        self.proto.ready(
            proto=PROTOCOL_VERSION,
            pid=os.getpid(),
            port=port,
            token=self.token,
            capabilities={
                "vision": True,
                "sources": sources,
                "maxFps": MAX_FPS,
                "input": self.input.status(),
                "hear": self.hear.status(),
            },
            python={
                "version": platform.python_version(),
                "executable": sys.executable,
                "deps": {"mss": "mss" in sources},
            },
            startedAt=int(self.started_at * 1000),
        )
        self._start_heartbeat()
        if self.args.parent_pid > 0:
            self._start_watchdog()

        exit_code = 0
        try:
            exit_code = self._command_loop()
        except KeyboardInterrupt:
            exit_code = 0
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._safe_error("E_INTERNAL", "daemon crashed; see stderr", fatal=True)
            exit_code = 1
        finally:
            self.shutdown("daemon-exit")
        return exit_code

    # ── background loops ─────────────────────────────────────────────────

    def _start_heartbeat(self) -> None:
        def loop() -> None:
            next_at = time.perf_counter()
            while not self._heartbeat_stop.is_set():
                status = self.runner.status()
                try:
                    self.proto.send(
                        "heartbeat",
                        uptimeMs=int((time.time() - self.started_at) * 1000),
                        frames=status["frames"],
                        diffs=status["diffs"],
                        dropped=status["dropped"],
                        fps=status["fps"],
                        queueDepth=0,
                        loopLagMs=round(self.runner.loop_lag_ms, 2),
                        lastDiffMs=status["diffMs"],
                        rssBytes=rss_bytes(),
                        vision=status["state"],
                    )
                except Exception as error:  # noqa: BLE001
                    print(f"vhcu-daemon: heartbeat failed: {error}", flush=True)
                    return
                self._check_memory_watermark()
                next_at += 1.0
                sleep_for = next_at - time.perf_counter()
                if sleep_for > 0:
                    self._heartbeat_stop.wait(sleep_for)
                else:
                    next_at = time.perf_counter()

        self._heartbeat_thread = threading.Thread(target=loop, name="vhcu-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _check_memory_watermark(self) -> None:
        rss = rss_bytes()
        if not rss or rss < self._memory_soft:
            return
        now = time.time()
        if now - self._memory_last_action < 5.0:
            return
        self._memory_last_action = now
        print(
            f"vhcu-daemon: memory watermark {rss / 1024 / 1024:.0f}MB "
            f"(soft={self._memory_soft / 1024 / 1024:.0f}MB)",
            file=sys.stderr, flush=True,
        )
        try:
            self.proto.event(
                "daemon.memory_watermark",
                rssBytes=int(rss),
                softBytes=int(self._memory_soft),
                hardBytes=int(self._memory_hard),
            )
        except Exception:  # noqa: BLE001
            pass
        if rss >= self._memory_hard:
            try:
                self.runner.stop("memory-watermark")
            except Exception:  # noqa: BLE001
                pass
            try:
                self.input.overlay.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._safe_error("E_MEMORY", "daemon memory hard watermark exceeded; vision stopped", fatal=False)
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                self.runner.trim_resources()
            except Exception:  # noqa: BLE001
                pass

    def _start_watchdog(self) -> None:
        parent = self.args.parent_pid

        def loop() -> None:
            while not self._closing:
                if not parent_alive(parent):
                    self._safe_error("E_SHUTDOWN", f"parent pid {parent} is gone", fatal=True)
                    try:
                        self.runner.stop("parent-exit")
                    except Exception:  # noqa: BLE001
                        pass
                    self.server.stop()
                    os._exit(0)
                time.sleep(2.0)

        threading.Thread(target=loop, name="vhcu-watchdog", daemon=True).start()

    # ── command loop ─────────────────────────────────────────────────────

    def _command_loop(self) -> int:
        stdin = sys.stdin.buffer
        while True:
            raw = stdin.readline(MAX_LINE_BYTES + 1)
            if not raw:
                return 0
            if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
                self._drain_line(stdin)
                self.proto.error("E_PROTO", f"line exceeds {MAX_LINE_BYTES}B; dropped")
                continue
            if not raw.strip():
                continue
            try:
                frame = parse_line(raw)
            except ProtocolError as error:
                self.proto.error(error.code, str(error), hint=error.hint)
                continue

            if frame["type"] == "shutdown":
                # No ack: the host is already tearing the pipe down, and a frame
                # without a cmd id would be an invalid ack on the wire.
                return 0
            try:
                if not self._dispatch(frame):
                    return 0
            except ProtocolError as error:
                self._safe_ack_error(frame, error.code, str(error), error.hint, fatal=error.fatal)
            except Exception as error:  # noqa: BLE001
                traceback.print_exc()
                self._safe_ack_error(frame, "E_INTERNAL", f"{type(error).__name__}: {error}")
            except BaseException as error:  # noqa: BLE001 - never let one command kill the daemon
                traceback.print_exc()
                self._safe_ack_error(frame, "E_INTERNAL", f"{type(error).__name__}: {error}")

    def _dispatch(self, frame: dict) -> bool:
        if deadline_expired(frame):
            raise ProtocolError(
                "E_TIMEOUT",
                "command deadline expired before execution",
                "指令已过期，未执行；请重新发起该动作",
            )
        op = frame["op"]
        args = frame.get("args") or {}
        cmd_id = frame["id"]

        if op == "ping":
            self.proto.ack(cmd_id, True, {"pong": True, "pid": os.getpid()})
            return True
        if op == "sys.info":
            self.proto.ack(
                cmd_id,
                True,
                {
                    "pid": os.getpid(),
                    "python": platform.python_version(),
                    "executable": sys.executable,
                    "platform": platform.platform(),
                    "protocol": PROTOCOL_VERSION,
                    "daemon": __version__,
                    "sources": self.drivers.capture.available_sources(),
                    "cwd": os.getcwd(),
                    "input": self.input.status(),
                    "paths": resource_paths(),
                    "interrupt": self.interrupt.status(),
                    "uia": self.uia.status(),
                },
            )
            return True
        if op == "sys.active_window":
            identity = foreground_identity()
            title = identity.get("title", "")
            self.proto.ack(
                cmd_id,
                True,
                {
                    "title": title,
                    "active_window": title,
                    "hwnd": identity.get("hwnd", 0),
                    "pid": identity.get("pid", 0),
                    "ts": int(time.time() * 1000),
                },
            )
            return True
        if op == "uia.find":
            hwnd = args.get("hwnd")
            allow_unprotected = bool(args.get("allowUnprotected"))
            if hwnd is None:
                hwnd = foreground_identity().get("hwnd", 0)
            if not hwnd:
                raise ProtocolError("E_UNAVAILABLE", "no foreground window for UIA search")
            # P1 隐患 3: UIA discovery must obey the privacy blacklist too,
            # unless the user explicitly enabled the unprotected switch.
            self._assert_uia_privacy(int(hwnd), allow_unprotected=allow_unprotected)
            try:
                timeout_ms = int(args.get("timeoutMs") or 2500)
            except (TypeError, ValueError) as error:
                raise ProtocolError("E_PROTO", "uia.find timeoutMs must be an integer") from error
            locator = args.get("locator")
            options = args.get("options")

            # UIA/COM can block for seconds. Never hold the command loop:
            # the worker thread serializes COM, and this request thread only
            # writes the asynchronous ack when it finishes.
            def run_uia() -> None:
                try:
                    result = self.uia.find(hwnd, locator, options, timeout_ms=timeout_ms)
                    try:
                        result = self.registry.register_result(
                            result, locator or {}, options or {}
                        )
                    except Exception as error:  # noqa: BLE001
                        print(f"vhcu-daemon: element registry failed: {error}", file=sys.stderr, flush=True)
                    self.proto.ack(cmd_id, True, result)
                except UiaError as error:
                    self._safe_ack_error(cmd_id, error.code, str(error), error.hint)
                except Exception as error:  # noqa: BLE001
                    self._safe_ack_error(cmd_id, "E_UIA_FAILED", f"{type(error).__name__}: {error}")

            threading.Thread(target=run_uia, name="vhcu-uia-request", daemon=True).start()
            return True
        if op == "uia.click":
            element_id = str(args.get("elementId") or args.get("element_id") or "").strip()
            allow_unprotected = bool(args.get("allowUnprotected"))
            if not element_id:
                raise ProtocolError("E_PROTO", "uia.click needs element_id")
            # P1 隐患 3: resolve the token's hwnd and apply the same privacy gate
            # before any thread starts. A missing token is reported by
            # actions.click with its normal E_ELEMENT_NOT_FOUND taxonomy.
            try:
                record = self.registry.get(element_id)
            except Exception:  # noqa: BLE001 - defer the real error to actions.click
                record = None
            if record is not None:
                self._assert_uia_privacy(
                    int(record.hwnd), allow_unprotected=allow_unprotected
                )
            try:
                retries = int(args.get("retryMaxAttempts", args.get("retry_max_attempts", 3)))
            except (TypeError, ValueError) as error:
                raise ProtocolError("E_PROTO", "retryMaxAttempts must be an integer") from error

            def run_click() -> None:
                try:
                    result = self.actions.click(element_id, retries)
                    self.proto.ack(cmd_id, True, result)
                except UiaError as error:
                    self._safe_ack_error(cmd_id, error.code, str(error), error.hint)
                except Exception as error:  # noqa: BLE001
                    self._safe_ack_error(cmd_id, "E_UIA_FAILED", f"{type(error).__name__}: {error}")

            threading.Thread(target=run_click, name="vhcu-uia-click", daemon=True).start()
            return True
        if op == "uia.click_point":
            allow_unprotected = bool(args.get("allowUnprotected"))
            try:
                x = int(args.get("x"))
                y = int(args.get("y"))
            except (TypeError, ValueError) as error:
                raise ProtocolError("E_PROTO", "uia.click_point needs integer x and y") from error
            click_type = str(args.get("clickType") or args.get("click_type") or "click")
            # The target window is only known after the point lookup; the
            # foreground title is the best privacy signal available up front.
            foreground = foreground_identity()
            self._assert_uia_privacy(
                int(foreground.get("hwnd") or 0), allow_unprotected=allow_unprotected
            )

            def run_point() -> None:
                try:
                    result = self.uia.invoke_point(
                        x, y, click_type=click_type, timeout_ms=4000,
                        privacy_check=lambda target: self._uia_privacy_blocked(
                            target, allow_unprotected=allow_unprotected
                        ),
                    )
                    if result.get("method") == "coordinate":
                        center = result.get("center") or {}
                        cx = int(center.get("x", x))
                        cy = int(center.get("y", y))
                        clicked = self._input(
                            lambda: self.input.click(
                                cx,
                                cy,
                                humanize_profile=args.get("humanizeProfile"),
                                cursor_injection_mode="physical",
                                click_type=click_type,
                                allow_unprotected=allow_unprotected,
                            ),
                            args.get("expectedForeground"),
                            allow_unprotected=allow_unprotected,
                        )
                        result = {**result, "used": "sendinput", "clicked": clicked}
                    else:
                        result = {**result, "used": "invoke"}
                    self.proto.ack(cmd_id, True, result)
                except UiaError as error:
                    self._safe_ack_error(cmd_id, error.code, str(error), error.hint)
                except ProtocolError as error:
                    self._safe_ack_error(cmd_id, error.code, str(error), error.hint)
                except Exception as error:  # noqa: BLE001
                    self._safe_ack_error(cmd_id, "E_UIA_FAILED", f"{type(error).__name__}: {error}")

            threading.Thread(target=run_point, name="vhcu-uia-point", daemon=True).start()
            return True
        if op == "vision.start":
            result = self.runner.start(
                args.get("source"),
                args.get("fps"),
                args.get("privacyBlacklist"),
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "vision.configure":
            result = self.runner.configure(args.get("privacyBlacklist"))
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "vision.stop":
            result = self.runner.stop(str(args.get("reason") or "cmd"))
            try:
                self.input.overlay.close()
            except Exception:  # noqa: BLE001 - overlay is decoration only
                pass
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "vision.status":
            self.proto.ack(cmd_id, True, self.runner.status())
            return True
        if op == "vision.snapshot":
            self.proto.ack(cmd_id, True, self.runner.status())
            self.proto.snapshot(self.runner.snapshot_values())
            return True
        if op == "vision.capture":
            target_hwnd = args.get("targetHwnd", args.get("target_hwnd"))
            allow_unprotected = bool(args.get("allowUnprotected"))
            privacy_block_mode = str(args.get("privacyBlockMode") or "warning_image")
            if target_hwnd is not None:
                result = self.runner.capture_window(
                    target_hwnd,
                    allow_unprotected=allow_unprotected,
                    strict_occlusion=bool(args.get("strictOcclusion")),
                    privacy_block_mode=privacy_block_mode,
                )
            else:
                result = self.runner.capture_now(
                    allow_unprotected=allow_unprotected,
                    privacy_block_mode=privacy_block_mode,
                )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.click":
            allow_unprotected = bool(args.get("allowUnprotected"))
            try:
                x = int(args.get("x"))
                y = int(args.get("y"))
            except (TypeError, ValueError) as error:
                raise ProtocolError("E_PROTO", "input.click needs integer x and y") from error
            result = self._input(
                lambda: self.input.click(
                    x, y, args.get("scaleFactor"),
                    args.get("humanizeProfile") or args.get("humanize_profile"),
                    args.get("cursorInjectionMode") or args.get("cursor_injection_mode"),
                    args.get("targetHwnd") or args.get("target_hwnd"),
                    args.get("scaleX") if "scaleX" in args else args.get("scale_x"),
                    args.get("scaleY") if "scaleY" in args else args.get("scale_y"),
                    args.get("clickType") or args.get("click_type"),
                    allow_unprotected=allow_unprotected,
                ),
                args.get("expectedForeground"),
                allow_unprotected=allow_unprotected,
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.type":
            allow_unprotected = bool(args.get("allowUnprotected"))
            result = self._input(
                lambda: self.input.type_text(
                    str(args.get("text") or ""),
                    self._ime_shortcut(args),
                    bool(args.get("useClipboardFallback")),
                ),
                args.get("expectedForeground"),
                allow_unprotected=allow_unprotected,
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.hotkey":
            allow_unprotected = bool(args.get("allowUnprotected"))
            keys = args.get("keys")
            if not isinstance(keys, list):
                raise ProtocolError("E_PROTO", "input.hotkey needs a keys array", '例如 ["ctrl","s"]')
            result = self._input(
                lambda: self.input.key_press(keys, int(args.get("delayMs") or 0)),
                args.get("expectedForeground"),
                allow_unprotected=allow_unprotected,
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.scroll":
            allow_unprotected = bool(args.get("allowUnprotected"))
            try:
                amount = int(args.get("amount"))
            except (TypeError, ValueError) as error:
                raise ProtocolError("E_PROTO", "input.scroll needs an integer amount") from error
            result = self._input(
                lambda: self.input.scroll(
                    amount,
                    args.get("cursorInjectionMode") or args.get("cursor_injection_mode"),
                    args.get("targetHwnd") or args.get("target_hwnd"),
                    allow_unprotected=allow_unprotected,
                ),
                args.get("expectedForeground"),
                allow_unprotected=allow_unprotected,
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.draw_shape":
            allow_unprotected = bool(args.get("allowUnprotected"))
            try:
                start_x = int(args.get("start_x", args.get("startX")))
                start_y = int(args.get("start_y", args.get("startY")))
                end_x = int(args.get("end_x", args.get("endX")))
                end_y = int(args.get("end_y", args.get("endY")))
            except (TypeError, ValueError) as error:
                raise ProtocolError("E_PROTO", "input.draw_shape needs integer start/end x,y") from error
            result = self._input(
                lambda: self.input.draw_shape(
                    str(args.get("shape") or "line"),
                    start_x, start_y, end_x, end_y,
                    float(args.get("curve") or 0),
                    int(args.get("samples") or 160),
                    int(args.get("durationMs") or args.get("duration_ms") or 500),
                    args.get("humanizeProfile") or args.get("humanize_profile"),
                    args.get("cursorInjectionMode") or args.get("cursor_injection_mode"),
                    args.get("targetHwnd") or args.get("target_hwnd"),
                ),
                args.get("expectedForeground"),
                allow_unprotected=allow_unprotected,
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.drag":
            allow_unprotected = bool(args.get("allowUnprotected"))
            try:
                x1 = int(args.get("x1"))
                y1 = int(args.get("y1"))
                x2 = int(args.get("x2"))
                y2 = int(args.get("y2"))
            except (TypeError, ValueError) as error:
                raise ProtocolError("E_PROTO", "input.drag needs integer x1,y1,x2,y2") from error
            result = self._input(
                lambda: self.input.drag(
                    x1, y1, x2, y2,
                    args.get("humanizeProfile") or args.get("humanize_profile"),
                    args.get("cursorInjectionMode") or args.get("cursor_injection_mode"),
                    args.get("targetHwnd") or args.get("target_hwnd"),
                    allow_unprotected=allow_unprotected,
                ),
                args.get("expectedForeground"),
                allow_unprotected=allow_unprotected,
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.middle_drag":
            allow_unprotected = bool(args.get("allowUnprotected"))
            try:
                x1 = int(args.get("x1"))
                y1 = int(args.get("y1"))
                x2 = int(args.get("x2"))
                y2 = int(args.get("y2"))
            except (TypeError, ValueError) as error:
                raise ProtocolError("E_PROTO", "input.middle_drag needs integer x1,y1,x2,y2") from error
            result = self._input(
                lambda: self.input.middle_drag(
                    x1, y1, x2, y2,
                    args.get("humanizeProfile") or args.get("humanize_profile"),
                    args.get("cursorInjectionMode") or args.get("cursor_injection_mode"),
                    args.get("targetHwnd") or args.get("target_hwnd"),
                    allow_unprotected=allow_unprotected,
                ),
                args.get("expectedForeground"),
                allow_unprotected=allow_unprotected,
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.macro.open_notepad":
            allow_unprotected = bool(args.get("allowUnprotected"))
            result = self._input(
                lambda: self.input.macro_open_notepad_and_type(
                    str(args.get("text") or ""), self._ime_shortcut(args)
                ),
                args.get("expectedForeground"),
                allow_unprotected=allow_unprotected,
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.macro.save":
            allow_unprotected = bool(args.get("allowUnprotected"))
            result = self._input(
                lambda: self.input.macro_save_file(),
                args.get("expectedForeground"),
                allow_unprotected=allow_unprotected,
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.macro.run":
            allow_unprotected = bool(args.get("allowUnprotected"))
            steps = args.get("steps")
            if not isinstance(steps, list):
                raise ProtocolError("E_PROTO", "input.macro.run needs a steps array")
            result = self._input(
                lambda: self.input.run_macro(
                    steps, args.get("variables"), args.get("scaleFactor"),
                    args.get("humanizeProfile") or args.get("humanize_profile"),
                    args.get("cursorInjectionMode") or args.get("cursor_injection_mode"),
                    args.get("scaleX") if "scaleX" in args else args.get("scale_x"),
                    args.get("scaleY") if "scaleY" in args else args.get("scale_y"),
                    args.get("originX") if "originX" in args else args.get("origin_x"),
                    args.get("originY") if "originY" in args else args.get("origin_y"),
                ),
                args.get("expectedForeground"),
                allow_unprotected=allow_unprotected,
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "hear.start":
            result = self._hear(self.hear.start)
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "hear.stop":
            result = self._hear(self.hear.stop)
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "hear.status":
            self.proto.ack(cmd_id, True, self.hear.status())
            return True
        if op == "hear.configure":
            result = self.hear.configure(args.get("level"), args.get("disableFilter"))
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "shutdown":
            self.proto.ack(cmd_id, True, {"bye": True})
            return False
        raise ProtocolError("E_DENIED", f"unknown op {op!r}", "命令不在白名单内")

    # ── helpers ──────────────────────────────────────────────────────────

    def _on_interrupt(self, reason: str, clear: bool = False) -> dict:
        """Called by the loopback HTTP thread while the command loop may be blocked."""
        if clear:
            self.interrupt.clear()
            self.proto.event("daemon.interrupt", cleared=True, reason=reason)
        else:
            self.interrupt.request(reason or "host-interrupt")
            self.proto.event("daemon.interrupt", cleared=False, reason=self.interrupt.reason)
        return {"interrupt": self.interrupt.status()}

    @staticmethod
    def _ime_shortcut(args: dict) -> str:
        value = str(args.get("imeShortcut") or "ctrl+space")
        return value if value in ("ctrl+space", "shift", "none") else "ctrl+space"

    def _input(self, action, expected_foreground=None, allow_unprotected: bool = False):
        """Run one input action, mapping driver errors onto the wire taxonomy."""
        try:
            hwnd = 0
            if isinstance(expected_foreground, dict):
                try:
                    hwnd = int(expected_foreground.get("hwnd") or 0)
                except (TypeError, ValueError):
                    hwnd = 0
            guard = getattr(self.input, "assert_privacy", None)
            if callable(guard) and not allow_unprotected:
                guard(hwnd)
            if expected_foreground:
                self.input.assert_foreground(expected_foreground)
            return action()
        except InterruptError as error:
            raise ProtocolError(error.code, str(error), error.hint) from error
        except InputError as error:
            raise ProtocolError(error.code, str(error), error.hint) from error

    def _input_privacy_blocked(self, hwnd: int = 0) -> bool:
        """Fail-closed checker used by InputController before any injection."""
        checker = getattr(self.runner, "_privacy_should_block", None)
        if not callable(checker):
            return True
        try:
            if bool(checker()):
                return True
        except Exception:  # noqa: BLE001 - a broken privacy check means deny
            return True
        target = int(hwnd or 0)
        if target <= 0:
            return False
        try:
            from .win_capture import window_title
        except Exception:  # noqa: BLE001
            return True
        try:
            title = str(window_title(target) or "")
        except Exception:  # noqa: BLE001
            return True
        if not title:
            return True
        return bool(is_sensitive_title(title, self.runner.privacy_blacklist))

    @staticmethod
    def _hear(action):
        """Run one hear action, mapping HearError onto the wire error taxonomy."""
        try:
            return action()
        except HearError as error:
            raise ProtocolError(error.code, str(error), error.hint) from error

    def _privacy_blocked(self) -> bool:
        """Defensive privacy check for internal fingerprint captures.

        Runner owns the real `_privacy_should_block`; Daemon must never call a
        missing method and turn a configuration error into a fake stale token.
        If the checker itself is unavailable/broken, fall back to the last
        recorded flag rather than blocking normal clicks.
        """
        checker = getattr(self.runner, "_privacy_should_block", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception as error:  # noqa: BLE001
                print(f"vhcu-daemon: privacy check failed: {error}", file=sys.stderr, flush=True)
        return bool(getattr(self.runner, "privacy_blocked", False))

    def _uia_privacy_blocked(self, hwnd: int, allow_unprotected: bool = False) -> bool:
        """Boolean callback for UiaWorker; fail closed on any check failure."""
        try:
            self._assert_uia_privacy(int(hwnd), allow_unprotected=allow_unprotected)
            return False
        except ProtocolError:
            return True

    def _assert_uia_privacy(self, hwnd: int, allow_unprotected: bool = False) -> None:
        """P1 隐患 3: block UIA discovery/invoke on a blacklisted target window.

        The title is read directly from the target HWND (not the foreground) so
        an explicit hwnd cannot bypass the foreground-based screenshot guard.
        Privacy is fail-closed: if the title cannot be read, UIA is denied.
        ``allow_unprotected`` is set only when the user explicitly enabled the
        high-risk privacy-off switch in settings; it preserves that choice, not
        a new default.
        """
        if allow_unprotected:
            return
        if int(hwnd or 0) <= 0:
            raise ProtocolError(
                "E_PRIVACY_BLOCKED",
                "无法确定 UIA 目标窗口，已按隐私保护阻断",
                "激活目标窗口后重试；禁止在无法校验窗口标题时使用 UIA",
            )
        try:
            from .win_capture import window_title
        except Exception as error:  # noqa: BLE001 - no title reader => fail closed
            raise ProtocolError(
                "E_PRIVACY_BLOCKED",
                f"UIA 隐私检查不可用：{error}",
                PRIVACY_BLOCKED_HINT,
            ) from error
        try:
            title = str(window_title(int(hwnd)) or "")
        except Exception as error:  # noqa: BLE001 - closed window / API failure
            raise ProtocolError(
                "E_PRIVACY_BLOCKED",
                f"无法读取目标窗口标题，已按隐私保护阻断：{error}",
                PRIVACY_BLOCKED_HINT,
            ) from error
        if not title:
            raise ProtocolError(
                "E_PRIVACY_BLOCKED",
                "目标窗口标题为空，无法确认隐私状态，已阻断 UIA",
                PRIVACY_BLOCKED_HINT,
            )
        if is_sensitive_title(title, self.runner.privacy_blacklist):
            raise ProtocolError(
                "E_PRIVACY_BLOCKED",
                PRIVACY_BLOCKED_MESSAGE,
                PRIVACY_BLOCKED_HINT,
            )

    def _capture_for_fingerprint(self, hwnd: int):
        """Fingerprint capture must never touch a sensitive target window."""
        try:
            from .win_capture import capture_window, window_title
        except Exception as error:  # noqa: BLE001
            raise RuntimeError(f"capture helper unavailable: {error}") from error
        if self._privacy_blocked():
            raise RuntimeError("privacy blacklist blocks fingerprint capture")
        title = window_title(hwnd)
        if title and is_sensitive_title(title, self.runner.privacy_blacklist):
            raise RuntimeError("target window is privacy-sensitive")
        try:
            with self.runner.capture_guard():
                return capture_window(hwnd)
        except Exception as error:  # noqa: BLE001
            raise RuntimeError(f"capture_window failed: {error}") from error

    def _safe_ack_error(self, frame: dict | str, code: str, message: str, hint: Optional[str] = None,
                        fatal: bool = False) -> None:
        try:
            error = {"code": code, "message": message, "hint": hint}
            if fatal:
                error["fatal"] = True
            cmd_id = frame if isinstance(frame, str) else frame.get("id", "")
            self.proto.ack(cmd_id, False, error=error)
        except Exception:  # noqa: BLE001
            pass

    def _safe_error(self, code: str, message: str, fatal: bool = False) -> None:
        try:
            self.proto.error(code, message, fatal=fatal)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _drain_line(stdin) -> None:
        while True:
            chunk = stdin.readline(MAX_LINE_BYTES)
            if not chunk or chunk.endswith(b"\n"):
                return

    def shutdown(self, reason: str) -> None:
        if self._closing:
            return
        self._closing = True
        self._heartbeat_stop.set()
        # P1 隐患 1: never leave a drag/modifier pressed after a clean shutdown.
        try:
            self.input.release_all()
        except Exception:  # noqa: BLE001 - shutdown must continue
            pass
        try:
            self.hear.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.runner.state in ("running", "starting"):
                self.runner.stop(reason)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.registry.clear()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.uia.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.input.overlay.close()
        except Exception:  # noqa: BLE001
            pass
        if getattr(self, "_pid_record", None):
            try:
                remove_record(self._pid_record)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.proto.event("daemon.bye", reason=reason)
        except Exception:  # noqa: BLE001
            pass
        self.server.stop()
        self.proto.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vhcu_daemon", description="VHCU vision daemon (P0)")
    parser.add_argument("--parent-pid", type=int, default=0, help="host pid to watch (0 = disabled)")
    parser.add_argument("--protocol", type=int, default=PROTOCOL_VERSION)
    parser.add_argument("--source", choices=["fake", "mss"], default="fake")
    parser.add_argument("--fps", type=float, default=8.0)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=180)
    parser.add_argument("--threshold", type=int, default=14)
    parser.add_argument("--max-width", type=int, default=960, help="preview PNG max width")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.protocol != PROTOCOL_VERSION:
        # stdout is still the raw channel here: report on stderr, not as a frame.
        print(f"vhcu-daemon: unsupported protocol {args.protocol}", file=sys.stderr)
        return 2

    daemon = Daemon(args)

    def _graceful(signum, _frame):
        try:
            daemon.interrupt.request(f"signal-{signum}")
            daemon.shutdown(f"signal-{signum}")
        finally:
            os._exit(0)

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _graceful)
            except (ValueError, OSError):
                pass

    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
