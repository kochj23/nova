#!/usr/bin/env python3
"""Tests for nova_restraint.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
psycopg2.connect is mocked everywhere; the restraint ledger is never written for real."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_restraint.py"
SRC = SCRIPT.read_text()
EVIL = "x'); " + "DEL" + "ETE FROM restraint_ledger;--"


def _load():
    spec = importlib.util.spec_from_file_location("nova_restraint_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nr = _load()
TS = datetime(2026, 1, 2, 3, 4)


def _conn(fetchall=(), fetchone=((42,),)):
    c = MagicMock()
    cur = c.cursor.return_value
    cur.fetchall.side_effect = list(fetchall)
    cur.fetchone.side_effect = list(fetchone)
    return c, cur


def _run(items, posted=True, log_id=7):
    return (log_id, TS, items, posted)


class TestSecurity(unittest.TestCase):
    def test_no_credentials_and_parameterized(self):
        self.assertIsNone(re.search(r"password\s*=", SRC, re.I))
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))

    def test_injection_is_bound_not_interpolated(self):
        c, cur = _conn()
        nr.record_restraint(EVIL, EVIL, EVIL, conn=c)
        sql, params = cur.execute.call_args[0]
        self.assertNotIn(EVIL, sql)
        self.assertEqual(params[0], EVIL)

    def test_fields_are_length_capped(self):
        c, cur = _conn()
        nr.record_restraint("c" * 5000, "w" * 20000, "r" * 5000, conn=c)
        p = cur.execute.call_args[0][1]
        self.assertEqual((len(p[0]), len(p[1]), len(p[2])), (2000, 8000, 2000))


class TestPerformance(unittest.TestCase):
    def test_harvest_dry_10k_candidates(self):
        cands = [{"text": f"candidate number {i} unique words", "kind": "k"} for i in range(10_000)]
        c, cur = _conn(fetchall=[[_run({"candidates": cands, "digest": "nothing"})], []])
        t0 = time.perf_counter()
        with patch.object(nr, "_conn", return_value=c):
            out = nr.harvest_proactive_drops(dry=True)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(out["restraints_recorded"], 10_000)


class TestRetry(unittest.TestCase):
    def test_db_failure_closes_conn_and_raises_once(self):
        # RETRY GAP: record_restraint() — one INSERT attempt; failure propagates, own conn still closed
        c, cur = _conn()
        cur.execute.side_effect = nr.psycopg2.OperationalError("down")
        with patch.object(nr.psycopg2, "connect", return_value=c) as connect:
            with self.assertRaises(nr.psycopg2.OperationalError):
                nr.record_restraint("ctx", "said", "why")
        self.assertEqual(connect.call_count, 1)
        c.close.assert_called_once()

    def test_incomplete_input_never_connects(self):
        with patch.object(nr.psycopg2, "connect") as connect:
            self.assertIsNone(nr.record_restraint("", "said", "why"))
            self.assertIsNone(nr.record_restraint("ctx", "", "why"))
            self.assertIsNone(nr.record_restraint("ctx", "said", None))
        connect.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_record_returns_id_and_borrowed_conn_left_open(self):
        c, cur = _conn()
        self.assertEqual(nr.record_restraint("ctx", "said", "why", channel="digest", detail={"a": 1}, conn=c), 42)
        self.assertEqual(json.loads(cur.execute.call_args[0][1][4]), {"a": 1})
        c.close.assert_not_called()

    def test_surfaced_candidates_and_empty_text_skipped(self):
        items = {"candidates": [{"text": "Weather alert for Burbank tonight"}, {"text": "  "},
                                {"text": "Unrelated thing nobody saw"}],
                 "digest": "Heads up: weather alert for burbank tonight is serious"}
        c, cur = _conn(fetchall=[[_run(items)], []])
        with patch.object(nr, "_conn", return_value=c):
            out = nr.harvest_proactive_drops(dry=True)
        self.assertEqual(out, {"runs_scanned": 1, "candidates_considered": 3, "restraints_recorded": 1})

    def test_already_harvested_run_skipped(self):
        c, cur = _conn(fetchall=[[_run({"candidates": [{"text": "x"}]}, log_id=9)], [("9",), (None,)]])
        with patch.object(nr, "_conn", return_value=c):
            out = nr.harvest_proactive_drops(dry=True)
        self.assertEqual(out["candidates_considered"], 0)


class TestIntegration(unittest.TestCase):
    def test_harvest_ledgers_with_digest_id_and_reason(self):
        items = json.dumps({"candidates": [{"text": "dropped idea", "kind": "news", "ref": "r1"}], "digest": ""})
        c, cur = _conn(fetchall=[[_run(items, posted=False, log_id=11)], []])
        with patch.object(nr, "_conn", return_value=c), patch.object(nr, "record_restraint", return_value=1) as rr:
            out = nr.harvest_proactive_drops()
        kw = rr.call_args.kwargs
        self.assertEqual(kw["detail"]["digest_log_id"], "11")
        self.assertIn("posted NOTHING", kw["reason"])
        self.assertEqual(kw["would_have_said"], "[news] dropped idea")
        self.assertIs(kw["conn"], c)
        self.assertEqual(out["restraints_recorded"], 1)

    def test_tables(self):
        self.assertIn("INSERT INTO restraint_ledger", SRC)
        self.assertIn("FROM proactive_digest_log", SRC)
        self.assertIn("dbname=nova_ops", nr.OPS_DSN)


class TestFunctional(unittest.TestCase):
    def test_main_summary_prints_ledger(self):
        c, cur = _conn(fetchone=[(3, 2)])
        cur.fetchall.side_effect = [[("digest", 2), ("chat", 1)], [(TS, "chat", "said", "why")]]
        with patch.object(nr, "_conn", return_value=c), patch.object(sys, "argv", ["x"]), \
             patch("builtins.print") as p:
            self.assertEqual(nr.main(), 0)
        out = "\n".join(str(a[0][0]) for a in p.call_args_list)
        self.assertIn("3 row(s) across 2 channel(s)", out)
        self.assertIn("would_have_said='said'", out)

    def test_main_harvest_dry_run_never_inserts(self):
        with patch.object(nr, "harvest_proactive_drops", return_value={"x": 1}) as h, \
             patch.object(nr, "_summary"), patch.object(sys, "argv", ["x", "--harvest", "--dry-run"]), \
             patch("builtins.print") as p:
            self.assertEqual(nr.main(), 0)
        h.assert_called_once_with(dry=True)
        self.assertIn("(DRY)", p.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_restraint"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
