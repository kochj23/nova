#!/usr/bin/env python3
"""Tests for nova_claude_cred_sync.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_claude_cred_sync.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("ccsync", SCRIPTS / "nova_claude_cred_sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cs = _load()
H = 3600 * 1000


def _blob(now, access_h=6.0, refresh_h=500.0, at="AT-fake", rt="RT-fake"):
    return {"claudeAiOauth": {"accessToken": at, "refreshToken": rt,
                              "expiresAt": now + int(access_h * H), "refreshTokenExpiresAt": now + int(refresh_h * H)}}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_secret_goes_over_stdin_never_argv(self):
        with patch.object(cs.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="OK", stderr="")) as r:
            ok, _ = cs.push("user@host", "SECRET-BLOB")
        self.assertTrue(ok)
        argv = r.call_args[0][0]
        self.assertNotIn("SECRET-BLOB", " ".join(argv))
        self.assertEqual(r.call_args[1]["input"], "SECRET-BLOB")
        self.assertIn("umask 077", argv[2])
        self.assertIn("chmod 600", argv[2])

    def test_main_never_prints_secret(self):
        now = int(time.time() * 1000)
        raw = json.dumps(_blob(now, at="ACCESS-SHOULD-NOT-PRINT"))
        with patch.object(cs, "local_credential", return_value=raw), \
             patch.object(cs, "push", return_value=(True, "")), \
             patch("builtins.print") as pr:
            cs.main()
        self.assertNotIn("ACCESS-SHOULD-NOT-PRINT", " ".join(str(c) for c in pr.call_args_list))

    def test_dead_token_never_pushed(self):
        now = int(time.time() * 1000)
        with patch.object(cs, "local_credential", return_value=json.dumps(_blob(now, access_h=0.1))), \
             patch.object(cs, "push") as push, patch("builtins.print"):
            self.assertEqual(cs.main(), 0)
        push.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_is_syncable_10k_fast(self):
        now = int(time.time() * 1000)
        blobs = [_blob(now, access_h=i % 12) for i in range(10_000)]
        t0 = time.perf_counter()
        n = sum(cs.is_syncable(b, now) for b in blobs)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertGreater(n, 0)


class TestRetry(unittest.TestCase):
    def test_push_fails_open_on_ssh_timeout(self):
        # RETRY GAP: push() — one ssh per host per cycle; the 6-hourly schedule is the retry
        calls = []
        def boom(*a, **k):
            calls.append(1); raise subprocess.TimeoutExpired("ssh", 25)
        with patch.object(cs.subprocess, "run", side_effect=boom):
            ok, err = cs.push("h", "x")
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertIn("timed out", err)

    def test_keychain_failure_returns_none(self):
        with patch.object(cs.subprocess, "run", side_effect=OSError("no security")):
            self.assertIsNone(cs.local_credential())
        with patch.object(cs.subprocess, "run", return_value=SimpleNamespace(returncode=44, stdout="", stderr="")):
            self.assertIsNone(cs.local_credential())


class TestUnit(unittest.TestCase):
    def test_access_remaining(self):
        self.assertEqual(cs.access_remaining_ms({"claudeAiOauth": {"expiresAt": 100}}, 40), 60)
        self.assertEqual(cs.access_remaining_ms({}, 40), -40)

    def test_is_syncable_edges(self):
        now = int(time.time() * 1000)
        self.assertTrue(cs.is_syncable(_blob(now), now))
        self.assertFalse(cs.is_syncable({}, now))
        self.assertFalse(cs.is_syncable({"claudeAiOauth": "x"}, now))
        self.assertFalse(cs.is_syncable(_blob(now, rt=""), now))
        self.assertFalse(cs.is_syncable(_blob(now, refresh_h=-1), now))
        self.assertFalse(cs.is_syncable(_blob(now, access_h=0.4), now))


class TestIntegration(unittest.TestCase):
    def test_keychain_service_and_remote_path(self):
        with patch.object(cs.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="{}\n", stderr="")) as r:
            self.assertEqual(cs.local_credential(), "{}")
        self.assertEqual(r.call_args[0][0][:4], ["security", "find-generic-password", "-s", "Claude Code-credentials"])
        self.assertEqual(cs.REMOTE_PATH, "~/.claude/.credentials.json")


class TestFunctional(unittest.TestCase):
    def test_pushes_to_every_host_and_alerts_failures(self):
        now = int(time.time() * 1000)
        results = iter([(True, "")] * (len(cs.HOSTS) - 1) + [(False, "refused")])
        sent = []
        with patch.object(cs, "local_credential", return_value=json.dumps(_blob(now))), \
             patch.object(cs, "push", side_effect=lambda h, b: next(results)) as push, \
             patch("nova_notify.notify", side_effect=lambda *a, **k: sent.append((a, k))), \
             patch("builtins.print"):
            self.assertEqual(cs.main(), 0)
        self.assertEqual(push.call_count, len(cs.HOSTS))
        self.assertEqual(len(sent), 1)
        self.assertIn("refused", sent[0][0][0])
        self.assertEqual(sent[0][1]["dedup_key"], "claude-cred-sync-fail")

    def test_no_or_bad_credential_skips(self):
        for raw in (None, "not json"):
            with patch.object(cs, "local_credential", return_value=raw), patch.object(cs, "push") as push, \
                 patch("builtins.print"):
                self.assertEqual(cs.main(), 0)
            push.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # main() would read the real Keychain and ssh the fleet, so the smoke is an import only
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_claude_cred_sync"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip() + r.stderr.strip(), "")


if __name__ == "__main__":
    unittest.main()
