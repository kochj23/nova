#!/usr/bin/env python3
"""Tests for nova_toil.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_toil.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tl = _load("toil_under_test", SCRIPT)
tl.log = lambda m: None
SRC = SCRIPT.read_text()
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


class _Cur:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.sql = []
        self._rows = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._rows = []
        for k, v in self.answers.items():
            if k in sql:
                self._rows = list(v(sql, params) if callable(v) else v)
                break

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class TestSecurity(unittest.TestCase):
    def test_no_credentials(self):
        self.assertIsNone(re.search(r"(password|token|secret)\s*=\s*['\"]\w", SRC, re.I))

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))

    def test_never_deletes(self):
        self.assertNotIn("DELETE FROM", SRC)
        self.assertNotIn("TRUNCATE", SRC)


class TestPerformance(unittest.TestCase):
    def test_bucket_10k_rows_fast(self):
        rows = [(f"c{i % 50}", NOW - timedelta(hours=i % 800), 5) for i in range(10000)]
        t = time.time()
        top = tl.top_causes(tl.weekly_buckets(rows, NOW))
        self.assertLess(time.time() - t, 1.0)
        self.assertEqual(len(top), 3)


class TestRetry(unittest.TestCase):
    def test_report_notify_failure_fails_open(self):
        # RETRY GAP: report -> nova_notify.notify (single attempt; notify itself never raises)
        cur = _Cur({"to_regclass": [("toil_events",)], "FROM toil_events WHERE at": [], "count(*)": [(0,)]})
        with patch.dict(sys.modules, {"nova_notify": None}):
            out = tl.report(cur, dry=False, now=NOW)
        self.assertEqual(out["top"], [])


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        self.assertEqual(tl.selftest(), 0)

    def test_classification(self):
        self.assertEqual(tl.classify_queue("TASK FAILING: yt_subs_baseline — 5 failures"),
                         ("queue_interrupt", "task failing:yt_subs_baseline"))
        self.assertEqual(tl.classify_queue("OVERNIGHT: :rotating_light: *LLM down 3x*")[1], "overnight:llm down #x")
        self.assertIsNone(tl.classify_queue("Toil automation candidate: x (2026-W42)"))
        self.assertIsNone(tl.classify_action("feature", "x", "y"))
        self.assertEqual(tl.classify_action("fix", "nova-core7", "z"), ("manual_fix", "fix:nova-core#"))

    def test_trend_math(self):
        self.assertEqual(tl.trend(0, [0, 0, 0, 0]), (0.0, "flat"))
        self.assertEqual(tl.trend(3, [0, 0, 0, 0])[1], "new")
        self.assertEqual(tl.trend(6, [4, 4, 4, 4])[1], "up")
        self.assertEqual(tl.trend(5, [4, 4, 4, 4])[1], "flat")
        self.assertEqual(tl.trend(1, [4, 4, 4, 4])[1], "down")

    def test_buckets_and_share(self):
        rows = [("a", NOW - timedelta(days=d), 1) for d in (1, 2, 8, 15, 40)]
        self.assertEqual(tl.weekly_buckets(rows, NOW), {"a": [2, 1, 1, 0, 0]})
        self.assertAlmostEqual(tl.toil_share(1, 3), 0.25)
        self.assertIsNone(tl.toil_share(0, 0))

    def test_machine_toil_not_human(self):
        self.assertFalse(tl.event("x", 1, "auto_restart", "c", NOW)["human"])
        self.assertTrue(tl.event("x", 1, "manual_rerun", "c", NOW)["human"])


class TestIntegration(unittest.TestCase):
    def test_collect_is_idempotent_insert(self):
        cur = _Cur({"FROM claude_queue": [(1, NOW, "INCIDENT: Scheduler — bug")], "to_regclass": [(None,)]})
        out = tl.collect(cur, 35)
        ins = [s for s, _ in cur.sql if "INSERT INTO toil_events" in s]
        self.assertEqual(len(ins), out["found"])
        self.assertTrue(all("ON CONFLICT (source, ref_id) DO NOTHING" in s for s in ins))

    def test_render_report(self):
        title, body = tl.render_report("2026-W42", [("incident:scheduler", 9, 2.0, "up")], 12, 140, 3, 0.2,
                                       {"queue_interrupt": 12}, [("restart:x", 3, 0, "new")])
        self.assertIn("incident:scheduler", title)
        self.assertIn("20%", body)
        self.assertIn("automation candidate", body)


class TestFunctional(unittest.TestCase):
    def test_report_notifies_and_files_top_cause_once(self):
        rows = [("incident:scheduler", NOW - timedelta(days=1), 15, True, "queue_interrupt")] * 4
        calls = []
        fake = type(sys)("nova_notify")
        fake.notify = lambda *a, **k: calls.append(k)
        cur = _Cur({"to_regclass": [("toil_events",)], "SELECT detail FROM toil_events": [("ex",)],
                    "FROM toil_events WHERE at": rows, "count(*)": [(4,)],
                    "SELECT id FROM claude_queue": [], "INSERT INTO claude_queue": [(77,)]})
        with patch.dict(sys.modules, {"nova_notify": fake}):
            out = tl.report(cur, now=NOW)
        self.assertEqual(out["queued"], 77)
        self.assertEqual(calls[0]["dedup_key"], "toil-weekly-2026-W42")
        self.assertEqual(calls[0]["category"], "digest")
        self.assertAlmostEqual(out["share"], 0.5)

    def test_report_does_not_refile_when_already_queued(self):
        rows = [("c", NOW - timedelta(days=1), 15, True, "manual_fix")]
        cur = _Cur({"to_regclass": [("toil_events",)], "SELECT detail FROM toil_events": [],
                    "FROM toil_events WHERE at": rows, "count(*)": [(0,)], "SELECT id FROM claude_queue": [(5,)]})
        with patch.dict(sys.modules, {"nova_notify": None}):
            out = tl.report(cur, now=NOW)
        self.assertIsNone(out["queued"])
        self.assertFalse(any("INSERT INTO claude_queue" in s for s, _ in cur.sql))


class TestFrame(unittest.TestCase):
    def test_help_and_selftest_exit_zero(self):
        env = dict(os.environ, NOVA_TEST_QUIET="1")
        for arg in ("--help", "--selftest"):
            r = subprocess.run([sys.executable, str(SCRIPT), arg], capture_output=True, timeout=30, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_does_not_run_main(self):
        self.assertTrue(hasattr(tl, "main"))


if __name__ == "__main__":
    unittest.main()
