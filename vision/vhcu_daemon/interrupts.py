"""Cooperative interrupt channel for long-running daemon work.

The stdio command loop is synchronous: while a macro or a Hear inference is
running it cannot read another command. The loopback HTTP plane is served by a
separate thread, so the host can POST /interrupt and set this event immediately.
Every InputController step and every interruptible wait checks it.
"""

from __future__ import annotations

import threading


class InterruptError(RuntimeError):
    code = "E_INTERRUPTED"
    hint = "操作已被用户中断；重新截图确认状态后再决定是否继续。"

    def __init__(self, reason: str = "user-interrupt"):
        super().__init__(f"operation interrupted ({reason})")
        self.reason = reason


class InterruptController:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def request(self, reason: str = "user-interrupt") -> None:
        with self._lock:
            self._reason = str(reason or "user-interrupt")
        self._event.set()

    def clear(self) -> None:
        with self._lock:
            self._reason = ""
        self._event.clear()

    def is_set(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise InterruptError(self.reason)

    def sleep(self, seconds: float) -> None:
        """Interruptible sleep; raises if the host requests cancellation."""
        if seconds <= 0:
            self.check()
            return
        if self._event.wait(seconds):
            raise InterruptError(self.reason)

    def status(self) -> dict:
        return {"set": self._event.is_set(), "reason": self.reason}


__all__ = ["InterruptController", "InterruptError"]
