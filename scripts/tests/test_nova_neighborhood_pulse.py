#!/usr/bin/env python3
"""Tests for nova_neighborhood_pulse.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_neighborhood_pulse.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nnp_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


np_ = _load()
np_.notify = MagicMock()          # stub the outbound Slack path at module load


def _r(mi, d="N", src="scanner", text="[ch1] units respond", locs=None):
    return {"source": src, "text": text, "created_at": None, "mi": mi, "dir": d, "locs": locs}


WEEK = [_r(0.4, "NE", "fire", "[x] structure fire", [{"addr": "Main St & 1st Ave"}]),
        _r(2.0, "NE", locs=[{"addr": "Main St & 1st Ave"}, {"addr": "123 Elm St"}]),
        _r(4.9, "S"), _r(None, None), _r(7.0, "NE")]


class _Cur:
    def __init__(self, week, prior):
        self.answers = [week, prior]; self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchall(self):
        return self.answers.pop(0)


def _run(week, prior):
    cur = _Cur(week, prior)
    con = MagicMock(); con.cursor.return_value = cur
    np_.notify.reset_mock()
    with patch.object(np_.psycopg2, "connect", return_value=con):
        np_.main()
    return cur, con


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_interval_interpolation_is_int_only(self):
        # the only interpolated SQL forces %d (ints from code, never user input)
        self.assertIn("interval '%d hours' AND now() - interval '%d hours'", SRC)
        cur = _Cur([], [])
        with self.assertRaises(TypeError):
            np_._rows(cur, "1; DROP", 0)

    def test_house_numbers_never_listed_as_hotspots(self):
        _run(WEEK, [])
        self.assertNotIn("123 Elm", np_.notify.call_args.kwargs["body"])


class TestPerformance(unittest.TestCase):
    def test_10k_incidents_fast_and_body_capped(self):
        week = [_r(i % 9 / 2, "NESW"[i % 4:i % 4 + 1], locs=[{"addr": f"A{i % 50} & B"}]) for i in range(10_000)]
        t0 = time.perf_counter()
        _run(week, week[:5000])
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertLessEqual(len(np_.notify.call_args.kwargs["body"]), 2900)


class TestRetry(unittest.TestCase):
    def test_pg_failure_propagates_without_notify(self):
        # RETRY GAP: main()/psycopg2.connect — one attempt, no fallback; nothing is posted on failure
        np_.notify.reset_mock()
        with patch.object(np_.psycopg2, "connect", side_effect=Exception("pg down")) as c:
            with self.assertRaises(Exception):
                np_.main()
        self.assertEqual(c.call_count, 1)
        np_.notify.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_trend_arrow_and_proximity(self):
        _run(WEEK, [1, 2, 3, 4, 5, 6])
        kw = np_.notify.call_args.kwargs
        title = np_.notify.call_args[0][0]
        self.assertIn("5 calls, ↓-1", title)
        self.assertIn("Busiest direction: *northeast* (3 calls)", kw["body"])
        self.assertIn("1 within 1 mi · 2 within 2.5 mi · 3 within 5 mi", kw["body"])

    def test_too_few_incidents(self):
        _run(WEEK[:2], [])
        self.assertIn("Too few", np_.notify.call_args.kwargs["body"])

    def test_closest_lines_strip_tags(self):
        _run(WEEK, [])
        body = np_.notify.call_args.kwargs["body"]
        self.assertIn("~0.4 mi NE — structure fire", body)
        self.assertIn("Main St & 1st Ave (2)", body)


class TestIntegration(unittest.TestCase):
    def test_reads_week_then_prior_from_memories(self):
        cur, con = _run(WEEK, [])
        self.assertEqual(len(cur.sql), 2)
        self.assertIn("now() - interval '168 hours' AND now() - interval '0 hours'", cur.sql[0])
        self.assertIn("now() - interval '336 hours' AND now() - interval '168 hours'", cur.sql[1])
        self.assertIn("source IN ('scanner','fire')", cur.sql[0])
        con.close.assert_called_once()
        self.assertEqual(np_.notify.call_args.kwargs["dedup_key"], "pulse")


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_pulse(self):
        _run(WEEK, [])
        title = np_.notify.call_args[0][0]
        self.assertTrue(title.startswith("\U0001F4CD Neighborhood pulse — 5 calls, ↑+5"))
        self.assertEqual(np_.notify.call_args.kwargs["category"], "pulse")

    def test_null_locations_and_dirs_tolerated(self):
        _run([_r(1.0, None), _r(2.0, None), _r(3.0, None)], [])
        self.assertIn("Busiest direction: *?*", np_.notify.call_args.kwargs["body"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help: any invocation hits PG + Slack, so the frame check is an import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_neighborhood_pulse"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
