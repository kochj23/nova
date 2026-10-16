#!/usr/bin/env python3
"""Tests for nova_ledger_aging.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Little Mister, 2026-10-15 (#25). PG is mocked throughout.
Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ledger_aging.py"
spec = importlib.util.spec_from_file_location("ledger_aging_under_test", SCRIPT)
la = importlib.util.module_from_spec(spec)
spec.loader.exec_module(la)
la.log = lambda m: None
SRC = SCRIPT.read_text()


class _Cur:
    def __init__(self, answers=None, missing=()):
        self.answers, self.missing, self.sql = answers or {}, set(missing), []
        self._rows = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._rows = []
        if "to_regclass" in sql:
            self._rows = [(params[0] not in self.missing,)]
            return
        for k, v in self.answers.items():
            if k in sql:
                self._rows = list(v)
                break

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class TestSecurity(unittest.TestCase):
    def test_never_deletes_and_never_names_a_cause(self):
        self.assertNotRegex(SRC, r"\bDELETE\s+FROM\b")
        self.assertNotIn("SET cause", SRC)
        self.assertIsNone(re.search(r"(password|token|secret)\s*=\s*['\"]\w", SRC, re.I))

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))


class TestPerformance(unittest.TestCase):
    def test_rules_fast_on_100k(self):
        t0 = time.perf_counter()
        for i in range(100000):
            la.hotwash_reason("rule", i % 10)
            la.buick8_reason(i % 10)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_missing_ledgers_are_skipped(self):
        # RETRY GAP: run — one pass a day; a ledger that does not exist yet is skipped, not an error
        cur = _Cur(missing={"hotwash", "unexplained_events", "postmortem_actions", "tinker_log"})
        self.assertEqual(la.run(cur, dry=True), {"hotwash": 0, "buick8_dormant": 0, "postmortem_actions": 0,
                                                 "tinker_synced": 0})


class TestUnit(unittest.TestCase):
    def test_hotwash_rules(self):
        self.assertIn("fix_kind=none", la.hotwash_reason("none", 1.5))
        self.assertIsNone(la.hotwash_reason("none", 0.5))
        self.assertIsNone(la.hotwash_reason("rule", 6.9))
        self.assertIn("not grouped", la.hotwash_reason("threshold", 7.0))

    def test_buick8_dormant_keeps_cause_unknown(self):
        self.assertIsNone(la.buick8_reason(6.9))
        r = la.buick8_reason(10)
        self.assertIn("cause still unknown", r)
        self.assertIn("reopens", r)

    def test_pm_action_rule(self):
        self.assertIsNone(la.pm_action_reason("queued", 20))
        self.assertIsNotNone(la.pm_action_reason("queued", 21))
        self.assertIsNotNone(la.pm_action_reason(None, 30))
        self.assertIsNone(la.pm_action_reason("in_progress", 60))   # someone is on it

    def test_tinker_status(self):
        self.assertEqual(la.tinker_status("pending_human", "executed"), "executed")
        self.assertIsNone(la.tinker_status("blocked", "blocked"))
        self.assertIsNone(la.tinker_status("pending_human", None))


class TestIntegration(unittest.TestCase):
    def test_buick8_reopens_dormant_entries(self):
        bsrc = (SCRIPTS / "nova_buick8_log.py").read_text()
        self.assertIn("WHEN status='dormant' THEN 'open'", bsrc)


class TestFunctional(unittest.TestCase):
    def test_run_closes_with_reasons(self):
        cur = _Cur({"FROM hotwash WHERE status='open'": [(1, "none", 2.0), (2, "rule", 1.0), (3, "rule", 8.0)],
                    "FROM unexplained_events WHERE status='open'": [(7, 9.0), (8, 1.0)],
                    "FROM postmortem_actions a LEFT JOIN": [(11, 90, "queued", 30.0), (12, None, None, 2.0)],
                    "FROM tinker_log t JOIN": [(21, "pending_human", "rejected"), (22, "blocked", "blocked")]})
        out = {"hotwash": la.age_hotwash(cur, False), "buick8_dormant": la.age_buick8(cur, False),
               "postmortem_actions": la.age_pm_actions(cur, False), "tinker_synced": la.sync_tinker(cur, False)}
        self.assertEqual(out, {"hotwash": 2, "buick8_dormant": 1, "postmortem_actions": 1, "tinker_synced": 1})
        hw = cur.ran("UPDATE hotwash SET status='rolled_up'")
        self.assertEqual([p[1] for _, p in hw], [1, 3])
        self.assertTrue(all(p[0].startswith("aged out") for _, p in hw))
        self.assertEqual(cur.ran("UPDATE unexplained_events SET status='dormant'")[0][1][1], 7)
        q = cur.ran("UPDATE claude_queue SET status='aged_out'")[0][1]
        self.assertEqual(q[1], 90)
        self.assertEqual(cur.ran("UPDATE tinker_log")[0][1], ("rejected", 21))

    def test_dry_run_writes_nothing(self):
        cur = _Cur({"FROM hotwash WHERE status='open'": [(1, "none", 5.0)]})
        self.assertEqual(la.age_hotwash(cur, True), 1)
        self.assertEqual(cur.ran("UPDATE"), [])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--run", r.stdout)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)


if __name__ == "__main__":
    unittest.main()
