#!/usr/bin/env python3
"""7-category tests for nova_value_check_eval.py (labelled agreement eval for value_check).
Security, Performance, Retry, Unit, Integration, Functional, Frame. PG and value_check are
mocked; no model or database is touched. Written by Jordan Koch (via Claude)."""
import importlib.util
import inspect
import io
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_value_check_eval.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("vce7", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ev = _load()


class _Cur:
    def __init__(self, rows):
        self.rows, self.sql, self.params = rows, [], []

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params)

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cur):
        self.cur, self.closed = cur, False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _rows(ids=None):
    ids = ids or sorted(ev.LABELS)
    return [(i, "growth", f"action {i}", f"why {i}") for i in ids]


def _run(argv, verdict_fn, rows=None, connect=None):
    """Run main() with PG and nova_values.value_check mocked. verdict_fn(action, ctx|None) -> dict."""
    cur = _Cur(rows if rows is not None else _rows())
    conn = _Conn(cur)
    calls = []

    def value_check(action, *args):
        calls.append((action,) + args)
        return verdict_fn(action, args[0] if args else None)
    fake_nv = types.SimpleNamespace(value_check=value_check)
    out = io.StringIO()
    with mock.patch.object(sys, "argv", ["nova_value_check_eval.py"] + argv), \
            mock.patch.dict(sys.modules, {"nova_values": fake_nv}), \
            mock.patch.object(ev.psycopg2, "connect", connect or mock.Mock(return_value=conn)), \
            mock.patch.object(ev.time, "sleep"), redirect_stdout(out), redirect_stderr(io.StringIO()):
        rc = ev.main()
    return rc, out.getvalue(), calls, cur, conn


def _oracle(action, _ctx):
    pid = int(action.split()[-1])
    return {"allowed": ev.LABELS[pid], "reasoning": "r"}


# ── Security ─────────────────────────────────────────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_read_only_no_writes(self):
        self.assertIsNone(re.search(r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE)\b", SRC))

    def test_query_is_parameterized(self):
        _, _, _, cur, conn = _run([], _oracle)
        self.assertEqual(len(cur.sql), 1)
        self.assertIn("ANY(%s)", cur.sql[0])
        self.assertEqual(sorted(cur.params[0][0]), sorted(ev.LABELS))
        self.assertTrue(conn.closed)

    def test_no_secrets_or_user_paths(self):
        self.assertNotRegex(SRC, r"password\s*=|/Users/\w+/")
        self.assertNotIn("password", ev.OPS_DSN)

    def test_untrusted_rationale_is_passed_as_context_not_executed(self):
        evil = [(94, "growth", "action 94", "'; DROP TABLE values; -- and __import__('os').system('x')")]
        _, _, calls, _, _ = _run([], _oracle, rows=evil)
        self.assertIn("DROP TABLE", calls[0][1])          # just text handed to the gate


# ── Performance ──────────────────────────────────────────────────────────────
class TestPerformance(unittest.TestCase):
    def test_one_query_and_exactly_runs_x_cases_gate_calls(self):
        t = time.perf_counter()
        _, _, calls, cur, _ = _run(["--runs", "3"], _oracle)
        self.assertEqual(len(cur.sql), 1)                 # no N+1
        self.assertEqual(len(calls), 3 * len(ev.LABELS))  # bounded
        self.assertLess(time.perf_counter() - t, 1.0)


# ── Retry ────────────────────────────────────────────────────────────────────
class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff_then_succeeds(self):
        conn = _Conn(_Cur([]))
        connect = mock.Mock(side_effect=[OSError("blip"), OSError("blip"), conn])
        sleep = mock.Mock()
        with mock.patch.object(ev.psycopg2, "connect", connect), mock.patch.object(ev.time, "sleep", sleep), \
                redirect_stderr(io.StringIO()):
            self.assertIs(ev.connect_ops(), conn)
        self.assertEqual(connect.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 2.0])

    def test_connect_raises_loudly_after_last_attempt(self):
        err = io.StringIO()
        with mock.patch.object(ev.psycopg2, "connect", side_effect=OSError("pg down")), \
                mock.patch.object(ev.time, "sleep"), redirect_stderr(err):
            with self.assertRaises(OSError):
                ev.connect_ops()
        self.assertIn("attempt 3/3", err.getvalue())

    def test_main_uses_the_retrying_connect(self):
        conn = _Conn(_Cur(_rows([94])))
        connect = mock.Mock(side_effect=[OSError("blip"), conn])
        rc, out, _, _, _ = _run([], _oracle, connect=connect)
        self.assertEqual(rc, 0)
        self.assertEqual(connect.call_count, 2)
        self.assertIn("agreement 1/1", out)


# ── Unit ─────────────────────────────────────────────────────────────────────
class TestUnit(unittest.TestCase):
    def test_labels_shape(self):
        self.assertEqual(len(ev.LABELS), 25)
        self.assertEqual(sum(ev.LABELS.values()), 14)
        self.assertTrue(all(isinstance(k, int) and isinstance(v, bool) for k, v in ev.LABELS.items()))

    def test_known_declines_are_labelled_deny(self):
        for pid in (110, 136, 139, 144, 147):
            self.assertFalse(ev.LABELS[pid], pid)

    def test_context_for(self):
        self.assertEqual(ev.context_for("growth", "why"), "origin: growth\nNova's stated rationale: why")
        self.assertIn("(none)", ev.context_for("o", None))
        self.assertIn("(none)", ev.context_for("o", ""))

    def test_majority_vote_tie_counts_as_deny(self):
        votes = iter([True, False])
        _, out, _, _, _ = _run(["--runs", "2"], lambda a, c: {"allowed": next(votes)}, rows=_rows([94]))
        self.assertIn("got=deny", out)


# ── Integration ──────────────────────────────────────────────────────────────
class TestIntegration(unittest.TestCase):
    def test_context_flows_from_pg_row_into_value_check(self):
        _, _, calls, _, _ = _run([], _oracle, rows=_rows([110]))
        self.assertEqual(calls[0], ("action 110", "origin: growth\nNova's stated rationale: why 110"))

    def test_no_context_judges_the_bare_action(self):
        _, _, calls, _, _ = _run(["--no-context"], _oracle, rows=_rows([110]))
        self.assertEqual(calls[0], ("action 110",))

    def test_typeerror_fallback_for_an_old_one_arg_value_check(self):
        cur = _Cur(_rows([94]))
        seen = []

        def old_value_check(action):
            seen.append(action); return {"allowed": True}
        with mock.patch.object(sys, "argv", ["x"]), \
                mock.patch.dict(sys.modules, {"nova_values": types.SimpleNamespace(value_check=old_value_check)}), \
                mock.patch.object(ev.psycopg2, "connect", return_value=_Conn(cur)), redirect_stdout(io.StringIO()):
            self.assertEqual(ev.main(), 0)
        self.assertEqual(seen, ["action 94"])

    def test_real_value_check_signature_is_compatible(self):
        import nova_values
        self.assertEqual(len(inspect.signature(nova_values.value_check).parameters), 2)


# ── Functional ───────────────────────────────────────────────────────────────
class TestFunctional(unittest.TestCase):
    def test_golden_path_perfect_agreement(self):
        rc, out, _, _, _ = _run([], _oracle)
        self.assertEqual(rc, 0)
        self.assertIn("agreement 25/25 = 100%", out)
        self.assertIn("allow-cases allowed 14/14, deny-cases denied 11/11", out)
        self.assertEqual(out.count("OK "), 25)

    def test_always_deny_gate_scores_only_the_declines(self):
        _, out, _, _, _ = _run([], lambda a, c: {"allowed": False, "reasoning": "nope"})
        self.assertIn("agreement 11/25", out)
        self.assertEqual(out.count("XX "), 14)

    def test_missing_allowed_key_counts_as_deny(self):
        _, out, _, _, _ = _run([], lambda a, c: {}, rows=_rows([94]))
        self.assertIn("XX ", out)

    def test_empty_result_set_does_not_divide_by_zero(self):
        rc, out, _, _, _ = _run([], _oracle, rows=[])
        self.assertEqual(rc, 0)
        self.assertIn("agreement 0/0 = 0%", out)


# ── Frame ────────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_help_runs_without_touching_pg(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--runs", r.stdout)

    def test_import_does_not_run_main(self):
        with mock.patch.object(ev.psycopg2, "connect", side_effect=AssertionError("connected on import")):
            _load()

    def test_entrypoints(self):
        self.assertTrue(callable(ev.main) and callable(ev.connect_ops))
        self.assertIn('if __name__ == "__main__"', SRC)


if __name__ == "__main__":
    unittest.main()
