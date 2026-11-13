"""Authenticated loopback image plane: GET /health, GET /frame.

Only the host process (which learns port+token from READY) is expected to call
this. It is pull-only and rate-limited: previews must never back-pressure the
JSON control plane.
"""

from __future__ import annotations

import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from .png import encode_png
from .sources import Frame

MAX_INFLIGHT = 2
MAX_DIMENSION = 8192


def _int_param(params: dict, name: str, default: int) -> int:
    raw = params.get(name, [None])[0]
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    return value


def render_png(frame: Frame, x: int, y: int, w: int, h: int, max_width: int) -> tuple[bytes, int, int]:
    width, height = frame.width, frame.height
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))
    w = max(1, min(w, width - x, MAX_DIMENSION))
    h = max(1, min(h, height - y, MAX_DIMENSION))
    # Proportional resize, not integer decimation: max_width=1600 on a
    # 1920x1080 frame must yield 1600x900. The old ceil(w/max_width) step
    # produced 960x540 because step=2 silently halved the output.
    out_w = min(w, max(1, int(max_width)))
    out_h = max(1, (h * out_w + w // 2) // w)
    src_xs = [min(w - 1, (i * w) // out_w) for i in range(out_w)]
    src_ys = [min(h - 1, (j * h) // out_h) for j in range(out_h)]
    rows = []
    data = frame.data
    for src_y in src_ys:
        base = ((y + src_y) * width + x) * 4
        row = bytearray()
        for src_x in src_xs:
            offset = base + src_x * 4
            row.append(data[offset + 2])  # R
            row.append(data[offset + 1])  # G
            row.append(data[offset])      # B
        rows.append(bytes(row))
    return encode_png(out_w, out_h, rows), out_w, out_h


class HttpFrameServer:
    def __init__(
        self,
        provider: Callable[[], Optional[Frame]],
        token: str,
        host: str = "127.0.0.1",
        port: int = 0,
        max_width_default: int = 960,
        on_interrupt=None,
    ):
        if not token:
            raise ValueError("token is required")
        self._provider = provider
        self._token = token
        self._host = host
        self._requested_port = port
        self._max_width_default = max(64, int(max_width_default))
        self._on_interrupt = on_interrupt
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._semaphore = threading.Semaphore(MAX_INFLIGHT)
        self._started_at = time.time()

    @property
    def port(self) -> int:
        if self._server is None:
            return 0
        return int(self._server.server_address[1])

    def start(self) -> int:
        if self._server is not None:
            return self.port
        handler = _make_handler(self)
        server = ThreadingHTTPServer((self._host, self._requested_port), handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="vhcu-http", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ── request handling (called by the handler class) ───────────────────

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        params = parse_qs(parsed.query)
        if not self._token_ok(params):
            self._json(handler, 401, {"ok": False, "error": "invalid token"})
            return
        if parsed.path == "/health":
            self._json(handler, 200, {"ok": True, "uptimeMs": int((time.time() - self._started_at) * 1000)})
            return
        if parsed.path == "/interrupt":
            if handler.command != "POST":
                self._json(handler, 405, {"ok": False, "error": "POST required"})
                return
            if self._on_interrupt is None:
                self._json(handler, 503, {"ok": False, "error": "interrupt channel unavailable"})
                return
            reason = str(params.get("reason", ["host-interrupt"])[0] or "host-interrupt")
            clear = str(params.get("clear", [""])[0]).lower() in ("1", "true", "yes")
            result = self._on_interrupt(reason, clear)
            self._json(handler, 200, {"ok": True, **(result or {})})
            return
        if parsed.path == "/frame":
            if not self._semaphore.acquire(blocking=False):
                self._json(handler, 429, {"ok": False, "error": "too many concurrent frame requests"})
                return
            try:
                self._frame(handler, params)
            finally:
                self._semaphore.release()
            return
        self._json(handler, 404, {"ok": False, "error": "not found"})

    def _token_ok(self, params: dict) -> bool:
        provided = params.get("token", [""])[0]
        return hmac.compare_digest(str(provided), self._token)

    def _frame(self, handler: BaseHTTPRequestHandler, params: dict) -> None:
        frame = self._provider()
        if frame is None:
            self._json(handler, 404, {"ok": False, "error": "no frame captured yet"})
            return
        try:
            x = _int_param(params, "x", 0)
            y = _int_param(params, "y", 0)
            w = _int_param(params, "w", frame.width)
            h = _int_param(params, "h", frame.height)
            max_width = _int_param(params, "maxw", self._max_width_default)
            payload, out_w, out_h = render_png(frame, x, y, w, h, max_width)
        except ValueError as error:
            self._json(handler, 400, {"ok": False, "error": str(error)})
            return
        handler.send_response(200)
        handler.send_header("content-type", "image/png")
        handler.send_header("content-length", str(len(payload)))
        handler.send_header("cache-control", "no-store")
        handler.send_header("x-frame-size", f"{out_w}x{out_h}")
        handler.end_headers()
        handler.wfile.write(payload)

    @staticmethod
    def _json(handler: BaseHTTPRequestHandler, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        handler.send_response(status)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)


def _make_handler(server: HttpFrameServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            try:
                server.handle(self)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:  # noqa: BLE001
                try:
                    self.send_response(500)
                    self.send_header("content-length", "0")
                    self.end_headers()
                except Exception:  # noqa: BLE001
                    pass
                print(f"vhcu-daemon: http handler error: {error}", flush=True)

        def do_POST(self) -> None:  # noqa: N802
            try:
                server.handle(self)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as error:  # noqa: BLE001
                try:
                    self.send_response(500)
                    self.send_header("content-length", "0")
                    self.end_headers()
                except Exception:  # noqa: BLE001
                    pass
                print(f"vhcu-daemon: http handler error: {error}", flush=True)

        def log_message(self, fmt: str, *args) -> None:
            # Access logs belong on stderr at debug level; never per request.
            return

    return Handler
