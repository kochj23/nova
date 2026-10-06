#!/usr/bin/env python3
"""Tests for nova_school_report.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_school_report.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sr = _load("school_report_under_test", SCRIPT)
# Module-level stub: psql must never actually run. The module attribute is swapped (not the shared
# `subprocess` module), so nothing leaks into other test files.
sr.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=AssertionError("unmocked psql call")))


def _cp(stdout="", rc=0, stderr=""):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def _run_main(answers):
    """Drive main() with a psql stub that answers each call in order; returns (stdout, argv list)."""
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    buf = io.StringIO()
    with patch.object(sr.subprocess, "run", run), redirect_stdout(buf):
        sr.main()
    return buf.getvalue(), calls


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("PGPASSWORD", SRC)
        self.assertNotIn("password", " ".join(sr.DB).lower())      # psql auth comes from ~/.pgpass, not argv

    def test_sql_is_constant_and_passed_as_argv_not_shell(self):
        self.assertNotIn("shell=True", SRC)
        self.assertIsNone(re.search(r'f"""\s*(SELECT|WITH)', SRC))
        self.assertIsNone(re.search(r"\.format\(", SRC))
        self.assertNotIn("%s", sr.COUNTS_SQL + sr.SAMPLE_SQL)      # no interpolation points at all
        calls = []
        with patch.object(sr.subprocess, "run", lambda argv, **kw: calls.append(argv) or _cp("")):
            sr._psql(sr.COUNTS_SQL)
        self.assertEqual(calls[0][:-1], sr.DB)
        self.assertEqual(calls[0][-1], sr.COUNTS_SQL)              # one argv element, never a joined shell string

    def test_report_is_read_only(self):
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM|DROP|TRUNCATE)\b", SRC))


class TestPerformance(unittest.TestCase):
    def test_build_report_fast_on_10k_sources(self):
        counts = [(f"src{i}", str(i % 97)) for i in range(10_000)]
        samples = {f"src{i}": "x" * 140 for i in range(0, 10_000, 2)}
        t0 = time.perf_counter()
        out = sr.build_report(counts, samples)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(out.count("\n"), 10_000)


class TestRetry(unittest.TestCase):
    def test_psql_failure_is_one_shot_and_raises_loudly(self):
        # RETRY GAP: _psql — a non-zero psql exit is tried once and surfaces as RuntimeError (no backoff;
        # main() lets it escape so the cron/gateway caller sees the failure instead of an empty digest)
        run = MagicMock(return_value=_cp("", rc=2, stderr="psql: connection refused\n"))
        with patch.object(sr.subprocess, "run", run):
            with self.assertRaises(RuntimeError) as cm:
                sr._psql(sr.COUNTS_SQL)
        self.assertEqual(str(cm.exception), "psql: connection refused")
        self.assertEqual(run.call_count, 1)

    def test_psql_timeout_is_one_shot(self):
        # RETRY GAP: _psql — subprocess.TimeoutExpired propagates after a single 20 s attempt
        run = MagicMock(side_effect=subprocess.TimeoutExpired("psql", 20))
        with patch.object(sr.subprocess, "run", run):
            with self.assertRaises(subprocess.TimeoutExpired):
                sr._psql(sr.SAMPLE_SQL)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[1]["timeout"], 20)

    def test_empty_stderr_gets_a_default_message(self):
        with patch.object(sr.subprocess, "run", MagicMock(return_value=_cp("", rc=1, stderr="  "))):
            with self.assertRaisesRegex(RuntimeError, "psql failed"):
                sr._psql("SELECT 1")


class TestUnit(unittest.TestCase):
    def test_selftest_runs_clean(self):
        with redirect_stdout(io.StringIO()) as buf:
            sr._selftest()
        self.assertIn("selftest OK", buf.getvalue())

    def test_build_report_empty(self):
        self.assertEqual(sr.build_report([], {}), "No new memories today — Nova hasn't ingested anything yet.")

    def test_build_report_totals_and_samples(self):
        out = sr.build_report([("tv", "10"), ("email", "5")], {"tv": "  padded sample  ", "ghost": "ignored"})
        lines = out.splitlines()
        self.assertEqual(lines[0], "Nova's school day — 15 new memories across 2 vectors:")
        self.assertEqual(lines[1], "- tv (10): padded sample")
        self.assertEqual(lines[2], "- email (5)")
        self.assertNotIn("ghost", out)

    def test_build_report_blank_sample_is_treated_as_missing(self):
        self.assertEqual(sr.build_report([("a", "1")], {"a": "   "}).splitlines()[1], "- a (1)")

    def test_psql_splits_tab_separated_rows_and_skips_blank_lines(self):
        with patch.object(sr.subprocess, "run", MagicMock(return_value=_cp("a\t1\n\nb\t2\n"))):
            self.assertEqual(sr._psql("x"), [["a", "1"], ["b", "2"]])
        with patch.object(sr.subprocess, "run", MagicMock(return_value=_cp("\n"))):
            self.assertEqual(sr._psql("x"), [])


class TestIntegration(unittest.TestCase):
    def test_targets_the_memories_table_in_nova_memories_for_today(self):
        self.assertIn("nova_memories", sr.DB)
        self.assertIn("-tA", sr.DB)
        for sql in (sr.COUNTS_SQL, sr.SAMPLE_SQL):
            self.assertIn("FROM memories", sql)
            self.assertIn("created_at::date = current_date", sql)
        self.assertIn("left(regexp_replace(text", sr.SAMPLE_SQL)          # sample is whitespace-collapsed + capped

    def test_main_chains_psql_into_build_report(self):
        out, calls = _run_main([_cp("television\t401\nemail\t3\n"), _cp("television\tWorld Cup chatter\nemail\n")])
        self.assertEqual([c[0][-1] for c in calls], [sr.COUNTS_SQL, sr.SAMPLE_SQL])
        self.assertIn("404 new memories across 2 vectors", out)
        self.assertIn("- television (401): World Cup chatter", out)
        self.assertIn("- email (3)\n", out + "\n")

    def test_sample_row_without_text_column_is_tolerated(self):
        out, _ = _run_main([_cp("x\t1\n"), _cp("x\n")])
        self.assertIn("- x (1)", out)
        self.assertNotIn("- x (1):", out)


class TestFunctional(unittest.TestCase):
    def test_golden_path_prints_digest(self):
        out, calls = _run_main([_cp("email\t2\n"), _cp("email\tRe: standup moved\n")])
        self.assertEqual(out.strip(), "Nova's school day — 2 new memories across 1 vectors:\n- email (2): Re: standup moved")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c[1]["capture_output"] and c[1]["text"] for c in calls))

    def test_quiet_day_prints_the_empty_message(self):
        out, calls = _run_main([_cp(""), _cp("")])
        self.assertIn("hasn't ingested anything yet", out)

    def test_error_path_psql_failure_escapes_main(self):
        with self.assertRaises(RuntimeError):
            _run_main([_cp("", rc=1, stderr="FATAL: database does not exist")])


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest OK", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_school_report"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
