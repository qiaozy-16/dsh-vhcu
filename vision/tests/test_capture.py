"""P1/P2: frame metadata, quality checks and target-window capture plumbing."""

from __future__ import annotations

import io
import unittest
from unittest import mock

from vhcu_daemon import win_capture
from vhcu_daemon.protocol import Protocol, ProtocolError
from vhcu_daemon.runner import Runner
from vhcu_daemon.sources import CaptureError, Frame, WindowSource, create_source
from vhcu_daemon.win_capture import CaptureImage, WindowCaptureError, analyze_capture


class MssOriginTest(unittest.TestCase):
    def test_virtual_monitor_origin_is_preserved(self):
        import sys
        from types import SimpleNamespace

        class Shot:
            width = 4
            height = 3
            raw = b"\x00" * 48

        class FakeMss:
            monitors = [
                {"left": -1920, "top": -100, "width": 4, "height": 3},
                {"left": 0, "top": 0, "width": 4, "height": 3},
            ]

            def grab(self, monitor):
                return Shot()

            def close(self):
                pass

        from vhcu_daemon.sources import MssSource
        with mock.patch.dict(sys.modules, {"mss": SimpleNamespace(mss=FakeMss)}):
            source = MssSource(monitor=0)
            frame = source.grab()
        self.assertEqual(frame.origin_x, -1920)
        self.assertEqual(frame.origin_y, -100)
        self.assertEqual((frame.width, frame.height), (4, 3))


class FrameMetadataTest(unittest.TestCase):
    def test_frame_carries_capture_metadata(self):
        frame = Frame(
            width=4,
            height=3,
            channels=4,
            data=b"\x00" * (4 * 3 * 4),
            backend="printwindow_full",
            blank=False,
            occluded=True,
            frame_seq=42,
            target_hwnd=123,
        )
        self.assertEqual(frame.backend, "printwindow_full")
        self.assertFalse(frame.blank)
        self.assertTrue(frame.occluded)
        self.assertEqual(frame.frame_seq, 42)
        self.assertEqual(frame.target_hwnd, 123)

    def test_defaults_are_backward_compatible(self):
        frame = Frame(2, 2, 4, b"\x00" * 16)
        self.assertEqual(frame.backend, "unknown")
        self.assertFalse(frame.blank)
        self.assertFalse(frame.occluded)
        self.assertEqual(frame.frame_seq, 0)
        self.assertIsNone(frame.target_hwnd)


class ContentQualityTest(unittest.TestCase):
    def test_all_black_is_blank(self):
        quality = analyze_capture(bytes([0, 0, 0, 255]) * 100, 10, 10)
        self.assertTrue(quality.blank)
        self.assertGreater(quality.black_ratio, 0.99)

    def test_single_color_is_blank(self):
        quality = analyze_capture(bytes([10, 20, 30, 255]) * 100, 10, 10)
        self.assertTrue(quality.blank)
        self.assertGreater(quality.dominant_ratio, 0.99)

    def test_varied_content_is_not_blank(self):
        pixels = bytearray()
        for index in range(100):
            pixels.extend((index * 3 % 256, index * 7 % 256, index * 11 % 256, 255))
        quality = analyze_capture(bytes(pixels), 10, 10)
        self.assertFalse(quality.blank)
        self.assertLess(quality.dominant_ratio, 0.99)

    def test_short_buffer_is_blank_not_crash(self):
        quality = analyze_capture(b"\x00", 10, 10)
        self.assertTrue(quality.blank)
        self.assertEqual(quality.samples, 0)


def _varied_bgra(width=80, height=60):
    data = bytearray()
    for index in range(width * height):
        data.extend((index * 3 % 251, index * 7 % 251, index * 11 % 251, 255))
    return bytes(data)


class _FallbackUser:
    def __init__(self, iconic=False, visible=True):
        self.iconic = iconic
        self.visible = visible

    def IsWindow(self, hwnd):
        return True

    def IsIconic(self, hwnd):
        return self.iconic

    def IsWindowVisible(self, hwnd):
        return self.visible

    def GetAncestor(self, hwnd, flags):
        return hwnd

    def GetForegroundWindow(self):
        return 5


class GdiFallbackTest(unittest.TestCase):
    def test_black_printwindow_falls_back_to_foreground_bitblt(self):
        user = _FallbackUser()
        black = bytes(80 * 60 * 4)
        varied = _varied_bgra()

        def fake_capture(hwnd, width, height, mode, left=0, top=0):
            return varied if mode == "bitblt" else black

        with mock.patch.object(win_capture, "_api", return_value=(user, object())),              mock.patch.object(win_capture, "_ensure_dpi_aware", lambda: None),              mock.patch.object(win_capture, "_rect", lambda hwnd: (10, 10, 80, 60)),              mock.patch.object(win_capture, "_is_occluded", lambda *args: False),              mock.patch.object(win_capture, "_is_foreground", lambda hwnd: True),              mock.patch.object(win_capture, "_capture_with_dc", side_effect=fake_capture):
            image = win_capture.capture_window(7)
        self.assertEqual(image.backend, "bitblt")
        self.assertFalse(image.blank)
        self.assertFalse(image.occluded)

    def test_occluded_printwindow_success_is_allowed(self):
        user = _FallbackUser()
        varied = _varied_bgra()

        def fake_capture(hwnd, width, height, mode, left=0, top=0):
            return varied if mode == "printwindow_full" else None

        with mock.patch.object(win_capture, "_api", return_value=(user, object())),              mock.patch.object(win_capture, "_ensure_dpi_aware", lambda: None),              mock.patch.object(win_capture, "_rect", lambda hwnd: (10, 10, 80, 60)),              mock.patch.object(win_capture, "_is_occluded", lambda *args: True),              mock.patch.object(win_capture, "_is_foreground", lambda hwnd: True),              mock.patch.object(win_capture, "_capture_with_dc", side_effect=fake_capture):
            image = win_capture.capture_window(7, strict_occlusion=True)
        self.assertEqual(image.backend, "printwindow_full")
        self.assertTrue(image.occluded)
        self.assertFalse(image.blank)

    def test_occluded_black_window_never_bitblts(self):
        user = _FallbackUser()
        black = bytes(80 * 60 * 4)
        seen_modes = []

        def fake_capture(hwnd, width, height, mode, left=0, top=0):
            seen_modes.append(mode)
            return black

        with mock.patch.object(win_capture, "_api", return_value=(user, object())),              mock.patch.object(win_capture, "_ensure_dpi_aware", lambda: None),              mock.patch.object(win_capture, "_rect", lambda hwnd: (10, 10, 80, 60)),              mock.patch.object(win_capture, "_is_occluded", lambda *args: True),              mock.patch.object(win_capture, "_is_foreground", lambda hwnd: True),              mock.patch.object(win_capture, "_capture_with_dc", side_effect=fake_capture):
            with self.assertRaises(WindowCaptureError) as raised:
                win_capture.capture_window(7)
        self.assertEqual(raised.exception.code, "E_CAPTURE_OCCLUDED")
        self.assertNotIn("bitblt", seen_modes)

    def test_minimized_window_is_refused(self):
        user = _FallbackUser(iconic=True)
        with mock.patch.object(win_capture, "_api", return_value=(user, object())),              mock.patch.object(win_capture, "_ensure_dpi_aware", lambda: None):
            with self.assertRaises(WindowCaptureError) as raised:
                win_capture.capture_window(7)
        self.assertEqual(raised.exception.code, "E_CAPTURE_MINIMIZED")


class _FakeTargetSource:
    name = "window"

    def __init__(self, frame):
        self.frame = frame
        self.closed = False

    def grab(self):
        return self.frame

    def close(self):
        self.closed = True


class TargetCaptureTest(unittest.TestCase):
    def _runner(self, source):
        seen = {}

        def factory(name, **options):
            seen["name"] = name
            seen["options"] = options
            return source

        runner = Runner(Protocol(stream=io.StringIO()), source_factory=factory)
        runner.privacy_blacklist = []
        return runner, seen

    def test_capture_window_publishes_metadata(self):
        frame = Frame(
            width=640,
            height=480,
            channels=4,
            data=b"\x00" * (640 * 480 * 4),
            backend="printwindow_full",
            occluded=True,
            target_hwnd=123,
        )
        source = _FakeTargetSource(frame)
        runner, seen = self._runner(source)
        result = runner.capture_window(123, strict_occlusion=True)
        self.assertEqual(seen["name"], "window")
        self.assertEqual(seen["options"]["target_hwnd"], 123)
        self.assertEqual(result["backend"], "printwindow_full")
        self.assertTrue(result["occluded"])
        self.assertFalse(result["blank"])
        self.assertEqual(result["targetHwnd"], 123)
        self.assertEqual(result["frame_seq"], 1)
        self.assertTrue(source.closed)
        self.assertEqual(runner.latest_frame().target_hwnd, 123)

    def test_privacy_purges_old_seq_frames_before_warning_frame(self):
        frame = Frame(4, 3, 4, b"\x00" * 48, frame_seq=7, target_hwnd=123)
        source = _FakeTargetSource(frame)
        runner, _ = self._runner(source)
        runner._latest = frame
        runner._recent_frames[7] = frame
        runner._recent_order.append(7)
        runner._target_frames[7] = frame
        runner._target_frame_order.append(7)
        runner._privacy_should_block = lambda: True
        with mock.patch.object(win_capture, "window_title", return_value=""):
            result = runner.capture_window(123)
        self.assertTrue(result["privacyBlocked"])
        self.assertIsNone(runner.frame_by_seq(7))
        self.assertNotEqual(result["frame_seq"], 7)
        self.assertEqual(runner._recent_order, [result["frame_seq"]])
        self.assertEqual(runner._target_frame_order, [result["frame_seq"]])

    def test_trim_resources_drops_latest_frame_and_snapshot_source(self):
        frame = Frame(4, 3, 4, b"\x00" * 48)
        source = _FakeTargetSource(frame)
        runner, _ = self._runner(source)
        runner._latest = frame
        runner._snapshot_source = source
        result = runner.trim_resources()
        self.assertTrue(result["dropped_frame"])
        self.assertTrue(result["snapshot_source_closed"])
        self.assertIsNone(runner.latest_frame())
        self.assertTrue(source.closed)

    def test_capture_window_preserves_capture_error_code(self):
        source = _FakeTargetSource(None)
        source.grab = lambda: (_ for _ in ()).throw(
            CaptureError("minimized", code="E_CAPTURE_MINIMIZED", hint="restore it")
        )
        runner, _ = self._runner(source)
        with self.assertRaises(ProtocolError) as raised:
            runner.capture_window(123)
        self.assertEqual(raised.exception.code, "E_CAPTURE_MINIMIZED")
        self.assertEqual(raised.exception.hint, "restore it")

    def test_window_source_wraps_capture_image(self):
        source = WindowSource(target_hwnd=99)
        source._capture_window = lambda hwnd, strict_occlusion=False: CaptureImage(
            width=3,
            height=2,
            data=b"\x00" * (3 * 2 * 4),
            backend="bitblt",
            occluded=True,
            blank=False,
        )
        frame = source.grab()
        self.assertEqual(frame.backend, "bitblt")
        self.assertTrue(frame.occluded)
        self.assertEqual(frame.target_hwnd, 99)
        self.assertEqual(frame.frame_seq, 0)

    def test_create_source_requires_hwnd_for_window(self):
        with self.assertRaises(ValueError):
            create_source("window")


class _RecordingSource:
    name = "fake"
    width = 64
    height = 48

    def __init__(self, order):
        self._order = order

    def grab(self):
        self._order.append("grab")
        return Frame(64, 48, 4, b"\x00" * (64 * 48 * 4), backend="fake")

    def close(self):
        pass


class _RecordingOverlay:
    def __init__(self, order):
        self._order = order

    def hide_sync(self, timeout: float = 0.3) -> bool:
        self._order.append("hide")
        return True

    def restore_after_capture(self) -> None:
        self._order.append("restore")


class CaptureGuardTest(unittest.TestCase):
    """P1 8.8: the virtual cursor must be hidden around model-facing captures."""

    def _runner(self, order):
        def factory(name, **options):
            return _RecordingSource(order)

        runner = Runner(Protocol(stream=io.StringIO()), source_factory=factory)
        runner.privacy_blacklist = []
        runner.attach_overlay(_RecordingOverlay(order))
        return runner

    def test_capture_now_hides_before_grab_and_restores_after(self):
        order = []
        runner = self._runner(order)
        runner.capture_now()
        self.assertEqual(order, ["hide", "grab", "restore"])

    def test_capture_window_hides_before_grab_and_restores_after(self):
        order = []

        class Source:
            name = "window"

            def grab(self):
                order.append("grab")
                return Frame(
                    32, 24, 4, b"\x00" * (32 * 24 * 4),
                    backend="printwindow_full", target_hwnd=7,
                )

            def close(self):
                pass

        runner = Runner(
            Protocol(stream=io.StringIO()),
            source_factory=lambda name, **options: Source(),
        )
        runner.privacy_blacklist = []
        runner.attach_overlay(_RecordingOverlay(order))
        runner.capture_window(7)
        self.assertEqual(order, ["hide", "grab", "restore"])

    def test_capture_guard_restores_even_when_hide_fails(self):
        order = []

        class BadOverlay:
            def hide_sync(self, timeout: float = 0.3) -> bool:
                order.append("hide")
                return False

            def restore_after_capture(self) -> None:
                order.append("restore")

        runner = Runner(
            Protocol(stream=io.StringIO()),
            source_factory=lambda name, **options: _RecordingSource(order),
        )
        runner.privacy_blacklist = []
        runner.attach_overlay(BadOverlay())
        runner.capture_now()
        self.assertEqual(order, ["hide", "grab", "restore"])


class RecentFrameSeqTest(unittest.TestCase):
    """P1 8.7: a signed full-frame URL stays bound to its captured frame_seq."""

    class Proto:
        def event(self, *_args, **_kwargs):
            return None

    def _runner(self):
        runner = Runner(self.Proto(), width=32, height=24, source="fake", fps=1)
        runner.privacy_blacklist = []
        return runner

    def test_frame_by_seq_serves_recent_full_frames(self):
        from vhcu_daemon.runner import MAX_RECENT_FRAMES

        runner = self._runner()
        seqs = [runner.capture_now()["frame_seq"] for _ in range(MAX_RECENT_FRAMES)]
        for seq in seqs:
            self.assertIsNotNone(runner.frame_by_seq(seq), f"seq {seq} was evicted too early")
        # One more capture pushes the oldest seq out of the bounded history.
        newest = runner.capture_now()["frame_seq"]
        self.assertIsNone(runner.frame_by_seq(seqs[0]))
        self.assertIsNotNone(runner.frame_by_seq(newest))
        self.assertIsNone(runner.frame_by_seq(999_999))

    def test_trim_resources_drops_recent_frames(self):
        runner = self._runner()
        seq = runner.capture_now()["frame_seq"]
        self.assertIsNotNone(runner.frame_by_seq(seq))
        runner.trim_resources()
        self.assertIsNone(runner.frame_by_seq(seq))

    def test_older_publish_cannot_regress_latest_frame(self):
        runner = self._runner()
        newer = Frame(32, 24, 4, b"\x00" * (32 * 24 * 4), frame_seq=10)
        older = Frame(32, 24, 4, b"\xff" * (32 * 24 * 4), frame_seq=9)
        runner._publish_frame(newer)
        runner._publish_frame(older)
        self.assertIs(runner.latest_frame(), newer)
        self.assertIs(runner.frame_by_seq(9), older)
        self.assertIs(runner.frame_by_seq(10), newer)


if __name__ == "__main__":
    unittest.main()
