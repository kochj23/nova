#!/usr/bin/env python3
"""Tests for nova_fact.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_fact.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nf = _load("nova_fact_under_test", SCRIPT)


class _Cur:
    def __init__(self, rows=(), rowcount=0):
        self.rows, self.rowcount, self.sql, self.params = list(rows), rowcount, [], []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split())); self.params.append(params)

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cur):
        self.cur, self.exited = cur, False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.exited = True
        return False

    def cursor(self):
        return self.cur


def _with(cur, fn, *argv):
    out = io.StringIO()
    with patch.object(nf.psycopg2, "connect", return_value=_Conn(cur)) as pg, \
         patch.object(sys, "argv", ["nova_fact.py", *argv]), redirect_stdout(out):
        rc = fn()
    return pg, out.getvalue(), rc


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", nf.DSN)

    def test_sql_is_parameterized_and_scoped_to_ground_truth(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"nova.ground_truth"})

    def test_injection_in_key_travels_as_a_parameter(self):
        evil = "k'; DROP TABLE nova.ground_truth; --"
        cur = _Cur()
        _with(cur, lambda: nf.add(evil, "fact"))
        self.assertNotIn("DROP", cur.sql[0]); self.assertEqual(cur.params[0], (evil, "fact", "general"))
        cur = _Cur(rowcount=0)
        _with(cur, lambda: nf.remove(evil))
        self.assertEqual(cur.params[0], (evil,))


class TestPerformance(unittest.TestCase):
    def test_listing_10k_facts_is_fast(self):
        rows = [(f"key{i}", "identity", f"fact {i}", datetime(2026, 10, 5)) for i in range(10_000)]
        t0 = time.perf_counter()
        _, out, _ = _with(_Cur(rows), nf.list_facts)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(out.count("\n"), 10_000)


class TestRetry(unittest.TestCase):
    def test_pg_failure_surfaces_loudly_and_never_claims_success(self):
        # RETRY GAP: add()/list_facts()/remove() — psycopg2.connect is one attempt; the CLI exits non-zero
        for fn in (lambda: nf.add("k", "f"), nf.list_facts, lambda: nf.remove("k")):
            out = io.StringIO()
            with patch.object(nf.psycopg2, "connect", side_effect=OSError("pg down")), redirect_stdout(out):
                with self.assertRaises(OSError):
                    fn()
            self.assertEqual(out.getvalue(), "")


class TestUnit(unittest.TestCase):
    def test_usage_paths_exit_with_help(self):
        for argv in ([], ["bogus"]):
            with patch.object(sys, "argv", ["nova_fact.py", *argv]), self.assertRaises(SystemExit) as cm:
                nf.main()
            self.assertEqual(cm.exception.code, nf.__doc__)
        with patch.object(sys, "argv", ["nova_fact.py", "add", "k"]), self.assertRaises(SystemExit) as cm:
            nf.main()
        self.assertIn("usage: nova_fact.py add", cm.exception.code)
        with patch.object(sys, "argv", ["nova_fact.py", "remove"]), self.assertRaises(SystemExit) as cm:
            nf.main()
        self.assertIn("usage: nova_fact.py remove", cm.exception.code)

    def test_category_flag_is_position_independent_and_fact_words_are_joined(self):
        cur = _Cur()
        _with(cur, nf.main, "add", "--category=location", "rack-pi", "lives", "in", "the", "garage")
        self.assertEqual(cur.params[0], ("rack-pi", "lives in the garage", "location"))
        cur = _Cur()
        _with(cur, nf.main, "add", "k", "one", "two", "--category=identity")
        self.assertEqual(cur.params[0], ("k", "one two", "identity"))

    def test_remove_reports_not_found_when_nothing_matched(self):
        _, out, _ = _with(_Cur(rowcount=0), lambda: nf.remove("ghost"))
        self.assertEqual(out.strip(), "not found: ghost")
        _, out, _ = _with(_Cur(rowcount=1), lambda: nf.remove("ghost"))
        self.assertEqual(out.strip(), "removed: ghost")

    def test_empty_list(self):
        _, out, _ = _with(_Cur([]), nf.list_facts)
        self.assertEqual(out.strip(), "(no facts stored)")


class TestIntegration(unittest.TestCase):
    def test_upsert_keeps_the_key_unique_and_bumps_updated_at(self):
        cur = _Cur()
        _with(cur, lambda: nf.add("k", "f", "retirement"))
        self.assertIn("ON CONFLICT (key) DO UPDATE SET fact = EXCLUDED.fact, category = EXCLUDED.category, updated_at = now()", cur.sql[0])
        self.assertIn("dbname=nova_ops", nf.DSN)

    def test_list_is_ordered_for_the_voice_prompt_and_rendered_per_line(self):
        cur = _Cur([("rack-pi", "identity", "192.168.1.9 is now the Rack-Pi", datetime(2026, 10, 5, 9, 30))])
        _, out, _ = _with(cur, nf.list_facts)
        self.assertIn("ORDER BY category, key", cur.sql[0])
        self.assertEqual(out.strip(), "[identity] rack-pi — 192.168.1.9 is now the Rack-Pi  (2026-10-05)")

    def test_connection_context_is_closed_after_each_command(self):
        cur = _Cur()
        pg, _, _ = _with(cur, lambda: nf.add("k", "f"))
        self.assertTrue(pg.return_value.exited)


class TestFunctional(unittest.TestCase):
    def test_golden_add_then_list_then_remove(self):
        cur = _Cur()
        _, out, _ = _with(cur, nf.main, "add", "unas", "UNAS Pro 8 lives in the rack", "--category=location")
        self.assertEqual(out.strip(), "saved: unas")
        self.assertEqual(cur.params[0], ("unas", "UNAS Pro 8 lives in the rack", "location"))
        cur = _Cur([("unas", "location", "UNAS Pro 8 lives in the rack", datetime(2026, 1, 2))])
        _, out, _ = _with(cur, nf.main, "list")
        self.assertIn("[location] unas — UNAS Pro 8 lives in the rack  (2026-01-02)", out)
        cur = _Cur(rowcount=1)
        _, out, _ = _with(cur, nf.main, "remove", "unas")
        self.assertEqual(out.strip(), "removed: unas")
        self.assertEqual(cur.sql[0], "DELETE FROM nova.ground_truth WHERE key = %s")

    def test_error_path_unknown_command_never_touches_pg(self):
        with patch.object(nf.psycopg2, "connect") as pg, patch.object(sys, "argv", ["nova_fact.py", "frobnicate"]):
            with self.assertRaises(SystemExit):
                nf.main()
        pg.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_no_args_prints_usage_and_exits_nonzero_without_pg(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("nova_fact.py add <key> <fact>", r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_fact"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
