"""P6: PID-record reclaim and command deadline helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from vhcu_daemon.cli import deadline_expired
from vhcu_daemon.pidfile import (
    reclaim_orphans,
    record_path,
    remove_record,
    run_dir,
    write_record,
)

WINDOWS = os.name == "nt"


class DeadlineTest(unittest.TestCase):
    def test_missing_or_invalid_deadline_never_expires(self):
        self.assertFalse(deadline_expired({}, 1000))
        self.assertFalse(deadline_expired({"deadlineMs": None}, 1000))
        self.assertFalse(deadline_expired({"deadlineMs": "abc"}, 1000))
        self.assertFalse(deadline_expired({"deadlineMs": 0}, 1000))
        self.assertFalse(deadline_expired({"deadlineMs": -5}, 1000))

    def test_future_deadline_is_admitted_and_past_is_refused(self):
        self.assertFalse(deadline_expired({"deadlineMs": 2000}, 1000))
        self.assertFalse(deadline_expired({"deadlineMs": 1000}, 1000))
        self.assertTrue(deadline_expired({"deadlineMs": 999}, 1000))


@unittest.skipUnless(WINDOWS, "PID records are Windows-first")
class PidRecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"VHCU_PID_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _record(self, pid: int, **fields) -> "os.PathLike[str]":
        path = run_dir() / f"vhcu-{pid}.json"
        payload = {
            "version": 1,
            "pid": pid,
            "parentPid": fields.get("parentPid", 999998),
            "createTime": fields.get("createTime", 123),
            "image": fields.get("image", "python.exe"),
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_write_and_remove_own_record(self):
        path = write_record(os.getpid())
        self.assertEqual(path, record_path(os.getpid()))
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["parentPid"], os.getpid())
        self.assertGreater(data["createTime"], 0)
        self.assertTrue(remove_record(path))
        self.assertFalse(path.exists())

    def test_remove_refuses_a_record_owned_by_another_pid(self):
        path = self._record(999999)
        self.assertFalse(remove_record(path))
        self.assertTrue(path.exists())

    def test_dead_pid_record_is_cleared(self):
        path = self._record(999999)
        results = reclaim_orphans()
        self.assertFalse(path.exists())
        self.assertTrue(any(item.get("action") == "stale-record" for item in results))

    def test_live_parent_record_is_never_touched(self):
        path = self._record(999999, parentPid=os.getpid())
        reclaim_orphans()
        self.assertTrue(path.exists())

    def test_malformed_record_is_removed(self):
        path = run_dir() / "vhcu-999999.json"
        path.write_text("not-json", encoding="utf-8")
        reclaim_orphans()
        self.assertFalse(path.exists())

    def test_identity_mismatch_never_kills_a_live_process(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            path = self._record(child.pid, createTime=123)
            reclaim_orphans()
            self.assertIsNone(child.poll(), "identity check must protect a live process")
            self.assertTrue(path.exists())
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except Exception:  # noqa: BLE001
                child.kill()
