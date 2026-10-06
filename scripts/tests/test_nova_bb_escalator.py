#!/usr/bin/env python3
"""Tests for nova_bb_escalator.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_bb_escalator.py"
SRC = SCRIPT.read_text()


def _stub_logger():
    m = types.ModuleType("nova_logger")
    m.LOG_INFO, m.LOG_WARN, m.LOG_ERROR, m.LOG_DEBUG = "info", "warn", "error", "debug"
    m.log = MagicMock()
    return m


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_logger": _stub_logger()}):   # no log-file writes; restored after import
        spec.loader.exec_module(mod)
    return mod


bb = _load("bb_mod", SCRIPT)


def _reset():
    with bb._lock:
        bb._escalations.clear()
    bb.log.reset_mock()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_pure_logic_no_io(self):
        for needle in ("psycopg2", "urllib", "requests", "subprocess", "open("):
            self.assertNotIn(needle, SRC)

    def test_issue_id_is_opaque_never_evaluated(self):
        _reset()
        evil = "x'; select pg_sleep(9); --"
        ok, sfx = bb.should_notify(evil, "info")
        self.assertTrue(ok)
        self.assertIn(evil, bb.active_keys())
        self.assertEqual(sfx, "")
        _reset()


class TestPerformance(unittest.TestCase):
    def test_10k_issues_under_bound(self):
        _reset()
        t0 = time.perf_counter()
        for i in range(10_000):
            bb.should_notify(f"perf:{i}", "warning")
        for i in range(10_000):
            bb.should_notify(f"perf:{i}", "warning")        # all cooled down
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(bb.active_count(), 10_000)
        for i in range(10_000):
            bb._resolve_escalation(f"perf:{i}")
        self.assertEqual(bb.active_count(), 0)


class TestRetry(unittest.TestCase):
    def test_logger_failure_on_bump_propagates_but_state_is_consistent(self):
        # RETRY GAP: should_notify()/nova_logger.log — the one outbound call (log on severity bump) is
        # one-shot and unguarded: a raising logger escapes should_notify. State is already mutated
        # under the lock, and the lock is released by the context manager, so the gate recovers next call.
        _reset()
        bb.should_notify("r:1", "info")
        bb.log.side_effect = RuntimeError("log disk full")
        with bb._lock:
            bb._escalations["r:1"]["first_seen"] -= 3601
        try:
            with self.assertRaises(RuntimeError):
                bb.should_notify("r:1", "info")
            self.assertTrue(bb._lock.acquire(timeout=1)); bb._lock.release()
            bb.log.side_effect = None
            ok, sfx = bb.should_notify("r:1", "info")      # bump already applied; next call is a cooldown hit
            self.assertFalse(ok)
            with bb._lock:
                self.assertEqual(bb._escalations["r:1"]["severity"], "warning")
        finally:
            bb.log.side_effect = None
            _reset()

    def test_lock_is_released_after_every_path(self):
        _reset()
        bb.should_notify("l:1", "critical")
        bb.should_notify("l:1", "critical")
        bb._resolve_escalation("l:1")
        self.assertTrue(bb._lock.acquire(timeout=1))
        bb._lock.release()


class TestUnit(unittest.TestCase):
    def test_next_severity(self):
        self.assertEqual(bb._next_severity("info"), "warning")
        self.assertEqual(bb._next_severity("warning"), "critical")
        self.assertEqual(bb._next_severity("critical"), "critical")
        self.assertEqual(bb._next_severity("bogus"), "warning")   # unknown -> index 0 -> bump

    def test_first_detection_notifies_then_cooldown_suppresses(self):
        _reset()
        self.assertEqual(bb.should_notify("u:1", "info"), (True, ""))
        self.assertEqual(bb.should_notify("u:1", "info"), (False, ""))
        with bb._lock:
            self.assertEqual(bb._escalations["u:1"]["suppressed_count"], 1)
        _reset()

    def test_cooldown_expiry_notifies_with_context(self):
        _reset()
        bb.should_notify("u:2", "warning")
        bb.should_notify("u:2", "warning")
        with bb._lock:
            bb._escalations["u:2"]["last_notified"] -= 601
        ok, sfx = bb.should_notify("u:2", "warning")
        self.assertTrue(ok)
        self.assertIn("ongoing", sfx)
        self.assertIn("suppressed 1 alerts", sfx)
        _reset()

    def test_max_notifications_cap(self):
        _reset()
        bb.should_notify("u:3", "info")
        for _ in range(10):
            with bb._lock:
                bb._escalations["u:3"]["last_notified"] -= 301
            bb.should_notify("u:3", "info")
        with bb._lock:
            self.assertEqual(bb._escalations["u:3"]["notify_count"], 3)
        _reset()

    def test_auto_bump(self):
        _reset()
        bb.should_notify("u:4", "info")
        with bb._lock:
            bb._escalations["u:4"]["first_seen"] -= 3601
        ok, sfx = bb.should_notify("u:4", "info")
        self.assertTrue(ok)
        self.assertIn("ESCALATED to warning", sfx)
        bb.log.assert_called_once()
        _reset()

    def test_resolve_untracked(self):
        _reset()
        self.assertEqual(bb._resolve_escalation("nope"), (False, ""))

    def test_unknown_severity_falls_back_to_warning_rules(self):
        _reset()
        bb.should_notify("u:5", "weird")
        with bb._lock:
            bb._escalations["u:5"]["last_notified"] -= 301   # past info cooldown, inside warning's 600
        self.assertEqual(bb.should_notify("u:5", "weird"), (False, ""))
        _reset()


class TestIntegration(unittest.TestCase):
    def test_uses_shared_logger_not_print(self):
        self.assertIn("from nova_logger import log, LOG_WARN", SRC)
        self.assertNotIn("print(", SRC.split('if __name__ == "__main__":')[0])

    def test_big_brother_routes_through_this_module(self):
        bbsrc = (SCRIPTS / "nova_big_brother.py").read_text()
        self.assertIn("nova_bb_escalator", bbsrc)

    def test_notify_then_resolve_chain(self):
        _reset()
        bb.should_notify("i:1", "critical")
        self.assertEqual(bb.active_keys(), ["i:1"])
        tracked, sfx = bb._resolve_escalation("i:1")
        self.assertTrue(tracked)
        self.assertRegex(sfx, r" RESOLVED after \d+m")
        self.assertEqual(bb.active_count(), 0)


class TestFunctional(unittest.TestCase):
    def test_full_lifecycle_info_to_critical(self):
        _reset()
        bb.should_notify("f:1", "info")
        with bb._lock:
            bb._escalations["f:1"]["first_seen"] -= 3601
        bb.should_notify("f:1", "info")
        with bb._lock:
            self.assertEqual(bb._escalations["f:1"]["severity"], "warning")
            bb._escalations["f:1"]["first_seen"] -= 7201
        ok, sfx = bb.should_notify("f:1", "info")
        self.assertTrue(ok)
        self.assertIn("ESCALATED to critical", sfx)
        with bb._lock:
            bb._escalations["f:1"]["first_seen"] -= 99999
        self.assertEqual(bb.should_notify("f:1", "info"), (False, ""))   # critical never bumps; cooldown applies
        _reset()

    def test_concurrent_access_is_consistent(self):
        _reset()
        def worker(n):
            for i in range(200):
                bb.should_notify(f"t{n}:{i}", "info")
        ts = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(bb.active_count(), 800)
        _reset()


class TestFrame(unittest.TestCase):
    def test_selfcheck_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("self-check: PASS", r.stdout)

    def test_import_never_runs_selfcheck(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_bb_escalator"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
