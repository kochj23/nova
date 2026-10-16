#!/usr/bin/env python3
"""Tests for nova_noise_rollup.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Little Mister, 2026-10-15 (#22). PG is mocked throughout.
Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_noise_rollup.py"
spec = importlib.util.spec_from_file_location("noise_rollup_under_test", SCRIPT)
nr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nr)
nr.log = lambda m: None
SRC = SCRIPT.read_text()
D = date(2026, 10, 15)
T = datetime(2026, 10, 15, 9, 0)


class _Cur:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.sql = rows or [], fail, []

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("pg down")
        self.sql.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return None


class TestSecurity(unittest.TestCase):
    def test_parameterized_and_no_secrets(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        self.assertIsNone(re.search(r"(password|token|secret)\s*=\s*['\"]\w", SRC, re.I))

    def test_digest_is_info_never_a_page(self):
        n = MagicMock()
        nr.digest(_Cur([("s", "t", 3, T, T)]), D, notify=n)
        self.assertEqual((n.call_args[1]["level"], n.call_args[1]["category"]), ("info", "digest"))


class TestPerformance(unittest.TestCase):
    def test_render_bounded_on_10k_rows(self):
        rows = [(f"src{i % 5}", f"cond {i}", i, T, T) for i in range(10000)]
        t0 = time.perf_counter()
        title, body = nr.render(rows, D)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertLessEqual(body.count("\n"), nr.MAX_LINES + 1)
        self.assertIn("10000 condition(s) from 5 detector(s)", title)


class TestRetry(unittest.TestCase):
    def test_record_never_raises(self):
        # RETRY GAP: record — one write; a PG failure is logged and returns False, the detector carries on
        with patch.object(nr, "ensure_schema", lambda c: None):
            self.assertFalse(nr.record("src", "k", "t", cur=_Cur(fail=True)))
        self.assertFalse(nr.record("", "k", "t", cur=_Cur()))


class TestUnit(unittest.TestCase):
    def test_render_groups_by_source_and_sorts_by_count(self):
        title, body = nr.render([("b", "x", 1, T, T), ("a", "y", 2, T, T), ("a", "z", 9, T, T)], D)
        lines = body.splitlines()
        self.assertEqual(lines[0], "*a*")
        self.assertTrue(lines[1].startswith("• z — seen 9x"))
        self.assertEqual(lines[3], "*b*")


class TestIntegration(unittest.TestCase):
    def test_record_upserts_one_row_per_day_source_key(self):
        cur = _Cur()
        with patch.object(nr, "ensure_schema", lambda c: None):
            self.assertTrue(nr.record("nova_home_reachability", "homekit-dark-Garage", "HomeKit: Garage",
                                      "warning", day=D, cur=cur))
        sql, params = cur.sql[-1]
        self.assertIn("ON CONFLICT (day, source, key) DO UPDATE SET count=noise_rollup.count+1", sql)
        self.assertEqual(params[:3], (D, "nova_home_reachability", "homekit-dark-Garage"))

    def test_the_three_detectors_use_the_rollup(self):
        for name in ("nova_home_reachability.py", "nova_negative_space.py", "nova_output_drift.py"):
            self.assertIn("nova_noise_rollup", (SCRIPTS / name).read_text(), name)


class TestFunctional(unittest.TestCase):
    def test_digest_posts_once_and_marks_rows(self):
        n = MagicMock()
        cur = _Cur([("nova_output_drift", "Chronic down: x", 4, T, T)])
        self.assertEqual(nr.digest(cur, D, notify=n), 1)
        n.assert_called_once()
        self.assertEqual(n.call_args[1]["dedup_key"], "noise-rollup-2026-10-15")
        self.assertTrue(any("SET digested_at=now()" in s for s, _ in cur.sql))

    def test_empty_day_posts_nothing(self):
        n = MagicMock()
        self.assertEqual(nr.digest(_Cur([]), D, notify=n), 0)
        n.assert_not_called()

    def test_summary_lines_for_the_daily_summary(self):
        cur = _Cur([("nova_home_reachability", 4, 120)])
        self.assertEqual(nr.summary_lines(cur, D), ["nova_home_reachability: 4 condition(s), 120 sighting(s)"])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--digest", r.stdout)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)


if __name__ == "__main__":
    unittest.main()
