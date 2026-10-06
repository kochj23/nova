#!/usr/bin/env python3
"""Tests for nova_deep_healthcheck.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Every subprocess/ssh/HTTP/PG/Slack call is mocked; NO real service
is restarted, NO mount or DB is touched. Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hc = _load("nova_deep_healthcheck_t", SCRIPTS / "nova_deep_healthcheck.py")
SRC = (SCRIPTS / "nova_deep_healthcheck.py").read_text()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_plex_token_comes_from_keychain(self):
        with mock.patch.object(hc, "sh", return_value=types.SimpleNamespace(stdout="tok123\n", returncode=0)) as s:
            self.assertEqual(hc.keychain("nova-plex-token"), "tok123")
        self.assertEqual(s.call_args.args[0][0], "security")

    def test_redline_blocks_destructive_fixes(self):
        for desc in ("plex: delete old libraries", "pg: promote standby to primary", "reboot the host",
                     "buy more storage", "exfiltrate the db"):
            ok, detail = hc.safe_fix(desc, lambda: (True, "ran"))
            self.assertFalse(ok)
            self.assertIn("redline-blocked", detail)


class TestPerformance(unittest.TestCase):
    def test_redline_regex_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            hc._REDLINE.search(f"benign fix number {i} restart a service gently")
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_sh_failure_fails_open_with_124(self):
        # RETRY GAP: sh()/subprocess.run — single attempt; timeout/error returns a CompletedProcess rc=124
        with mock.patch.object(hc.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)) as r:
            cp = hc.sh(["ls"])
        self.assertEqual(cp.returncode, 124)
        self.assertEqual(r.call_count, 1)

    def test_get_failure_returns_status_zero(self):
        with mock.patch.object(hc.urllib.request, "urlopen", side_effect=OSError("conn refused")):
            body, st = hc.get("http://x/y")
        self.assertEqual(st, 0)
        self.assertIn("conn refused", body)


class TestUnit(unittest.TestCase):
    def test_result_shape(self):
        r = hc.result("x", True, "fine")
        self.assertEqual(set(r), {"name", "ok", "detail", "fixed", "fix_detail", "needs_human"})
        self.assertTrue(r["ok"])

    def test_safe_fix_dry_run_does_not_execute(self):
        ran = []
        with mock.patch.object(hc, "DRY", True):
            ok, detail = hc.safe_fix("gateway: restart", lambda: ran.append(1) or (True, "x"))
        self.assertFalse(ok)
        self.assertEqual(ran, [])
        self.assertIn("[dry-run] would", detail)

    def test_safe_fix_runs_when_allowed(self):
        with mock.patch.object(hc, "DRY", False):
            ok, detail = hc.safe_fix("gateway: restart", lambda: (True, "restarted"))
        self.assertTrue(ok)
        self.assertEqual(detail, "restarted")

    def test_safe_fix_catches_exceptions(self):
        with mock.patch.object(hc, "DRY", False):
            ok, detail = hc.safe_fix("gateway: restart", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertFalse(ok)
        self.assertIn("fix error", detail)


class TestIntegration(unittest.TestCase):
    def test_checks_registered(self):
        names = {c.__name__ for c in hc.CHECKS}
        for n in ("check_postgres", "check_plex", "check_memory", "check_gateway", "check_inference"):
            self.assertIn(n, names)

    def test_check_plex_functional_pass(self):
        def get(url, timeout=8):
            if "sections?" in url:
                return '<D key="1" size="1"/>', 200
            return 'totalSize="42"', 200
        with mock.patch.object(hc, "keychain", return_value="tok"), mock.patch.object(hc, "get", side_effect=get):
            r = hc.check_plex()
        self.assertTrue(r["ok"])
        self.assertIn("items", r["detail"])

    def test_check_plex_zero_libraries_triggers_guarded_fix(self):
        with mock.patch.object(hc, "keychain", return_value="tok"), \
             mock.patch.object(hc, "get", return_value=("<MediaContainer/>", 200)), \
             mock.patch.object(hc, "DRY", True):
            r = hc.check_plex()
        self.assertFalse(r["ok"])
        self.assertIn("ZERO libraries", r["detail"])
        self.assertIn("[dry-run]", r["fix_detail"])  # fix was gated, nothing restarted


class TestFunctional(unittest.TestCase):
    def test_main_dry_run_reports_without_fixing(self):
        posts = []
        fake_cfg = types.SimpleNamespace(SLACK_ALERTS="C_A", SLACK_NOTIFY="C_N",
                                         post_both=lambda msg, slack_channel=None: posts.append((msg, slack_channel)))
        with mock.patch.object(hc, "CHECKS", [lambda: hc.result("db", True, "ok"),
                                              lambda: hc.result("plex", False, "down", needs_human=True)]), \
             mock.patch.dict(sys.modules, {"nova_config": fake_cfg}), \
             mock.patch.object(hc.psycopg2 if hasattr(hc, "psycopg2") else hc, "connect", create=True,
                               side_effect=RuntimeError("no pg")), \
             mock.patch.object(sys, "argv", ["x", "--dry-run"]), mock.patch.object(hc, "log"):
            rc = hc.main()
        self.assertEqual(rc, 1)                      # one broken => exit 1
        self.assertEqual(posts[0][1], "C_A")         # broken => alerts channel
        self.assertIn("Needs you", posts[0][0])

    def test_main_all_healthy_posts_to_notify(self):
        posts = []
        fake_cfg = types.SimpleNamespace(SLACK_ALERTS="C_A", SLACK_NOTIFY="C_N",
                                         post_both=lambda msg, slack_channel=None: posts.append((msg, slack_channel)))
        with mock.patch.object(hc, "CHECKS", [lambda: hc.result("db", True, "ok")]), \
             mock.patch.dict(sys.modules, {"nova_config": fake_cfg}), \
             mock.patch.object(sys, "argv", ["x"]), mock.patch.object(hc, "log"), \
             mock.patch("psycopg2.connect", side_effect=RuntimeError("no pg")):
            rc = hc.main()
        self.assertEqual(rc, 0)
        self.assertEqual(posts[0][1], "C_N")
        self.assertIn("functional", posts[0][0])

    def test_crashing_check_is_contained(self):
        with mock.patch.object(hc, "CHECKS", [lambda: (_ for _ in ()).throw(RuntimeError("kaboom"))]), \
             mock.patch.dict(sys.modules, {"nova_config": types.SimpleNamespace(
                 SLACK_ALERTS="A", SLACK_NOTIFY="N", post_both=lambda *a, **k: None)}), \
             mock.patch.object(sys, "argv", ["x"]), mock.patch.object(hc, "log"), \
             mock.patch("psycopg2.connect", side_effect=RuntimeError("no pg")):
            rc = hc.main()
        self.assertEqual(rc, 1)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_deep_healthcheck"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("deep-hc", r.stdout)


if __name__ == "__main__":
    unittest.main()
