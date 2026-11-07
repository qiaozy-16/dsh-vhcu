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
from .hear import HearError, HearService
from .httpframe import HttpFrameServer
from .input import InputController, InputError
from .privacy import foreground_title
from .protocol import MAX_LINE_BYTES, Protocol, ProtocolError, parse_line
from .runner import MAX_FPS, Runner
from .sources import available_sources


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
        self.runner = Runner(
            self.proto,
            width=args.width,
            height=args.height,
            source=args.source,
            fps=args.fps,
            threshold=args.threshold,
        )
        self.token = os.urandom(24).hex()
        self.server = HttpFrameServer(
            self.runner.latest_frame,
            self.token,
            port=0,
            max_width_default=args.max_width,
        )
        self.started_at = time.time()
        self.input = InputController()
        self.hear = HearService(
            lambda kind, **fields: self.proto.event(kind, **fields),
            log=lambda message: print(f"vhcu-daemon: {message}", file=sys.stderr, flush=True),
        )
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._closing = False

    # ── startup ──────────────────────────────────────────────────────────

    def run(self) -> int:
        port = self.server.start()
        sources = available_sources()
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
                next_at += 1.0
                sleep_for = next_at - time.perf_counter()
                if sleep_for > 0:
                    self._heartbeat_stop.wait(sleep_for)
                else:
                    next_at = time.perf_counter()

        self._heartbeat_thread = threading.Thread(target=loop, name="vhcu-heartbeat", daemon=True)
        self._heartbeat_thread.start()

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

    def _dispatch(self, frame: dict) -> bool:
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
                    "sources": available_sources(),
                    "cwd": os.getcwd(),
                    "input": self.input.status(),
                },
            )
            return True
        if op == "sys.active_window":
            title = foreground_title()
            self.proto.ack(
                cmd_id,
                True,
                {"title": title, "active_window": title, "ts": int(time.time() * 1000)},
            )
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
            result = self.runner.capture_now(allow_unprotected=bool(args.get("allowUnprotected")))
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.click":
            try:
                x = int(args.get("x"))
                y = int(args.get("y"))
            except (TypeError, ValueError) as error:
                raise ProtocolError("E_PROTO", "input.click needs integer x and y") from error
            result = self._input(
                lambda: self.input.click(x, y, args.get("scaleFactor")),
                args.get("expectedForeground"),
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.type":
            result = self._input(
                lambda: self.input.type_text(
                    str(args.get("text") or ""),
                    self._ime_shortcut(args),
                    bool(args.get("useClipboardFallback")),
                ),
                args.get("expectedForeground"),
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.hotkey":
            keys = args.get("keys")
            if not isinstance(keys, list):
                raise ProtocolError("E_PROTO", "input.hotkey needs a keys array", '例如 ["ctrl","s"]')
            result = self._input(
                lambda: self.input.key_press(keys, int(args.get("delayMs") or 0)),
                args.get("expectedForeground"),
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.scroll":
            try:
                amount = int(args.get("amount"))
            except (TypeError, ValueError) as error:
                raise ProtocolError("E_PROTO", "input.scroll needs an integer amount") from error
            result = self._input(lambda: self.input.scroll(amount), args.get("expectedForeground"))
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.drag":
            try:
                x1 = int(args.get("x1"))
                y1 = int(args.get("y1"))
                x2 = int(args.get("x2"))
                y2 = int(args.get("y2"))
            except (TypeError, ValueError) as error:
                raise ProtocolError("E_PROTO", "input.drag needs integer x1,y1,x2,y2") from error
            result = self._input(
                lambda: self.input.drag(x1, y1, x2, y2),
                args.get("expectedForeground"),
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.middle_drag":
            try:
                x1 = int(args.get("x1"))
                y1 = int(args.get("y1"))
                x2 = int(args.get("x2"))
                y2 = int(args.get("y2"))
            except (TypeError, ValueError) as error:
                raise ProtocolError("E_PROTO", "input.middle_drag needs integer x1,y1,x2,y2") from error
            result = self._input(
                lambda: self.input.middle_drag(x1, y1, x2, y2),
                args.get("expectedForeground"),
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.macro.open_notepad":
            result = self._input(
                lambda: self.input.macro_open_notepad_and_type(
                    str(args.get("text") or ""), self._ime_shortcut(args)
                ),
                args.get("expectedForeground"),
            )
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.macro.save":
            result = self._input(lambda: self.input.macro_save_file(), args.get("expectedForeground"))
            self.proto.ack(cmd_id, True, result)
            return True
        if op == "input.macro.run":
            steps = args.get("steps")
            if not isinstance(steps, list):
                raise ProtocolError("E_PROTO", "input.macro.run needs a steps array")
            result = self._input(
                lambda: self.input.run_macro(steps, args.get("variables"), args.get("scaleFactor")),
                args.get("expectedForeground"),
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

    @staticmethod
    def _ime_shortcut(args: dict) -> str:
        value = str(args.get("imeShortcut") or "ctrl+space")
        return value if value in ("ctrl+space", "shift", "none") else "ctrl+space"

    def _input(self, action, expected_foreground=None):
        """Run one input action, mapping InputError onto the wire error taxonomy."""
        try:
            if expected_foreground:
                self.input.assert_foreground(expected_foreground)
            return action()
        except InputError as error:
            raise ProtocolError(error.code, str(error), error.hint) from error

    @staticmethod
    def _hear(action):
        """Run one hear action, mapping HearError onto the wire error taxonomy."""
        try:
            return action()
        except HearError as error:
            raise ProtocolError(error.code, str(error), error.hint) from error

    def _safe_ack_error(self, frame: dict, code: str, message: str, hint: Optional[str] = None,
                        fatal: bool = False) -> None:
        try:
            error = {"code": code, "message": message, "hint": hint}
            if fatal:
                error["fatal"] = True
            self.proto.ack(frame.get("id", ""), False, error=error)
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
