#!/usr/bin/env python3
"""Tests for nova_send_mail.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
subprocess.run (the herd-mail shell-out) is mocked in every test that calls send_mail()."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_send_mail.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_send_mail_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sm = _load()
sm.log = lambda msg: None
OK = SimpleNamespace(returncode=0, stdout="sent", stderr="")
BAD = SimpleNamespace(returncode=1, stdout="", stderr="smtp refused")


class TestSecurity(unittest.TestCase):
    def test_no_direct_smtp_or_credentials(self):
        self.assertNotIn("import smtplib", SRC)
        self.assertIsNone(re.search(r"(password|secret|token)\s*=\s*['\"]", SRC, re.I))

    def test_argv_list_not_shell_string(self):
        evil = "x@example.com; rm -rf /"
        with patch.object(sm.subprocess, "run", return_value=OK) as run:
            sm.send_mail(evil, "$(whoami)", "`id`")
        args, kw = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertNotIn("shell", kw)
        self.assertIn(evil, args[0])          # passed as one argv element, never interpreted


class TestPerformance(unittest.TestCase):
    def test_10k_recipients_build_args_fast(self):
        rcpts = [f"u{i}@example.com" for i in range(10_000)]
        t0 = time.perf_counter()
        with patch.object(sm.subprocess, "run", return_value=OK) as run:
            self.assertTrue(sm.send_mail(rcpts, "s", "b"))
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(run.call_count, 10_000)


class TestRetry(unittest.TestCase):
    def test_failure_is_single_attempt_and_fails_open(self):
        # RETRY GAP: send_mail() — herd-mail is called once per recipient; failures return False, no raise
        with patch.object(sm.subprocess, "run", return_value=BAD) as run:
            self.assertFalse(sm.send_mail("a@example.com", "s", "b"))
        self.assertEqual(run.call_count, 1)

    def test_timeout_exception_is_swallowed(self):
        with patch.object(sm.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 60)):
            self.assertFalse(sm.send_mail("a@example.com", "s", "b"))


class TestUnit(unittest.TestCase):
    def _args(self, **kw):
        with patch.object(sm.subprocess, "run", return_value=OK) as run:
            sm.send_mail("a@example.com", "subj", "body", **kw)
        return run.call_args[0][0]

    def test_minimal_args(self):
        a = self._args()
        self.assertEqual(a[:2], [sm.HERD_MAIL, "send"])
        self.assertIn("--skip-haiku", a)
        for flag in ("--attachment", "--message-id", "--rich"):
            self.assertNotIn(flag, a)

    def test_optional_flags(self):
        a = self._args(image_path=Path("/tmp/x.png"), in_reply_to="<mid@x>", rich=True)
        self.assertEqual(a[a.index("--attachment") + 1], "/tmp/x.png")
        self.assertEqual(a[a.index("--message-id") + 1], "<mid@x>")
        self.assertIn("--rich", a)

    def test_empty_recipient_list_is_vacuous_success(self):
        with patch.object(sm.subprocess, "run") as run:
            self.assertTrue(sm.send_mail([], "s", "b"))
        run.assert_not_called()


class TestIntegration(unittest.TestCase):
    def test_routes_through_herd_mail_wrapper(self):
        self.assertEqual(Path(sm.HERD_MAIL).name, "nova_herd_mail.sh")
        self.assertEqual(Path(sm.HERD_MAIL).parent, SCRIPTS)

    def test_partial_failure_still_tries_everyone(self):
        with patch.object(sm.subprocess, "run", side_effect=[OK, BAD, OK]) as run:
            self.assertFalse(sm.send_mail(["a@x.io", "b@x.io", "c@x.io"], "s", "b"))
        self.assertEqual(run.call_count, 3)


class TestFunctional(unittest.TestCase):
    def test_cli_golden_path(self):
        argv = ["nova_send_mail.py", "a@example.com", "Hi", "Body"]
        with patch.object(sys, "argv", argv), patch.object(subprocess, "run", return_value=OK) as run:
            code = None
            try:
                exec(compile(SRC, str(SCRIPT), "exec"), {"__name__": "__main__", "__file__": str(SCRIPT)})
            except SystemExit as e:
                code = e.code
        self.assertEqual(code, 0)
        self.assertIn("a@example.com", run.call_args[0][0])

    def test_cli_usage_error(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage", r.stdout)


class TestFrame(unittest.TestCase):
    def test_import_never_sends(self):
        r = subprocess.run([sys.executable, "-c", "import nova_send_mail"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
