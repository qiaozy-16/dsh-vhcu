"""P4 Element Registry, dHash and stale-aware action tests."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from vhcu_daemon.uia_worker import UiaFailed, json_safe
from vhcu_daemon.element_registry import (
    ElementActionService,
    ElementCaptureFailed,
    ElementNotFound,
    ElementRegistry,
    ElementStale,
    dhash_bgra,
    hamming,
)
from vhcu_daemon.sources import Frame


def _solid(width=80, height=60, value=128):
    data = bytearray()
    for index in range(width * height):
        data.extend((value, value, value, 255))
    return bytes(data)


def _checker(width=80, height=60):
    data = bytearray()
    for y in range(height):
        for x in range(width):
            value = 240 if (x // 4 + y // 4) % 2 else 20
            data.extend((value, value, value, 255))
    return bytes(data)


class DHashTest(unittest.TestCase):
    def test_same_pixels_same_hash(self):
        data = _checker()
        bounds = {"x": 10, "y": 5, "width": 30, "height": 20}
        window = {"x": 0, "y": 0, "width": 80, "height": 60}
        self.assertEqual(
            dhash_bgra(data, 80, 60, bounds, window),
            dhash_bgra(data, 80, 60, bounds, window),
        )

    def test_changed_pixels_change_hash(self):
        window = {"x": 0, "y": 0, "width": 80, "height": 60}
        bounds = {"x": 10, "y": 5, "width": 30, "height": 20}
        one = dhash_bgra(_checker(), 80, 60, bounds, window)
        two = dhash_bgra(_solid(), 80, 60, bounds, window)
        self.assertIsNotNone(one)
        self.assertIsNotNone(two)
        self.assertNotEqual(one, two)
        self.assertGreater(hamming(one, two), 0)

    def test_out_of_frame_returns_none(self):
        self.assertIsNone(
            dhash_bgra(_checker(), 80, 60, {"x": 100, "y": 100, "width": 10, "height": 10},
                       {"x": 0, "y": 0, "width": 80, "height": 60})
        )


class JsonSafeTest(unittest.TestCase):
    def test_nan_bytes_and_com_objects_become_json_primitives(self):
        safe = json_safe({
            "nan": float("nan"),
            "inf": float("inf"),
            "raw": bytes([1, 2]),
            "object": object(),
            "list": [1, float("nan")],
        })
        self.assertIsNone(safe["nan"])
        self.assertIsNone(safe["inf"])
        self.assertEqual(safe["raw"], "0102")
        self.assertIsInstance(safe["object"], str)
        self.assertIsNone(safe["list"][1])
        json.dumps(safe, allow_nan=False)

    def test_json_safe_preserves_strings(self):
        self.assertEqual(json_safe("el_abc"), "el_abc")
        self.assertEqual(json_safe({"element_id": "el_abc"}), {"element_id": "el_abc"})


class RegistryTest(unittest.TestCase):
    def test_register_assigns_opaque_tokens_and_fingerprints(self):
        registry = ElementRegistry()
        frame = Frame(80, 60, 4, _checker())
        result = {
            "hwnd": 7,
            "window_rect": {"x": 0, "y": 0, "width": 80, "height": 60},
            "elements": [{
                "name": "Save",
                "automation_id": "btnSave",
                "control_type": "Button",
                "bounds": {"x": 10, "y": 5, "width": 30, "height": 20},
            }],
        }
        out = registry.register_result(result, {"name": "Save"}, {}, capture_fn=lambda hwnd: frame)
        element = out["elements"][0]
        self.assertTrue(element["element_id"].startswith("el_"))
        self.assertTrue(element["fingerprint"])
        self.assertTrue(element["fingerprint_available"])
        record = registry.get(element["element_id"])
        self.assertEqual(record.hwnd, 7)
        self.assertEqual(record.bounds["width"], 30)
        self.assertEqual(registry.get(element["element_id"]).element["name"], "Save")

    def test_registry_is_bounded_by_max_entries(self):
        registry = ElementRegistry(max_entries=3, ttl=100)
        frame = Frame(80, 60, 4, _checker())
        result = {
            "hwnd": 7,
            "window_rect": {"x": 0, "y": 0, "width": 80, "height": 60},
            "elements": [
                {"name": f"E{i}", "bounds": {"x": 1, "y": 1, "width": 10, "height": 10}}
                for i in range(8)
            ],
        }
        out = registry.register_result(result, {}, {}, capture_fn=lambda hwnd: frame)
        self.assertEqual(len(out["elements"]), 8)
        self.assertLessEqual(len(registry), 3)

    def test_registry_always_adds_string_token_despite_weird_values(self):
        registry = ElementRegistry()
        result = {
            "hwnd": 7,
            "window_rect": {"x": 0, "y": 0, "width": 80, "height": 60},
            "elements": [{
                "name": "A",
                "bounds": {"x": 0, "y": 0, "width": 10, "height": 10},
                "value": float("nan"),
                "raw": b"\x00",
                "control": object(),
            }],
        }
        out = registry.register_result(result, {}, {}, capture_fn=lambda hwnd: None)
        element = out["elements"][0]
        self.assertIsInstance(element["element_id"], str)
        self.assertTrue(element["element_id"].startswith("el_"))
        json.dumps(out, allow_nan=False)

    def test_capture_failure_still_registers_token(self):
        registry = ElementRegistry()
        result = {
            "hwnd": 7,
            "window_rect": {"x": 0, "y": 0, "width": 80, "height": 60},
            "elements": [{"name": "A", "bounds": {"x": 0, "y": 0, "width": 10, "height": 10}}],
        }
        out = registry.register_result(
            result, {}, {}, capture_fn=lambda hwnd: (_ for _ in ()).throw(RuntimeError("no capture"))
        )
        self.assertFalse(out["elements"][0]["fingerprint_available"])
        self.assertIsNone(registry.get(out["elements"][0]["element_id"]).fingerprint)

    def test_missing_token_is_explicit(self):
        with self.assertRaises(ElementNotFound):
            ElementRegistry().get("el_missing")


class _FakeWorker:
    def __init__(self, finds, invoke_result=None):
        self.finds = list(finds)
        self.invoke_result = invoke_result or {"method": "invoke", "invoked": True, "pattern": "Invoke"}
        self.invoke_calls = 0

    def find(self, hwnd, locator, options, timeout_ms=4000):
        if not self.finds:
            raise RuntimeError("no more finds")
        value = self.finds.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def invoke(self, hwnd, locator, target, options, timeout_ms=4000):
        self.invoke_calls += 1
        return dict(self.invoke_result)


def _find_result(x=10, fingerprint_frame=None):
    return {
        "hwnd": 7,
        "window_rect": {"x": 0, "y": 0, "width": 80, "height": 60},
        "elements": [{
            "name": "Save",
            "automation_id": "btnSave",
            "control_type": "Button",
            "bounds": {"x": x, "y": 5, "width": 30, "height": 20},
        }],
    }


class DaemonPrivacyFallbackTest(unittest.TestCase):
    def test_missing_or_broken_privacy_checker_does_not_raise(self):
        from vhcu_daemon.cli import Daemon

        daemon = Daemon.__new__(Daemon)
        daemon.runner = SimpleNamespace(privacy_blocked=False)
        self.assertFalse(daemon._privacy_blocked())

        daemon.runner = SimpleNamespace(
            privacy_blocked=False,
            _privacy_should_block=lambda: (_ for _ in ()).throw(AttributeError("broken checker")),
        )
        self.assertFalse(daemon._privacy_blocked())

        daemon.runner = SimpleNamespace(
            privacy_blocked=True,
            _privacy_should_block=lambda: False,
        )
        self.assertFalse(daemon._privacy_blocked())


class ActionServiceTest(unittest.TestCase):
    def _registry(self, frame):
        registry = ElementRegistry()
        out = registry.register_result(_find_result(), {"name": "Save"}, {}, capture_fn=lambda hwnd: frame)
        return registry, out["elements"][0]["element_id"]

    def test_fresh_element_invokes_pattern(self):
        frame = Frame(80, 60, 4, _checker())
        registry, token = self._registry(frame)
        worker = _FakeWorker([_find_result()])
        service = ElementActionService(registry, worker, capture_fn=lambda hwnd: frame)
        result = service.click(token, retry_max_attempts=2)
        self.assertEqual(result["method"], "invoke")
        self.assertTrue(result["invoked"])
        self.assertEqual(result["retries"], 0)
        self.assertEqual(worker.invoke_calls, 1)

    def test_coordinate_plan_does_not_click(self):
        frame = Frame(80, 60, 4, _checker())
        registry, token = self._registry(frame)
        worker = _FakeWorker(
            [_find_result()],
            invoke_result={
                "method": "coordinate",
                "invoked": False,
                "center": {"x": 25, "y": 15},
            },
        )
        service = ElementActionService(registry, worker, capture_fn=lambda hwnd: frame)
        result = service.click(token, retry_max_attempts=0)
        self.assertEqual(result["method"], "coordinate")
        self.assertFalse(result["invoked"])
        self.assertEqual(worker.invoke_calls, 1)

    def test_stale_bounds_triggers_retry_and_recovery(self):
        frame = Frame(80, 60, 4, _checker())
        registry, token = self._registry(frame)
        worker = _FakeWorker([_find_result(x=40), _find_result(x=10)])
        service = ElementActionService(registry, worker, capture_fn=lambda hwnd: frame)
        result = service.prepare(token, retry_max_attempts=2)
        self.assertGreaterEqual(result["retries"], 1)
        self.assertTrue(result["stale_seen"])
        self.assertEqual(result["element"]["bounds"]["x"], 10)

    def test_stale_hash_triggers_retry(self):
        original = Frame(80, 60, 4, _checker())
        registry, token = self._registry(original)
        changed = Frame(80, 60, 4, _solid())
        frames = [changed, original]
        worker = _FakeWorker([_find_result(), _find_result()])
        service = ElementActionService(
            registry, worker, capture_fn=lambda hwnd: frames.pop(0)
        )
        result = service.prepare(token, retry_max_attempts=1)
        self.assertTrue(result["stale_seen"])
        self.assertGreaterEqual(result["retries"], 1)

    def test_stale_exhaustion_is_explicit(self):
        frame = Frame(80, 60, 4, _checker())
        registry, token = self._registry(frame)
        worker = _FakeWorker([_find_result(x=40), _find_result(x=40), _find_result(x=40)])
        service = ElementActionService(registry, worker, capture_fn=lambda hwnd: frame)
        with self.assertRaises(ElementStale) as raised:
            service.prepare(token, retry_max_attempts=2)
        self.assertEqual(raised.exception.code, "E_ELEMENT_STALE")

    def test_capture_failure_is_capture_not_stale(self):
        frame = Frame(80, 60, 4, _checker())
        registry, token = self._registry(frame)
        worker = _FakeWorker([_find_result(), _find_result()])
        service = ElementActionService(
            registry, worker, capture_fn=lambda hwnd: (_ for _ in ()).throw(AttributeError("missing privacy"))
        )
        with self.assertRaises(ElementCaptureFailed) as raised:
            service.prepare(token, retry_max_attempts=1)
        self.assertEqual(raised.exception.code, "E_CAPTURE")
        self.assertNotIsInstance(raised.exception, ElementStale)

    def test_missing_candidate_is_not_stale(self):
        frame = Frame(80, 60, 4, _checker())
        registry, token = self._registry(frame)
        empty = {"hwnd": 7, "window_rect": {"x": 0, "y": 0, "width": 80, "height": 60}, "elements": []}
        worker = _FakeWorker([empty, empty])
        service = ElementActionService(registry, worker, capture_fn=lambda hwnd: frame)
        with self.assertRaises(ElementNotFound) as raised:
            service.prepare(token, retry_max_attempts=1)
        self.assertEqual(raised.exception.code, "E_ELEMENT_NOT_FOUND")

    def test_unexpected_find_error_is_uia_failed_not_stale(self):
        frame = Frame(80, 60, 4, _checker())
        registry, token = self._registry(frame)
        worker = _FakeWorker([RuntimeError("internal"), RuntimeError("internal")])
        service = ElementActionService(registry, worker, capture_fn=lambda hwnd: frame)
        with self.assertRaises(UiaFailed) as raised:
            service.prepare(token, retry_max_attempts=1)
        self.assertEqual(raised.exception.code, "E_UIA_FAILED")

    def test_find_error_retries_then_recovers(self):
        frame = Frame(80, 60, 4, _checker())
        registry, token = self._registry(frame)
        worker = _FakeWorker([RuntimeError("temporary UIA error"), _find_result()])
        service = ElementActionService(registry, worker, capture_fn=lambda hwnd: frame)
        result = service.prepare(token, retry_max_attempts=1)
        self.assertGreaterEqual(result["retries"], 1)


if __name__ == "__main__":
    unittest.main()
