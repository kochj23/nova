#!/usr/bin/env python3
"""Tests for nova_reclassify_vectors.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The script is a one-shot migration with no __main__ guard: running/importing it IS the migration. Every test
drives it through runpy with `subprocess.run` (its only door to psql) replaced by a recorder, so no SQL ever
reaches nova_memories, and the default (dry) mode is proven never to issue an UPDATE."""
import io
import os
import re
import runpy
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_reclassify_vectors.py"
SRC = SCRIPT.read_text()


class FakePsql:
    """Answers COUNT(*) with `counts.get(source, default)`; records every SQL string."""
    def __init__(self, counts=None, default=0):
        self.counts = counts or {}; self.default = default; self.sql = []

    def __call__(self, cmd, **kw):
        sql = cmd[cmd.index("-c") + 1]
        self.sql.append(sql)
        out = ""
        if sql.startswith("SELECT COUNT(*)"):
            m = re.search(r"source ?= ?'((?:[^']|'')*)'", sql)
            src = m.group(1).replace("''", "'") if m else ""
            out = str(self.counts.get(src, self.default))
        return MagicMock(stdout=out + "\n", returncode=0)


def _run(argv=(), counts=None, default=0):
    fake = FakePsql(counts, default)
    out = io.StringIO()
    with patch.object(subprocess, "run", side_effect=fake), patch.object(sys, "argv", [str(SCRIPT), *argv]), \
         redirect_stdout(out):
        ns = runpy.run_path(str(SCRIPT), run_name="__main__")
    return ns, fake, out.getvalue()


NS, _, _ = _run()                       # functions for unit tests (their __globals__ is the live run namespace)


def _fn(name, dry=True):
    f = NS[name]; f.__globals__["DRY_RUN"] = dry
    return f


def _updates(fake):
    return [s for s in fake.sql if s.lstrip().startswith("UPDATE")]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)

    def test_default_is_dry_run_and_never_updates(self):
        _, fake, out = _run(default=5)
        self.assertEqual(_updates(fake), [])
        self.assertIn("[DRY RUN]", out)
        self.assertIn("Run with --live to apply changes.", out)

    def test_quotes_in_source_names_are_escaped(self):
        # regression: "mail_don't_fight_the_crazy" produced invalid SQL that psql silently counted as 0
        fake = FakePsql({"mail_don't_fight_the_crazy": 7})
        with patch.object(subprocess, "run", side_effect=fake), redirect_stdout(io.StringIO()):
            n = _fn("rename", dry=False)("mail_don't_fight_the_crazy", "email_archive")
        self.assertEqual(n, 7)
        self.assertIn("source = 'mail_don''t_fight_the_crazy'", _updates(fake)[0])


class TestPerformance(unittest.TestCase):
    def test_full_dry_run_is_quick_and_bounded(self):
        t0 = time.perf_counter()
        _, fake, _ = _run(default=1)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertLess(len(fake.sql), 200)          # a fixed, finite plan — no loops over data


class TestRetry(unittest.TestCase):
    def test_psql_failure_counts_as_zero_and_skips(self):
        # RETRY GAP: psql() — one subprocess call per statement; an error (empty stdout) reads as 0 rows,
        # so the rename is skipped rather than half-applied.
        fake = MagicMock(return_value=MagicMock(stdout="", returncode=2))
        with patch.object(subprocess, "run", fake), redirect_stdout(io.StringIO()):
            self.assertEqual(_fn("rename", dry=False)("document", "livejournal"), 0)
        self.assertEqual(fake.call_count, 1)                 # the COUNT only; no UPDATE attempted


class TestUnit(unittest.TestCase):
    def test_q_escapes(self):
        self.assertEqual(NS["_q"]("a'b''c"), "a''b''''c")
        self.assertEqual(NS["_q"]("plain"), "plain")

    def test_rename_zero_rows_does_nothing(self):
        fake = FakePsql()
        with patch.object(subprocess, "run", side_effect=fake), redirect_stdout(io.StringIO()):
            self.assertEqual(_fn("rename", dry=False)("x", "y"), 0)
        self.assertEqual(_updates(fake), [])

    def test_rename_with_condition(self):
        fake = FakePsql({"local_knowledge": 3})
        with patch.object(subprocess, "run", side_effect=fake), redirect_stdout(io.StringIO()):
            _fn("rename", dry=False)("local_knowledge", "documentary", "text ILIKE '%[Documentary:%'")
        self.assertIn("AND (text ILIKE '%[Documentary:%')", _updates(fake)[0])

    def test_psql_targets_nova_memories(self):
        fake = MagicMock(return_value=MagicMock(stdout=" 4 \n"))
        with patch.object(subprocess, "run", fake):
            self.assertEqual(NS["psql"]("SELECT 1"), "4")
        cmd = fake.call_args[0][0]
        self.assertEqual(cmd[cmd.index("-d") + 1], "nova_memories")


class TestIntegration(unittest.TestCase):
    def test_privacy_tagging_only_untagged_rows(self):
        fake = FakePsql({"imessage": 9})
        with patch.object(subprocess, "run", side_effect=fake), redirect_stdout(io.StringIO()):
            self.assertEqual(_fn("fix_privacy", dry=False)("imessage"), 9)
        upd = _updates(fake)[0]
        self.assertIn('"local-only"', upd)
        self.assertIn("metadata->>'privacy' IS NULL", upd)


class TestFunctional(unittest.TestCase):
    def test_live_run_applies_renames_and_privacy(self):
        _, fake, out = _run(argv=["--live"], counts={"document": 2, "imessage": 4, "mail_don't_fight_the_crazy": 1})
        ups = _updates(fake)
        self.assertTrue(any("SET source = 'livejournal'" in u for u in ups))
        self.assertTrue(any("jsonb_set" in u and "'imessage'" in u for u in ups))
        self.assertTrue(any("mail_don''t_fight_the_crazy" in u for u in ups))
        self.assertIn("[LIVE]", out)
        self.assertIn("Total rows affected: 3", out)

    def test_dry_run_reports_counts(self):
        _, _, out = _run(counts={"document": 12})
        self.assertRegex(out, r"DRY RUN:\s+12\s+'document'")


class TestFrame(unittest.TestCase):
    def test_runs_against_a_fake_psql_on_path(self):
        # no --help/--selftest and no __main__ guard: the frame check is a real subprocess dry run with a stub
        # `psql` first on PATH that answers 0 and logs every statement.
        d = Path(tempfile.mkdtemp(prefix="fakepsql_"))
        logf = d / "sql.log"
        exe = d / "psql"
        exe.write_text(f"#!/bin/sh\nfor a; do last=\"$a\"; done\nprintf '%s\\n' \"$last\" >> '{logf}'\necho 0\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "PATH": f"{d}:{os.environ.get('PATH', '')}"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Total rows affected: 0", r.stdout)
        self.assertNotIn("UPDATE", logf.read_text())

    def test_mode_flag_documented(self):
        self.assertIn('DRY_RUN = "--live" not in sys.argv', SRC)


if __name__ == "__main__":
    unittest.main()
