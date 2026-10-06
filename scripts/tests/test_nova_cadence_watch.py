#!/usr/bin/env python3
"""Tests for nova_cadence_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_cadence_watch.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_cadence_watch_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cw = _load()


def _learned(age_s, median=60.0, n=50):
    return {"last_seen": datetime.now(timezone.utc) - timedelta(seconds=age_s), "median_gap_s": median, "n": n}


class _Cur:
    """Answers learn() with a per-table row; records every statement."""
    def __init__(self, rows, prev=None):
        self.rows, self.prev, self.sql, self._last = rows, prev, [], None

    def execute(self, sql, params=None):
        self.sql.append((sql, params)); self._last = sql

    def fetchone(self):
        s = self._last
        if "to_regclass" in s:
            return ("cadence_state",)
        if s.startswith("SELECT state FROM cadence_state"):
            return (self.prev,) if self.prev else None
        for table, row in self.rows.items():
            if f"FROM {table} " in s:
                return row
        return (None, None, None)

    def close(self):
        pass


def _conn(cur):
    return MagicMock(cursor=MagicMock(return_value=cur))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")

    def test_learn_sql_only_interpolates_fixed_identifiers(self):
        for table, col in cw.STREAMS:
            self.assertRegex(table, r"^[a-z_]+\.[a-z_]+$")
            self.assertRegex(col, r"^[a-z_]+$")
        self.assertIn('WHERE source=%s", (source,)', SRC)

    def test_missing_requires_witness(self):
        cur = MagicMock()
        for bad in (None, "", "   "):
            with self.assertRaises(ValueError):
                cw.record_missing(cur, "s", bad)
        cur.execute.assert_not_called()
        self.assertIn("CHECK (state <> 'SILENT'  OR witness IS NULL)", cw.DDL)


class TestPerformance(unittest.TestCase):
    def test_classify_10k(self):
        now = datetime.now(timezone.utc)
        items = [_learned(i, 30.0) for i in range(10_000)]
        t0 = time.perf_counter()
        states = [cw.classify(x, now)[0] for x in items]
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(states.count("SILENT"), 10_000 - 3601)


class TestRetry(unittest.TestCase):
    def test_memory_note_fails_open(self):
        # RETRY GAP: _write_silence_memory — single POST, False on failure
        with patch.object(cw.urllib.request, "urlopen", side_effect=OSError("down")) as u, \
             redirect_stdout(io.StringIO()):
            self.assertFalse(cw._write_silence_memory("telemetry.soil", 9000, 60))
        self.assertEqual(u.call_count, 1)

    def test_triage_failure_swallowed(self):
        # RETRY GAP: _alert_silent/_triage — one attempt, logged and swallowed
        with patch.object(cw, "_triage", side_effect=RuntimeError("triage down")), \
             redirect_stdout(io.StringIO()) as out:
            cw._alert_silent("s", 9000, 60, dry_run=False)
        self.assertIn("triage failed", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_classify_floor_and_factor(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(cw.classify(_learned(3000, median=1.0), now)[0], "OK")      # floor 3600 wins
        st, _, thr = cw.classify(_learned(5000, median=1000.0), now)
        self.assertEqual((st, thr), ("SILENT", 4000.0))

    def test_learn_insufficient(self):
        cur = MagicMock()
        for row in (None, (None, 5, 20), (datetime.now(timezone.utc), 30.0, 3), (datetime.now(timezone.utc), 0, 50)):
            cur.fetchone.return_value = row
            self.assertIsNone(cw.learn(cur, "telemetry.soil", "ts"))

    def test_human_age(self):
        self.assertEqual(cw._human_age(30), "30 seconds")
        self.assertEqual(cw._human_age(120), "2 minutes")
        self.assertEqual(cw._human_age(7200), "2.0 hours")
        self.assertEqual(cw._human_age(172800), "2.0 days")


class TestIntegration(unittest.TestCase):
    def test_upsert_writes_silent_without_witness(self):
        cur = _Cur({}, prev="OK")
        prev, trans = cw.upsert_cadence(cur, "telemetry.soil", "stream", _learned(9000), "SILENT", {"a": 1})
        self.assertEqual((prev, trans), ("OK", True))
        sql, params = cur.sql[-1]
        self.assertIn("INSERT INTO cadence_state", sql)
        self.assertIn("witness=NULL", sql)
        self.assertEqual(params[4], "SILENT")

    def test_alert_routes_through_triage_with_cadence_category(self):
        with patch.object(cw, "_triage", return_value={"decision": "post"}) as t, redirect_stdout(io.StringIO()):
            cw._alert_silent("telemetry.soil", 9000, 60, dry_run=False)
        kw = t.call_args.kwargs
        self.assertEqual(kw["category"], "cadence")
        self.assertEqual(kw["dedup_key"], "cadence:telemetry.soil")
        self.assertNotIn("missing", t.call_args[0][0].lower())


class TestFunctional(unittest.TestCase):
    def _rows(self):
        now = datetime.now(timezone.utc)
        return {"telemetry.soil": (now - timedelta(hours=10), 60.0, 50),       # SILENT
                "telemetry.weather": (now - timedelta(seconds=30), 60.0, 50)}   # OK

    def test_run_once_golden_path(self):
        cur = _Cur(self._rows(), prev="OK")
        with patch.object(cw.psycopg2, "connect", return_value=_conn(cur)), \
             patch.object(cw, "_alert_silent") as al, patch.object(cw, "_write_silence_memory") as wm, \
             redirect_stdout(io.StringIO()):
            s = cw.run_once()
        self.assertEqual([x["source"] for x in s["silent"]], ["telemetry.soil"])
        self.assertEqual(s["ok"], 1)
        self.assertEqual(len(s["skipped"]), len(cw.STREAMS) - 2)
        al.assert_called_once()
        wm.assert_called_once()
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS" in q for q, _ in cur.sql))

    def test_dry_run_writes_nothing(self):
        cur = _Cur(self._rows())
        with patch.object(cw.psycopg2, "connect", return_value=_conn(cur)), \
             patch.object(cw, "_triage") as tr, patch.object(cw.urllib.request, "urlopen") as u, \
             redirect_stdout(io.StringIO()):
            rc = cw.main(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse(any(re.match(r"\s*(INSERT|UPDATE|CREATE)", q) for q, _ in cur.sql))
        tr.assert_not_called()
        u.assert_not_called()

    def test_learn_error_is_skipped(self):
        cur = _Cur({})
        orig = cur.execute
        cur.execute = lambda sql, params=None: (_ for _ in ()).throw(RuntimeError("no table")) \
            if "telemetry.soil" in sql else orig(sql, params)
        with patch.object(cw.psycopg2, "connect", return_value=_conn(cur)), redirect_stdout(io.StringIO()):
            s = cw.run_once(dry_run=True)
        self.assertIn({"source": "telemetry.soil", "why": "no table"}, s["skipped"])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(cw.psycopg2, "connect") as c:
            _load()
        c.assert_not_called()


if __name__ == "__main__":
    unittest.main()
