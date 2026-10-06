#!/usr/bin/env python3
"""Tests for ingest_to_vector.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The script is a flat CLI with no functions: every test drives it through runpy with sys.argv set and
requests.post mocked, so nothing ever reaches the memory server."""
import io
import os
import re
import runpy
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "ingest_to_vector.py"
SRC = SCRIPT.read_text()


def _run(argv, post=None):
    """Run the script with argv; returns (stdout, exit_code, post_mock)."""
    post = post or MagicMock(return_value=MagicMock(status_code=200, text="ok"))
    buf, code = io.StringIO(), 0
    with patch.object(sys, "argv", ["ingest_to_vector.py", *argv]), patch("requests.post", post), \
            redirect_stdout(buf):
        try:
            runpy.run_path(str(SCRIPT), run_name="__main__")
        except SystemExit as e:
            code = e.code
    return buf.getvalue(), code, post


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.md = Path(self.td.name) / "my_notes_file.md"
        self.md.write_text("# hello\nbody text")

    def tearDown(self):
        self.td.cleanup()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_no_sql_and_no_shell(self):
        # the script only talks HTTP to the memory server — no SQL, no subprocess, no eval
        self.assertNotRegex(SRC, r"\b(INSERT|DELETE|UPDATE)\b")
        self.assertNotIn("subprocess", SRC)
        self.assertNotIn("eval(", SRC)


class TestPerformance(_Tmp):
    def test_large_file_posts_once_fast(self):
        self.md.write_text("line\n" * 10_000)
        t0 = time.perf_counter()
        out, code, post = _run([str(self.md), "src"])
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(post.call_args.kwargs["json"]["text"]), 50_000)


class TestRetry(_Tmp):
    def test_post_failure_is_one_shot_and_fails_open(self):
        # RETRY GAP: module-level requests.post — one attempt, exception is caught and printed, never raised
        post = MagicMock(side_effect=ConnectionError("down"))
        out, code, _ = _run([str(self.md), "src"], post)
        self.assertEqual(post.call_count, 1)
        self.assertIn("Request failed: down", out)
        self.assertEqual(code, 0)

    def test_non_200_reported_not_raised(self):
        post = MagicMock(return_value=MagicMock(status_code=503, text="busy"))
        out, _, _ = _run([str(self.md), "src"], post)
        self.assertIn("Failed to ingest: 503 busy", out)


class TestUnit(_Tmp):
    def test_title_derived_from_filename(self):
        _, _, post = _run([str(self.md), "docs"])
        self.assertEqual(post.call_args.kwargs["json"]["title"], "my notes file")

    def test_wrong_arg_count_prints_usage(self):
        out, code, post = _run(["only-one"])
        self.assertEqual(code, 1)
        self.assertIn("Usage", out)
        post.assert_not_called()

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            _run([str(Path(self.td.name) / "nope.md"), "src"])


class TestIntegration(_Tmp):
    def test_posts_to_memory_server_ingest_endpoint(self):
        _, _, post = _run([str(self.md), "docs"])
        self.assertTrue(post.call_args.args[0].endswith(":18790/ingest"))
        self.assertEqual(set(post.call_args.kwargs["json"]), {"text", "title", "source"})


class TestFunctional(_Tmp):
    def test_golden_path(self):
        out, code, post = _run([str(self.md), "docs"])
        self.assertEqual(code, 0)
        self.assertIn("Successfully ingested", out)
        body = post.call_args.kwargs["json"]
        self.assertEqual((body["text"], body["source"]), ("# hello\nbody text", "docs"))


class TestFrame(unittest.TestCase):
    def test_no_args_exits_with_usage_and_no_network(self):
        # the script has no --help; with no args it prints usage and exits 1 before any HTTP call
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage: ingest_to_vector.py", r.stdout)

    def test_entrypoint_is_argv_gated(self):
        self.assertIn("if len(sys.argv) != 3", SRC)


if __name__ == "__main__":
    unittest.main()
