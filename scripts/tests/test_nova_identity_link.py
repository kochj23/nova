#!/usr/bin/env python3
"""Tests for nova_identity_link.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_identity_link.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_identity_link_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


il = _load()


class _Cur:
    def __init__(self, presence, bt):
        self.presence, self.bt, self.sql, self.many = presence, bt, [], None
        self._last = ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self._last = sql

    def fetchall(self):
        return self.presence if "telemetry.presence" in self._last else self.bt

    def executemany(self, sql, rows):
        self.many = (sql, list(rows))


def _data(n_slots=40):
    presence = [("always", s) for s in range(n_slots)] + [("alice", s) for s in range(20)]
    bt = [("fp-phone", "fingerprint", s) for s in range(20)] + \
         [("aa:bb", "mac", s) for s in range(n_slots)]          # always-on speaker
    return presence, bt


def _run(presence, bt, dry_run=False, days=7):
    cur = _Cur(presence, bt)
    conn = MagicMock(cursor=MagicMock(return_value=cur))
    with patch.object(il, "_connect", return_value=conn), redirect_stdout(io.StringIO()) as out:
        rc = il.main(days, 0.6, dry_run)
    return rc, cur, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")

    def test_never_writes_device_owner(self):
        writes = re.findall(r"(INSERT INTO|UPDATE|DELETE FROM)\s+(?!SET\b)([\w.]+)", SRC)
        self.assertEqual({t for _, t in writes}, {"telemetry.identity_link_proposal"})

    def test_interpolated_sql_only_from_typed_ints(self):
        self.assertIn("ap.add_argument(\"--days\", type=int", SRC)
        self.assertIn("VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", SRC)


class TestPerformance(unittest.TestCase):
    def test_scoring_many_devices(self):
        presence = [("alice", s) for s in range(0, 400, 2)] + [("bob", s) for s in range(1, 400, 3)]
        bt = [(f"dev{d}", "mac", s) for d in range(300) for s in range(d % 50, d % 50 + 30)]
        t0 = time.perf_counter()
        rc, cur, _ = _run(presence, bt, dry_run=True)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(rc, 0)


class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff(self):
        err = il.psycopg2.OperationalError("Operation timed out")
        sentinel = object()
        with patch.object(il.psycopg2, "connect", side_effect=[err, err, sentinel]) as c, \
             patch("time.sleep") as sl:
            self.assertIs(il._connect("dsn"), sentinel)
        self.assertEqual(c.call_count, 3)
        self.assertEqual([x[0][0] for x in sl.call_args_list], [5, 10])

    def test_connect_gives_up_after_tries(self):
        err = il.psycopg2.OperationalError("down")
        with patch.object(il.psycopg2, "connect", side_effect=err) as c, patch("time.sleep"):
            with self.assertRaises(il.psycopg2.OperationalError):
                il._connect("dsn", tries=2)
        self.assertEqual(c.call_count, 2)


class TestUnit(unittest.TestCase):
    def test_always_present_person_is_skipped(self):
        presence = [("always", s) for s in range(40)]
        rc, cur, out = _run(presence, [])
        self.assertEqual(rc, 0)
        self.assertIn("skipping 'always'", out)
        self.assertIn("nothing to correlate", out)

    def test_thin_history_filtered(self):
        rc, cur, out = _run([("alice", s) for s in range(il.MIN_PERSON_SLOTS - 1)], [])
        self.assertIn("nothing to correlate", out)
        self.assertEqual(len(cur.sql), 2)  # DDL + presence only


class TestIntegration(unittest.TestCase):
    def test_reads_presence_and_bluetooth_excluding_owned(self):
        _, cur, _ = _run(*_data(), dry_run=True)
        joined = "\n".join(cur.sql)
        self.assertIn("FROM telemetry.presence", joined)
        self.assertIn("FROM telemetry.bluetooth", joined)
        self.assertIn("NOT IN\n              (SELECT mac FROM telemetry.device_owner)", joined)
        self.assertIn("interval '7 days'", joined)


class TestFunctional(unittest.TestCase):
    def test_phi_links_phone_not_always_on_speaker(self):
        rc, cur, out = _run(*_data())
        rows = cur.many[1]
        self.assertEqual([(r[0], r[2]) for r in rows], [("fp-phone", "alice")])
        self.assertEqual(rows[0][5], 1.0)
        self.assertIn("would auto-apply", out)
        self.assertIn("ON CONFLICT (device_key, person)", cur.many[0])

    def test_dry_run_stores_nothing(self):
        _, cur, out = _run(*_data(), dry_run=True)
        self.assertIsNone(cur.many)
        self.assertIn("proposals above score", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        with patch.object(il.psycopg2, "connect") as c:
            _load()
        c.assert_not_called()


if __name__ == "__main__":
    unittest.main()
