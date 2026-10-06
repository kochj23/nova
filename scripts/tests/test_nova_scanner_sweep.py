#!/usr/bin/env python3
"""Tests for nova_scanner_sweep.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). ssh and the Slack/Discord post are mocked; the SDR host is never
contacted. Written by Jordan Koch (via Claude)."""
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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_scanner_sweep.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("scannersweep", SCRIPTS / "nova_scanner_sweep.py")
    mod = importlib.util.module_from_spec(spec)
    with patch.object(sys, "argv", ["nova_scanner_sweep.py"]):   # DUR is read from argv at import
        spec.loader.exec_module(mod)
    return mod


sw = _load()


def _run(log_text, dur="1800"):
    calls = []

    def run(argv, **k):
        calls.append((argv, k))
        return SimpleNamespace(stdout=log_text if "cat /tmp/harvest.log" in argv[-1] else "", returncode=0)

    post = MagicMock()
    with patch.object(sw, "DUR", dur), patch.object(sw.subprocess, "run", side_effect=run), \
         patch.object(sw.nova_config, "post_both", post), redirect_stdout(io.StringIO()) as out:
        sw.main()
    return calls, post, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('"BatchMode=yes"', SRC)                  # key auth only

    def test_hostile_duration_never_reaches_ssh(self):
        with patch.object(sw, "DUR", "1800; rm -rf ~"), patch.object(sw.subprocess, "run") as run, \
             patch.object(sw.nova_config, "post_both") as post:
            with self.assertRaises(ValueError):
                sw.main()
        run.assert_not_called()                                 # int(DUR) rejects it before any ssh
        post.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_10k_line_log_capped_message(self):
        log = "\n".join(f"470.1 MHz: unit {i} copy" for i in range(10_000))
        t0 = time.perf_counter()
        _, post, _ = _run(log)
        self.assertLess(time.perf_counter() - t0, 1.0)
        msg = post.call_args[0][0]
        self.assertIn("10000 analog transmissions", msg)
        self.assertLess(len(msg), 3200)                         # 30 lines / 2800 chars cap


class TestRetry(unittest.TestCase):
    def test_harvester_timeout_aborts_without_post(self):
        # RETRY GAP: sh()/ssh — one attempt per sweep (launchd reruns morning + evening); a timeout raises
        # before anything is posted, so no misleading "quiet" report goes out.
        calls = []

        def boom(argv, **k):
            calls.append(argv); raise subprocess.TimeoutExpired("ssh", k["timeout"])

        with patch.object(sw, "DUR", "60"), patch.object(sw.subprocess, "run", side_effect=boom), \
             patch.object(sw.nova_config, "post_both") as post:
            with self.assertRaises(subprocess.TimeoutExpired):
                sw.main()
        self.assertEqual(len(calls), 1)
        post.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_header_lines_and_blanks_filtered(self):
        _, post, out = _run("=== sweep start ===\n\n  \n460.0 MHz: engine 3 en route\n=== end ===\n")
        self.assertIn("1 analog transmissions", post.call_args[0][0])
        self.assertIn("posted 1 hits", out)

    def test_ssh_argv_and_timeout(self):
        calls, _, _ = _run("", dur="120")
        argv, kw = calls[0]
        self.assertEqual(argv[:4], ["ssh", "-o", "BatchMode=yes", sw.SDR_HOST])
        self.assertTrue(argv[4].endswith("nova_scanner_harvest.py 120"))
        self.assertEqual(kw["timeout"], 420)


class TestIntegration(unittest.TestCase):
    def test_posts_via_shared_helper_to_feed(self):
        import nova_config
        _, post, _ = _run("x")
        self.assertEqual(post.call_args[1]["slack_channel"], nova_config.SLACK_FEED)
        self.assertIs(sw.nova_config, nova_config)


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_hits(self):
        calls, post, _ = _run("455.1 MHz: copy that\n455.2 MHz: 10-4\n")
        self.assertEqual(len(calls), 2)
        msg = post.call_args[0][0]
        self.assertIn("SDR analog sweep", msg)
        self.assertIn("10-4", msg)

    def test_quiet_window_posts_quiet_note(self):
        _, post, out = _run("")
        self.assertIn("No analog voice this pass", post.call_args[0][0])
        self.assertIn("posted 0 hits", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # running the script ssh's to the SDR host for 30 minutes, so the smoke is an import only
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_scanner_sweep"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
