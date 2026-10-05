#!/usr/bin/env python3
"""Tests for nova_attention_budget.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_attention_budget.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ab = _load("abudget", SCRIPT)
SRC = SCRIPT.read_text()
TODAY = date(2026, 10, 5)


class _Cur:
    """Answers keyed by a SQL fragment (first match wins); records every execute."""
    def __init__(self, answers=()):
        self.answers = list(answers); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        hit = next((v for k, v in self.answers if k in sql), None)
        self._last = hit(sql, params) if callable(hit) else hit

    def fetchone(self):
        return self._last[0] if isinstance(self._last, list) else self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else ([] if self._last is None else [self._last])

    def executed(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur): self._cur = cur; self.autocommit = False

    def cursor(self, *a, **k): return self._cur


def _budget_cur(total=20, spent=5, reserve=3, extra=()):
    return _Cur([("SELECT date, total_units", (TODAY, total, spent, reserve)), *extra])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_and_tunables_come_from_env(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertNotIn(".format(", SRC)
        self.assertNotIn("DELETE", SRC)
        self.assertIn('os.environ.get("NOVA_ATTENTION_TOTAL"', SRC)
        self.assertIn('os.environ.get("NOVA_ATTENTION_RESERVE"', SRC)

    def test_spend_guard_lives_in_the_where_clause(self):
        # try_spend's atomicity is the SQL predicate, not a Python read-modify-write
        cur = _budget_cur(extra=[("RETURNING spent_units", None)])
        self.assertFalse(ab.try_spend(cur, 4, TODAY.isoformat()))
        sql, params = cur.executed("RETURNING spent_units")[0]
        self.assertIn("(total_units - reserve_units - spent_units) >= %s", sql)
        self.assertEqual(params, (4, TODAY.isoformat(), 4))

    def test_volition_alternatives_are_json_not_interpolated(self):
        cur = _budget_cur(extra=[("RETURNING id", (9,))])
        alts = [{"topic": "x'); DROP TABLE volition_log; --", "why": "lost"}]
        ab.log_volition(cur, "horology", "preoccupation", alts, 1, 11, "because", "lineage")
        sql, params = cur.executed("INSERT INTO volition_log")[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(json.loads(params[2]), alts)


class TestPerformance(unittest.TestCase):
    def test_cost_lookup_and_ledger_calls_fast(self):
        t0 = time.perf_counter()
        total = sum(ab.cost_of(m) for m in ("preoccupation", "thread", "tangent", "unknown") * 25_000)
        self.assertEqual(total, 25_000 * 8)
        cur = _budget_cur(extra=[("RETURNING spent_units", (6,))])
        for _ in range(2_000):
            ab.spend(cur, 1, TODAY.isoformat())
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(cur.sql), 2_000 * 6)  # schema x3 + insert + select + update: bounded per call


class TestRetry(unittest.TestCase):
    def test_cli_has_no_retry_on_connect_and_lets_the_error_escape(self):
        # RETRY GAP: _cli psycopg2.connect — one attempt; the scheduler sees the failure, nothing half-written
        def boom(*a, **k):
            raise OSError("pg down")
        with patch.object(ab.psycopg2, "connect", boom), patch.object(sys, "argv", ["x", "--reset"]):
            with self.assertRaises(OSError):
                ab._cli()

    def test_reset_is_idempotent_so_a_rerun_is_safe(self):
        # a retried --reset re-issues ON CONFLICT DO NOTHING; the ledger cannot be double-provisioned
        cur = _budget_cur()
        ab.get_or_init_today(cur, TODAY.isoformat()); ab.get_or_init_today(cur, TODAY.isoformat())
        ins = cur.executed("INSERT INTO attention_budget")
        self.assertEqual(len(ins), 2)
        self.assertTrue(all("ON CONFLICT (date) DO NOTHING" in s for s, _ in ins))


class TestUnit(unittest.TestCase):
    def test_cost_of_modes_and_default(self):
        self.assertEqual(ab.cost_of("preoccupation"), 1)
        self.assertEqual(ab.cost_of("thread"), 2)
        self.assertEqual(ab.cost_of("tangent"), 3)
        self.assertEqual(ab.cost_of("nonsense"), 2)
        self.assertEqual(ab.cost_of(None), 2)
        self.assertLess(ab.cost_of("preoccupation"), ab.cost_of("tangent"))  # luxuries foreclosed first

    def test_remaining_is_floored_at_zero(self):
        self.assertEqual(ab.remaining(_budget_cur(20, 5, 3), TODAY.isoformat()), 12)
        self.assertEqual(ab.remaining(_budget_cur(20, 25, 3), TODAY.isoformat()), 0)
        self.assertEqual(ab.remaining(_budget_cur(20, 17, 3), TODAY.isoformat()), 0)  # reserve is never spendable

    def test_try_spend_and_spend_report_the_row(self):
        self.assertTrue(ab.try_spend(_budget_cur(extra=[("RETURNING spent_units", (9,))]), 4, TODAY.isoformat()))
        self.assertFalse(ab.try_spend(_budget_cur(extra=[("RETURNING spent_units", None)]), 4, TODAY.isoformat()))
        self.assertTrue(ab.spend(_budget_cur(extra=[("RETURNING spent_units", (99,))]), 50, TODAY.isoformat()))

    def test_default_day_is_today(self):
        cur = _budget_cur()
        ab.get_or_init_today(cur)
        self.assertEqual(cur.executed("INSERT INTO attention_budget")[0][1][0], date.today().isoformat())

    def test_log_volition_returns_the_new_id(self):
        cur = _budget_cur(extra=[("RETURNING id", (77,))])
        self.assertEqual(ab.log_volition(cur, "x", "thread", [], 2, 10, "d", None), 77)


class TestIntegration(unittest.TestCase):
    def test_schema_owns_both_tables_and_the_index(self):
        cur = _Cur()
        ab.ensure_schema(cur)
        ddl = "\n".join(s for s, _ in cur.sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS attention_budget", ddl)
        self.assertIn("CREATE TABLE IF NOT EXISTS volition_log", ddl)
        self.assertIn("idx_volition_ts", ddl)
        self.assertIn("alternatives_foreclosed jsonb", ddl)

    def test_init_provisions_with_the_env_tunables_then_reads_back(self):
        cur = _budget_cur(20, 0, 3)
        b = ab.get_or_init_today(cur, TODAY.isoformat())
        ins = cur.executed("INSERT INTO attention_budget")[0]
        self.assertEqual(ins[1], (TODAY.isoformat(), ab.TOTAL_UNITS, ab.RESERVE_UNITS))
        self.assertEqual(b, {"date": TODAY, "total_units": 20, "spent_units": 0, "reserve_units": 3})

    def test_spend_then_log_chain_is_an_honest_ledger(self):
        cur = _budget_cur(20, 16, 3, extra=[("RETURNING spent_units", (19,)), ("RETURNING id", (5,))])
        ab.spend(cur, ab.cost_of("tangent"), TODAY.isoformat())
        rem = ab.remaining(cur, TODAY.isoformat())  # stub still reports 16 spent; real row would be 19
        vid = ab.log_volition(cur, "rail radio", "tangent", [{"topic": "horology", "why": "cheaper but seen"}],
                              ab.cost_of("tangent"), rem, "I wanted the wander", None)
        self.assertEqual(vid, 5)
        _, params = cur.executed("INSERT INTO volition_log")[0]
        self.assertEqual(params[:2], ("rail radio", "tangent"))
        self.assertEqual(params[3], 3)


class TestFunctional(unittest.TestCase):
    def _cli(self, cur, argv):
        buf = io.StringIO()
        with patch.object(ab.psycopg2, "connect", lambda *a, **k: _Conn(cur)), \
                patch.object(sys, "argv", ["nova_attention_budget.py", *argv]), redirect_stdout(buf):
            rc = ab._cli()
        return rc, buf.getvalue()

    def test_reset_provisions_and_reports(self):
        rc, out = self._cli(_budget_cur(20, 0, 3), ["--reset"])
        self.assertEqual(rc, 0)
        self.assertIn("spendable_remaining=17", out)
        self.assertIn("reset complete", out)

    def test_status_lists_recent_volition(self):
        rows = [(datetime(2026, 10, 5, 9, 30), "horology", "preoccupation", 1, 11, "it was mine")]
        rc, out = self._cli(_budget_cur(20, 9, 3, extra=[("FROM volition_log", rows)]), ["--status"])
        self.assertEqual(rc, 0)
        self.assertIn("spendable_remaining=8", out)
        self.assertIn("09:30 -1u  horology (preoccupation) -> 11u left :: it was mine", out)

    def test_status_with_empty_ledger_is_quiet(self):
        rc, out = self._cli(_budget_cur(extra=[("FROM volition_log", [])]), [])
        self.assertEqual(rc, 0)
        self.assertNotIn("recent volition", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--reset", r.stdout)

    def test_import_never_runs_cli(self):
        self.assertIn('if __name__ == "__main__":\n    raise SystemExit(_cli())', SRC)
        self.assertEqual(ab.__name__, "abudget")


if __name__ == "__main__":
    unittest.main()
