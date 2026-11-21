"""P3 UIA worker tests: validation, worker lifecycle and UIA tree traversal."""

from __future__ import annotations

import io
import sys
import time
import types
import unittest
from unittest.mock import patch

from vhcu_daemon import uia_worker
from vhcu_daemon.uia_worker import (
    UiaAutomationAdapter,
    UiaError,
    UiaFailed,
    UiaTimeout,
    UiaUnavailable,
    UiaWindowGone,
    UiaWorker,
    normalize_find_request,
)


class _Rect:
    def __init__(self, left, top, right, bottom):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class _FakeValuePattern:
    def __init__(self, value="secret"):
        self.Value = value


class _FakeInvokePattern:
    def __init__(self):
        self.called = 0

    def Invoke(self):
        self.called += 1
        return True


class _FakeControl:
    def __init__(self, name="", automation_id="", control_type=50000,
                 class_name="Button", bounds=(0, 0, 1, 1), *,
                 offscreen=False, enabled=True, password=False, children=None,
                 patterns=None):
        self.Name = name
        self.AutomationId = automation_id
        self.ControlType = control_type
        self.ClassName = class_name
        self.BoundingRectangle = _Rect(*bounds)
        self.IsOffscreen = offscreen
        self.IsEnabled = enabled
        self.IsPassword = password
        self._children = list(children or [])
        self._patterns = set(patterns or [])
        self.value_reads = 0
        self.value_text = "secret"
        self.invoke_pattern = _FakeInvokePattern() if 10000 in self._patterns else None

    def GetChildren(self):
        return list(self._children)

    def GetPattern(self, pattern_id):
        if pattern_id == 10002 and pattern_id in self._patterns:
            self.value_reads += 1
            return _FakeValuePattern(self.value_text)
        if pattern_id in self._patterns:
            return object()
        return None

    def GetInvokePattern(self):
        return self.invoke_pattern


class _FakeAuto(types.ModuleType):
    ControlType = types.SimpleNamespace(ButtonControl=50000, EditControl=50004)
    ControlTypeNames = {50000: "ButtonControl", 50004: "EditControl"}
    PatternId = types.SimpleNamespace(
        InvokePattern=10000,
        ValuePattern=10002,
        TogglePattern=10015,
        SelectionItemPattern=10010,
        ExpandCollapsePattern=10005,
        ScrollItemPattern=10017,
        TextPattern=10014,
        RangeValuePattern=10003,
    )

    def __init__(self, root):
        super().__init__("uiautomation")
        self.root = root
        self.initialized = 0
        self.uninitialized = 0

    def InitializeUIAutomationInCurrentThread(self):
        self.initialized += 1

    def UninitializeUIAutomationInCurrentThread(self):
        self.uninitialized += 1

    def ControlFromHandle(self, hwnd):
        return self.root


class _FakeAdapter:
    def __init__(self, result=None, error=None):
        self.result = result or {"elements": []}
        self.error = error
        self.initialized = False
        self.uninitialized = False

    def initialize(self):
        self.initialized = True

    def uninitialize(self):
        self.uninitialized = True

    def find(self, hwnd, locator, options):
        if self.error:
            raise self.error
        return dict(self.result)

    def invoke(self, hwnd, locator, target, options):
        if self.error:
            raise self.error
        return {"method": "invoke", "invoked": True, "pattern": "Invoke"}

    def click_point(self, x, y, click_type="click"):
        if self.error:
            raise self.error
        return {
            "method": "invoke",
            "invoked": True,
            "pattern": "Invoke",
            "center": {"x": int(x), "y": int(y)},
            "element": {"name": "Point", "automation_id": "pt"},
        }


class NormalizeRequestTest(unittest.TestCase):
    def test_defaults_and_control_type(self):
        hwnd, locator, options = normalize_find_request(
            12, {"name": "Save", "control_type": "Button"}, {}
        )
        self.assertEqual(hwnd, 12)
        self.assertEqual(locator["name_match"], "exact")
        self.assertEqual(options["max_depth"], 10)
        self.assertEqual(options["limit"], 50)
        self.assertTrue(options["include_offscreen"])

    def test_invalid_fields_are_rejected(self):
        for kwargs in (
            {"hwnd": 0},
            {"hwnd": "x"},
            {"hwnd": 1, "locator": {"name_match": "regex"}},
            {"hwnd": 1, "locator": {"unknown": "x"}},
            {"hwnd": 1, "options": {"max_depth": 0}},
            {"hwnd": 1, "options": {"limit": 1000}},
            {"hwnd": 1, "options": {"node_budget": 0}},
        ):
            with self.assertRaises(ValueError):
                normalize_find_request(**kwargs)


class WorkerTest(unittest.TestCase):
    def test_worker_returns_result_and_shuts_down(self):
        adapter = _FakeAdapter({"elements": [{"name": "A"}]})
        worker = UiaWorker(adapter_factory=lambda: adapter)
        try:
            result = worker.find(1, {"name": "A"}, {}, timeout_ms=2000)
            self.assertEqual(result["elements"][0]["name"], "A")
            self.assertIn("elapsed_ms", result)
            self.assertTrue(worker.status()["started"])
        finally:
            worker.close()
        self.assertTrue(adapter.initialized)
        self.assertTrue(adapter.uninitialized)

    def test_worker_invokes_in_com_thread(self):
        adapter = _FakeAdapter({"elements": []})
        worker = UiaWorker(adapter_factory=lambda: adapter)
        try:
            result = worker.invoke(1, {"name": "A"}, {"name": "A"}, {}, timeout_ms=2000)
            self.assertEqual(result["method"], "invoke")
            self.assertTrue(result["invoked"])
        finally:
            worker.close()

    def test_worker_maps_errors(self):
        worker = UiaWorker(adapter_factory=lambda: _FakeAdapter(error=UiaWindowGone("gone")))
        try:
            with self.assertRaises(UiaWindowGone):
                worker.find(1, {}, {}, timeout_ms=2000)
        finally:
            worker.close()

    def test_worker_unavailable_when_adapter_init_fails(self):
        def factory():
            raise UiaUnavailable("missing")

        worker = UiaWorker(adapter_factory=factory)
        with self.assertRaises(UiaUnavailable):
            worker.find(1, {}, {}, timeout_ms=1000)
        worker.close()

    def test_worker_timeout_marks_unhealthy_and_requires_restart(self):
        class SlowAdapter(_FakeAdapter):
            def find(self, hwnd, locator, options):
                time.sleep(0.25)
                return {"elements": []}

        worker = UiaWorker(adapter_factory=lambda: SlowAdapter())
        try:
            with self.assertRaises(UiaTimeout):
                worker.find(1, {}, {}, timeout_ms=50)
            # P1 隐患 6: never wait for the stuck COM call to recover.
            self.assertTrue(worker.status()["unhealthy"])
            with self.assertRaises(UiaUnavailable) as raised:
                worker.find(2, {}, {}, timeout_ms=1000)
            self.assertIn("重启 daemon", raised.exception.hint or "")
        finally:
            worker.close()

    def test_invoke_point_runs_in_uia_worker(self):
        worker = UiaWorker(adapter_factory=lambda: _FakeAdapter())
        try:
            result = worker.invoke_point(320, 240, click_type="click", timeout_ms=1000)
            self.assertEqual(result["method"], "invoke")
            self.assertTrue(result["invoked"])
            self.assertEqual(result["center"], {"x": 320, "y": 240})
        finally:
            worker.close()


class AdapterTraversalTest(unittest.TestCase):
    def setUp(self):
        self.child = _FakeControl(
            name="Save",
            automation_id="btnSave",
            bounds=(120, 140, 200, 170),
            patterns={10000, 10002},
        )
        self.password = _FakeControl(
            name="Password",
            automation_id="editPassword",
            control_type=50004,
            class_name="Edit",
            bounds=(120, 180, 300, 205),
            password=True,
            patterns={10002},
        )
        self.root = _FakeControl(
            name="Window",
            bounds=(100, 100, 700, 500),
            children=[self.child, self.password],
        )
        self.auto = _FakeAuto(self.root)
        # Import before patch.dict: patch.dict restores/removes modules created
        # inside its scope, which would otherwise reload win_capture per test.
        from vhcu_daemon import win_capture
        self.win = win_capture
        self._module_patch = patch.dict(sys.modules, {"uiautomation": self.auto})
        self._module_patch.start()
        self.original = {
            name: getattr(win_capture, name)
            for name in ("ensure_dpi_awareness", "window_rect", "window_dpi",
                         "window_pid", "window_title")
        }

    def tearDown(self):
        for name, value in self.original.items():
            setattr(self.win, name, value)
        self._module_patch.stop()

    def _adapter(self):
        self.win.ensure_dpi_awareness = lambda: None
        self.win.window_rect = lambda hwnd: (100, 100, 600, 400)
        self.win.window_dpi = lambda hwnd: 96
        self.win.window_pid = lambda hwnd: 42
        self.win.window_title = lambda hwnd: "Fake Window"
        adapter = UiaAutomationAdapter()
        adapter.initialize()
        return adapter

    def test_exact_name_and_physical_bounds(self):
        adapter = self._adapter()
        try:
            result = adapter.find(1, {"name": "save"}, {
                "max_depth": 2, "limit": 10, "node_budget": 50, "include_offscreen": True,
            })
        finally:
            adapter.uninitialize()
        self.assertEqual(result["count"], 1)
        element = result["elements"][0]
        self.assertEqual(element["name"], "Save")
        self.assertEqual(element["bounds"]["x"], 120)
        self.assertEqual(element["bounds"]["width"], 80)
        self.assertEqual(element["center"], {"x": 160, "y": 155})
        self.assertEqual(element["patterns"], ["Invoke", "Value"])
        self.assertIsNone(element["value"])
        self.assertFalse(element["is_password"])

    def test_contains_is_explicit(self):
        adapter = self._adapter()
        try:
            exact = adapter.find(1, {"name": "Sav"}, {
                "max_depth": 2, "limit": 10, "node_budget": 50, "include_offscreen": True,
            })
            contains = adapter.find(1, {"name": "Sav", "name_match": "contains"}, {
                "max_depth": 2, "limit": 10, "node_budget": 50, "include_offscreen": True,
            })
        finally:
            adapter.uninitialize()
        self.assertEqual(exact["count"], 0)
        self.assertEqual(contains["count"], 1)

    def test_name_is_case_insensitive(self):
        adapter = self._adapter()
        try:
            result = adapter.find(1, {"name": "SAVE"}, {
                "max_depth": 2, "limit": 10, "node_budget": 50, "include_offscreen": True,
            })
        finally:
            adapter.uninitialize()
        self.assertEqual(result["count"], 1)

    def test_password_never_exposes_value_or_value_pattern(self):
        adapter = self._adapter()
        try:
            result = adapter.find(1, {"automation_id": "editPassword"}, {
                "max_depth": 2, "limit": 10, "node_budget": 50, "include_offscreen": True,
            })
        finally:
            adapter.uninitialize()
        self.assertEqual(result["count"], 1)
        element = result["elements"][0]
        self.assertTrue(element["is_password"])
        self.assertIsNone(element["value"])
        self.assertTrue(element["value_redacted"])
        self.assertNotIn("Value", element["patterns"])

    def test_limit_and_control_type(self):
        adapter = self._adapter()
        try:
            result = adapter.find(1, {"control_type": "Button"}, {
                "max_depth": 2, "limit": 1, "node_budget": 50, "include_offscreen": True,
            })
        finally:
            adapter.uninitialize()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["elements"][0]["control_type"], "Button")

    def test_allow_value_read_returns_nonpassword_value_only(self):
        self.child._patterns.add(10002)
        adapter = self._adapter()
        try:
            denied = adapter.find(1, {"name": "Save"}, {
                "max_depth": 2, "limit": 10, "node_budget": 50,
                "include_offscreen": True, "allow_value_read": False,
            })
            allowed = adapter.find(1, {"name": "Save"}, {
                "max_depth": 2, "limit": 10, "node_budget": 50,
                "include_offscreen": True, "allow_value_read": True,
            })
        finally:
            adapter.uninitialize()
        self.assertIsNone(denied["elements"][0]["value"])
        self.assertEqual(allowed["elements"][0]["value"], "secret")

    def test_password_value_stays_redacted_even_when_allowed(self):
        adapter = self._adapter()
        try:
            result = adapter.find(1, {"automation_id": "editPassword"}, {
                "max_depth": 2, "limit": 10, "node_budget": 50,
                "include_offscreen": True, "allow_value_read": True,
            })
        finally:
            adapter.uninitialize()
        element = result["elements"][0]
        self.assertIsNone(element["value"])
        self.assertTrue(element["value_redacted"])

    def test_invoke_prefers_invoke_pattern(self):
        adapter = self._adapter()
        try:
            result = adapter.invoke(
                1,
                {"name": "Save"},
                {
                    "name": "Save",
                    "automation_id": "btnSave",
                    "control_type": "Button",
                    "bounds": {"x": 120, "y": 140, "width": 80, "height": 30},
                },
                {"max_depth": 2, "limit": 10, "node_budget": 50, "include_offscreen": True},
            )
        finally:
            adapter.uninitialize()
        self.assertEqual(result["method"], "invoke")
        self.assertTrue(result["invoked"])
        self.assertEqual(self.child.invoke_pattern.called, 1)

    def test_missing_window_raises_window_gone(self):
        self.auto.root = None
        adapter = self._adapter()
        try:
            with self.assertRaises(UiaWindowGone):
                adapter.find(1, {}, {
                    "max_depth": 2, "limit": 10, "node_budget": 50, "include_offscreen": True,
                })
        finally:
            adapter.uninitialize()

    def test_unknown_control_type_fails(self):
        adapter = self._adapter()
        try:
            with self.assertRaises(UiaFailed):
                adapter.find(1, {"control_type": "Nope"}, {
                    "max_depth": 2, "limit": 10, "node_budget": 50, "include_offscreen": True,
                })
        finally:
            adapter.uninitialize()


if __name__ == "__main__":
    unittest.main()
