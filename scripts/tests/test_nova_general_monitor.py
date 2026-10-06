#!/usr/bin/env python3
"""Tests for nova_general_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import runpy
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_general_monitor.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_general_monitor_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gm = _load()


def _cp(out=""):
    return mock.Mock(returncode=0, stdout=out, stderr="")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_content_passed_as_argv_not_shell(self):
        evil = "hello; echo pwned $(whoami) `id`"
        with mock.patch.object(gm.subprocess, "run", return_value=_cp("ok")) as run:
            gm.ingest_to_memory(evil)
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "bash")
        self.assertEqual(argv[2], evil)                # one argv element, never interpolated
        self.assertNotIn("shell", run.call_args[1])


class TestPerformance(unittest.TestCase):
    def test_ingest_guard_10k_short_inputs(self):
        with mock.patch.object(gm.subprocess, "run") as run:
            t0 = time.perf_counter()
            for i in range(10_000):
                gm.ingest_to_memory(" " * (i % 9))
            self.assertLess(time.perf_counter() - t0, 1.0)
        run.assert_not_called()


class TestRetry(unittest.TestCase):
    def test_read_is_single_attempt_and_empty_is_safe(self):
        # RETRY GAP: get_general_channel_messages — one subprocess attempt, no retry; empty stdout
        # flows to the "No new messages" branch rather than raising.
        with mock.patch.object(gm.subprocess, "run", return_value=_cp("")) as run:
            self.assertEqual(gm.get_general_channel_messages(), "")
        self.assertEqual(run.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_ingest_rejects_empty_and_tiny(self):
        with mock.patch.object(gm.subprocess, "run") as run:
            self.assertIsNone(gm.ingest_to_memory(None))
            self.assertIsNone(gm.ingest_to_memory(""))
            self.assertIsNone(gm.ingest_to_memory("   short  "))
        run.assert_not_called()

    def test_ingest_returns_stdout(self):
        with mock.patch.object(gm.subprocess, "run", return_value=_cp("stored 1")):
            self.assertEqual(gm.ingest_to_memory("a long enough message"), "stored 1")


class TestIntegration(unittest.TestCase):
    def test_uses_shared_remember_script_with_slack_source(self):
        with mock.patch.object(gm.subprocess, "run", return_value=_cp()) as run:
            gm.ingest_to_memory("a long enough message")
        argv = run.call_args[0][0]
        self.assertTrue(argv[1].endswith(".openclaw/scripts/nova_remember.sh"))
        self.assertEqual(argv[3], "slack")
        self.assertEqual(json.loads(argv[4])["topic"], "general_channel")

    def test_reads_general_channel(self):
        with mock.patch.object(gm.subprocess, "run", return_value=_cp("x")) as run:
            gm.get_general_channel_messages()
        self.assertIn("C049EPC32", run.call_args[0][0][2])


class TestFunctional(unittest.TestCase):
    def _run_main(self, outputs):
        with mock.patch("subprocess.run", side_effect=[_cp(o) for o in outputs]) as run, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            runpy.run_path(str(PATH), run_name="__main__")
        return run, out.getvalue()

    def test_main_ingests_messages(self):
        run, out = self._run_main(["msg one from #general", "remembered"])
        self.assertEqual(run.call_count, 2)
        self.assertIn("Ingested: remembered", out)

    def test_main_no_messages(self):
        run, out = self._run_main([""])
        self.assertEqual(run.call_count, 1)
        self.assertIn("No new messages", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_general_monitor"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
