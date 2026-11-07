"""Unit tests for the VHCU daemon (stdlib only).

Run: py -3 -m unittest discover -s vision/tests -t vision -v
"""

from __future__ import annotations

import http.client
import io
import json
import os
import struct
import unittest
import zlib

from vhcu_daemon.cli import parent_alive
from vhcu_daemon.diff import compare
from vhcu_daemon.httpframe import HttpFrameServer, render_png
from vhcu_daemon.png import encode_png
from vhcu_daemon.protocol import MAX_LINE_BYTES, Protocol, ProtocolError, parse_line
from vhcu_daemon.sources import FakeSource, Frame


class FakeSourceTest(unittest.TestCase):
    def test_frame_shape_and_bytes(self):
        source = FakeSource(width=96, height=64, block=16)
        frame = source.grab()
        self.assertEqual((frame.width, frame.height, frame.channels), (96, 64, 4))
        self.assertEqual(len(frame.data), 96 * 64 * 4)

    def test_every_third_frame_is_identical(self):
        source = FakeSource(width=96, height=64, block=16)
        frames = [source.grab() for _ in range(5)]
        self.assertEqual(frames[0].data, frames[1].data)
        self.assertEqual(frames[1].data, frames[2].data)
        self.assertNotEqual(frames[2].data, frames[3].data)


class DiffTest(unittest.TestCase):
    def test_first_frame_has_no_rois(self):
        frame = FakeSource(width=96, height=64).grab()
        result = compare(None, frame)
        self.assertEqual(result.rois, [])
        self.assertEqual(result.changed_cells, 0)

    def test_identical_frames_have_no_rois(self):
        source = FakeSource(width=96, height=64, block=16)
        first = source.grab()
        second = Frame(first.width, first.height, first.channels, first.data, first.scale)
        result = compare(first, second, step=4)
        self.assertEqual(result.rois, [])
        self.assertEqual(result.diff_ms >= 0, True)

    def test_moved_block_produces_roi_containing_it(self):
        source = FakeSource(width=96, height=64, block=16)
        first = source.grab()
        source.grab()  # identical repeat (tick 1)
        moved = source.grab()  # tick 2 is still same position; move after 3 ticks
        moved = source.grab()
        result = compare(first, moved, step=4, min_cells=1)
        self.assertGreater(result.changed_cells, 0)
        self.assertTrue(result.rois, "expected at least one ROI")
        roi = result.rois[0]
        self.assertGreater(roi.w, 0)
        self.assertGreater(roi.h, 0)
        self.assertGreaterEqual(len(roi.hash), 8)
        self.assertLessEqual(len(roi.hash), 32)
        self.assertGreaterEqual(roi.x, 0)
        self.assertGreaterEqual(roi.y, 0)
        self.assertLessEqual(roi.x + roi.w, 96)
        self.assertLessEqual(roi.y + roi.h, 64)

    def test_dimension_change_resets_baseline(self):
        a = FakeSource(width=64, height=64).grab()
        b = FakeSource(width=96, height=64).grab()
        self.assertEqual(compare(a, b).rois, [])


class PngTest(unittest.TestCase):
    def test_signature_and_ihdr(self):
        payload = encode_png(2, 1, [b"\x01\x02\x03\x04\x05\x06"])
        self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(payload[12:16], b"IHDR")
        width, height = struct.unpack(">II", payload[16:24])
        self.assertEqual((width, height), (2, 1))

    def test_render_downscales_and_crops(self):
        frame = FakeSource(width=320, height=180).grab()
        payload, out_w, out_h = render_png(frame, 10, 10, 100, 50, max_width=25)
        self.assertEqual(out_w, 25)
        self.assertEqual(out_h, 13)
        self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")
        # IDAT must decompress to exactly one filter byte + 3 bytes per pixel per row.
        idat_len = struct.unpack(">I", payload[33:37])[0]
        raw = zlib.decompress(payload[41:41 + idat_len])
        self.assertEqual(len(raw), out_h * (1 + out_w * 3))


class ProtocolTest(unittest.TestCase):
    def test_seq_and_envelope(self):
        stream = io.StringIO()
        proto = Protocol(stream=stream)
        first = proto.send("ack", cmdId="c-1", ok=True)
        second = proto.send("event", kind="vision.started", source="fake", fps=8)
        self.assertEqual(first["seq"], 1)
        self.assertEqual(second["seq"], 2)
        self.assertEqual(second["v"], 1)
        lines = stream.getvalue().strip().split("\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1])["kind"], "vision.started")

    def test_oversized_frame_is_refused(self):
        stream = io.StringIO()
        proto = Protocol(stream=stream)
        with self.assertRaises(ProtocolError) as ctx:
            proto.send("event", kind="daemon.log", text="x" * (MAX_LINE_BYTES + 10))
        self.assertEqual(ctx.exception.code, "E_PROTO")

    def test_parse_line_accepts_cmd_and_rejects_garbage(self):
        cmd = {"v": 1, "seq": 1, "type": "cmd", "ts": 1, "id": "c-1", "op": "ping", "args": {}}
        self.assertEqual(parse_line(json.dumps(cmd).encode())[ "op"], "ping")
        with self.assertRaises(ProtocolError):
            parse_line(b"not json")
        with self.assertRaises(ProtocolError) as ctx:
            parse_line(json.dumps({**cmd, "v": 2}).encode())
        self.assertEqual(ctx.exception.code, "E_VERSION")
        with self.assertRaises(ProtocolError):
            parse_line(json.dumps({"type": "cmd"}).encode())

    def test_shutdown_type_is_accepted(self):
        frame = parse_line(json.dumps({"v": 1, "seq": 9, "type": "shutdown", "ts": 1, "reason": "x"}).encode())
        self.assertEqual(frame["type"], "shutdown")


class HttpFrameServerTest(unittest.TestCase):
    def setUp(self):
        self.source = FakeSource(width=64, height=48)
        self.frame = self.source.grab()
        self.server = HttpFrameServer(lambda: self.frame, "t" * 24, max_width_default=32)
        self.port = self.server.start()

    def tearDown(self):
        self.server.stop()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            body = response.read()
            return response.status, dict(response.getheaders()), body
        finally:
            conn.close()

    def test_health_requires_token(self):
        status, _, _ = self._get("/health")
        self.assertEqual(status, 401)
        status, _, body = self._get(f"/health?token={'t' * 24}")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_frame_returns_png(self):
        status, headers, body = self._get(f"/frame?token={'t' * 24}&maxw=16")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("content-type"), "image/png")
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(headers.get("x-frame-size"), "16x12")

    def test_bad_crop_is_rejected(self):
        status, _, _ = self._get(f"/frame?token={'t' * 24}&w=abc")
        self.assertEqual(status, 400)

    def test_missing_frame_is_404(self):
        self.frame = None
        status, _, _ = self._get(f"/frame?token={'t' * 24}")
        self.assertEqual(status, 404)


class ParentWatchdogTest(unittest.TestCase):
    def test_current_process_is_alive(self):
        self.assertTrue(parent_alive(os.getpid()))
        self.assertTrue(parent_alive(0))

    def test_impossible_pid_is_dead(self):
        self.assertFalse(parent_alive(999_999_999))


if __name__ == "__main__":
    unittest.main()


class RunnerCaptureTest(unittest.TestCase):
    """P6 regression: capture_now must use the active source, not the diff canvas."""

    class Proto:
        def event(self, *_args, **_kwargs):
            return None

    def test_capture_reports_active_source_and_size(self):
        from vhcu_daemon.runner import Runner

        runner = Runner(self.Proto(), width=96, height=64, source="fake", fps=1)
        runner.start("fake", 1)
        try:
            result = runner.capture_now()
            self.assertEqual(result["source"], "fake")
            self.assertEqual((result["width"], result["height"]), (96, 64))
            self.assertGreaterEqual(result["captureMs"], 0)
        finally:
            runner.stop("test")


class FrameScalingTest(unittest.TestCase):
    """P6.2: max_width must scale proportionally, not halve via integer step."""

    def test_1920_frame_to_1600(self):
        frame = Frame(width=1920, height=1080, channels=4, data=b"\x20\x20\x20\xff" * (1920 * 1080))
        payload, out_w, out_h = render_png(frame, 0, 0, 1920, 1080, max_width=1600)
        self.assertEqual((out_w, out_h), (1600, 900))
        self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")

    def test_no_upscale_when_max_width_larger(self):
        frame = FakeSource(width=320, height=180).grab()
        _, out_w, out_h = render_png(frame, 0, 0, 320, 180, max_width=2000)
        self.assertEqual((out_w, out_h), (320, 180))
