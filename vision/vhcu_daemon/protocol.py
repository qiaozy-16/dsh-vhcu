"""Wire protocol for the VHCU daemon (Python side).

Critical detail: the protocol owns a *duplicate* of the original stdout fd and
then points fd 1 at stderr. Any library that prints (mss, ctypes, warnings,
tracebacks) lands on stderr and can never corrupt a frame. Without this, one
stray ``print`` desynchronizes the host reader permanently.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Dict, Optional, TextIO

MAX_LINE_BYTES = 65536
PROTOCOL_VERSION = 1

ERROR_CODES = {
    "E_PROTO",
    "E_TIMEOUT",
    "E_BUSY",
    "E_DENIED",
    "E_UNAVAILABLE",
    "E_STATE",
    "E_CAPTURE",
    "E_PRIVACY_BLOCKED",
    "E_INTERNAL",
    "E_SEQ",
    "E_VERSION",
    "E_SHUTDOWN",
    "E_INTERRUPTED",
}


class ProtocolError(Exception):
    def __init__(self, code: str, message: str, hint: Optional[str] = None, fatal: bool = False):
        super().__init__(message)
        self.code = code if code in ERROR_CODES else "E_INTERNAL"
        self.hint = hint
        self.fatal = bool(fatal)


class Protocol:
    """Thread-safe NDJSON writer with monotonic seq."""

    def __init__(self, version: int = PROTOCOL_VERSION, stream: Optional[TextIO] = None):
        self.version = version
        self._lock = threading.Lock()
        self._seq = 0
        self._closed = False
        if stream is not None:
            self._out = stream
            return

        sys.stdout.flush()
        # Save the real stdout before hijacking fd 1. buffering=1 = line buffered
        # so a frame is visible to the reader immediately.
        saved = os.dup(1)
        os.dup2(2, 1)
        self._out = os.fdopen(saved, "w", encoding="utf-8", newline="\n", buffering=1)
        sys.stdout = sys.stderr

    @property
    def seq(self) -> int:
        return self._seq

    def send(self, mtype: str, **fields: Any) -> Dict[str, Any]:
        with self._lock:
            if self._closed:
                raise ProtocolError("E_SHUTDOWN", "protocol channel is closed")
            self._seq += 1
            frame: Dict[str, Any] = {
                "v": self.version,
                "seq": self._seq,
                "type": mtype,
                "ts": int(time.time() * 1000),
            }
            frame.update(fields)
            line = json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n"
            payload = line.encode("utf-8")
            if len(payload) > MAX_LINE_BYTES:
                raise ProtocolError("E_PROTO", f"frame is {len(payload)}B > {MAX_LINE_BYTES}B")
            self._out.write(line)
            self._out.flush()
            return frame

    def ready(self, **fields: Any) -> Dict[str, Any]:
        return self.send("ready", **fields)

    def ack(self, cmd_id: str, ok: bool, result: Optional[dict] = None,
            error: Optional[dict] = None) -> Dict[str, Any]:
        fields: Dict[str, Any] = {"cmdId": cmd_id, "ok": ok}
        if result is not None:
            fields["result"] = result
        if error is not None:
            fields["error"] = error
        return self.send("ack", **fields)

    def event(self, kind: str, **fields: Any) -> Dict[str, Any]:
        return self.send("event", kind=kind, **fields)

    def error(self, code: str, message: str, fatal: bool = False,
              hint: Optional[str] = None) -> Dict[str, Any]:
        fields: Dict[str, Any] = {"code": code, "message": message, "fatal": fatal}
        if hint:
            fields["hint"] = hint
        return self.send("error", **fields)

    def snapshot(self, values: dict) -> Dict[str, Any]:
        return self.send("snapshot", asOfSeq=self._seq, values=values)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            try:
                self._out.flush()
            except Exception:
                pass


def parse_line(line: bytes) -> Dict[str, Any]:
    """Parse one incoming line. Raises ProtocolError with a stable code."""
    if len(line) > MAX_LINE_BYTES:
        raise ProtocolError("E_PROTO", f"line exceeds {MAX_LINE_BYTES}B")
    try:
        frame = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("E_PROTO", f"invalid JSON ({error})") from error
    if not isinstance(frame, dict):
        raise ProtocolError("E_PROTO", "frame must be an object")
    for key in ("v", "seq", "type", "ts"):
        if key not in frame:
            raise ProtocolError("E_PROTO", f"missing {key}")
    if frame["v"] != PROTOCOL_VERSION:
        raise ProtocolError("E_VERSION", f"unsupported protocol {frame['v']}")
    if not isinstance(frame["seq"], int) or frame["seq"] < 1:
        raise ProtocolError("E_PROTO", "seq must be a positive integer")
    if frame["type"] not in ("cmd", "shutdown"):
        raise ProtocolError("E_PROTO", f"unsupported type {frame['type']}")
    if frame["type"] == "cmd":
        if not isinstance(frame.get("id"), str) or not frame["id"]:
            raise ProtocolError("E_PROTO", "cmd requires a non-empty id")
        if not isinstance(frame.get("op"), str):
            raise ProtocolError("E_PROTO", "cmd requires op")
        if not isinstance(frame.get("args", {}), dict):
            raise ProtocolError("E_PROTO", "cmd args must be an object")
    return frame
