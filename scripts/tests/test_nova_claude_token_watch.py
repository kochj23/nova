#!/usr/bin/env python3
"""Tests for nova_claude_token_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
The Keychain read (subprocess.run `security`), the `claude -p` probe, notify and psycopg2 are mocked —
no real Keychain item is ever read and no page is ever sent."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_claude_token_watch.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_claude_token_watch_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tw = _load()
import nova_claude_code  # noqa: E402
import nova_notify  # noqa: E402
import psycopg2  # noqa: E402


def _kc(payload, rc=0):
    out = json.dumps(payload) if isinstance(payload, dict) else payload
    return SimpleNamespace(returncode=rc, stdout=out, stderr="")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_token(self):
        self.assertIsNone(re.search(r"sk-ant-[A-Za-z0-9_-]{10,}", SRC))
        self.assertIsNone(re.search(r"(token|secret)\s*=\s*['\"][A-Za-z0-9+/_-]{16,}['\"]", SRC, re.I))

    def test_credential_comes_from_keychain(self):
        with patch.object(tw.subprocess, "run", return_value=_kc({"claudeAiOauth": {"expiresAt": 1}})) as run:
            tw.read_token()
        self.assertEqual(run.call_args[0][0][:2], ["security", "find-generic-password"])
        self.assertIn(tw.KEYCHAIN_SERVICE, run.call_args[0][0])

    def test_probe_runs_with_blank_home(self):
        r = SimpleNamespace(stdout="OK", returncode=0)
        with patch.object(nova_claude_code, "claude_oauth_token", return_value="tok"), \
             patch("subprocess.run", return_value=r) as run:
            self.assertTrue(tw._longlived_ok())
        env = run.call_args.kwargs["env"]
        self.assertNotEqual(env["HOME"], str(Path.home()))
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok")


class TestPerformance(unittest.TestCase):
    def test_record_10k_calls_fast_with_pg_down(self):
        t0 = time.perf_counter()
        with patch.object(psycopg2, "connect", side_effect=psycopg2.OperationalError("x")):
            for i in range(10_000):
                tw._record(i / 100)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_keychain_failures_fail_open(self):
        # RETRY GAP: read_token() — one `security` call; any failure -> None
        with patch.object(tw.subprocess, "run", side_effect=subprocess.TimeoutExpired("security", 8)) as run:
            self.assertIsNone(tw.read_token())
        self.assertEqual(run.call_count, 1)
        with patch.object(tw.subprocess, "run", return_value=_kc("", rc=44)):
            self.assertIsNone(tw.read_token())
        with patch.object(tw.subprocess, "run", return_value=_kc("not json")):
            self.assertIsNone(tw.read_token())

    def test_notify_and_record_never_raise(self):
        # RETRY GAP: _notify()/_record() — single attempt, exceptions swallowed
        with patch.object(nova_notify, "notify", side_effect=RuntimeError("slack")) as n:
            tw._notify("t", "b", "warning")
        self.assertEqual(n.call_count, 1)
        with patch.object(psycopg2, "connect", side_effect=RuntimeError("pg")):
            self.assertIsNone(tw._record(1.0))


class TestUnit(unittest.TestCase):
    def test_longlived_missing_token_is_false_without_probe(self):
        with patch.object(nova_claude_code, "claude_oauth_token", return_value=None), \
             patch("subprocess.run") as run:
            self.assertFalse(tw._longlived_ok())
        run.assert_not_called()

    def test_longlived_probe_not_ok(self):
        with patch.object(nova_claude_code, "claude_oauth_token", return_value="t"), \
             patch("subprocess.run", return_value=SimpleNamespace(stdout="Invalid API key", returncode=1)):
            self.assertFalse(tw._longlived_ok())

    def test_record_status_and_latency(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        with patch.object(psycopg2, "connect", return_value=conn):
            tw._record(-3.0)
        params = cur.execute.call_args[0][1]
        self.assertEqual(params[:2], ("down", 0))
        self.assertIn("-3.0h", params[2])


class TestIntegration(unittest.TestCase):
    def test_notify_uses_dedup_key_per_level(self):
        with patch.object(nova_notify, "notify") as n:
            tw._notify("t", "b", "critical")
        self.assertEqual(n.call_args.kwargs["dedup_key"], "claude-token:critical")
        self.assertEqual(n.call_args.kwargs["source"], "nova_claude_token_watch")

    def test_health_row_goes_to_health_checks(self):
        self.assertIn("INSERT INTO health_checks", SRC)
        self.assertIn("%s,%s,%s", SRC)
        self.assertIn("dbname=nova_ops", tw.DSN)


class TestFunctional(unittest.TestCase):
    def test_healthy_longlived_records_and_never_pages(self):
        with patch.object(tw, "_longlived_ok", return_value=True), patch.object(tw, "_record") as rec, \
             patch.object(tw, "_notify") as n, patch("builtins.print"):
            self.assertEqual(tw.main(), 0)
        n.assert_not_called()
        self.assertEqual(rec.call_args[0][0], 24 * 365)

    def test_broken_longlived_and_expired_file_cred_pages_critical(self):
        past = int((time.time() - 3600) * 1000)
        with patch.object(tw, "_longlived_ok", return_value=False), \
             patch.object(tw, "read_token", return_value={"expiresAt": past}), \
             patch.object(tw, "_record") as rec, patch.object(tw, "_notify") as n, patch("builtins.print"):
            self.assertEqual(tw.main(), 0)
        self.assertEqual([c[0][2] for c in n.call_args_list], ["warning", "critical"])
        self.assertLess(rec.call_args[0][0], 0)

    def test_logged_out_entirely(self):
        with patch.object(tw, "_longlived_ok", return_value=False), patch.object(tw, "read_token", return_value=None), \
             patch.object(tw, "_record") as rec, patch.object(tw, "_notify") as n, patch("builtins.print"):
            self.assertEqual(tw.main(), 0)
        rec.assert_not_called()
        self.assertEqual(n.call_args[0][2], "critical")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_claude_token_watch"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
