#!/usr/bin/env python3
"""Tests for nova_osint_hibp.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.error    # noqa: F401
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2          # noqa: F401
import psycopg2.extras   # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_osint_hibp.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_hibp_test_"))
ACCT = "someone" + "@" + "example.org"


def _load(accounts=""):
    import nova_config
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    spec = importlib.util.spec_from_file_location("nhibp", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_notify": nn}), \
         patch.object(nova_config, "_keychain", return_value=accounts), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    mod.LOG_FILE = TMP / "osint_hibp.log"
    mod.notify = MagicMock(return_value=True)
    return mod


hb = _load()


def _resp(obj):
    r = MagicMock(); r.read.return_value = json.dumps(obj).encode()
    r.__enter__ = lambda s: s; r.__exit__ = lambda s, *a: False
    return r


def _http(code):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b"rate limited"))


def _main(breaches, known=(), key="k", accounts=(ACCT,)):
    cur = MagicMock(); cur.fetchall.return_value = [{"finding": k} for k in known]
    conn = MagicMock(); conn.cursor.return_value = cur
    hb.notify.reset_mock()
    with patch.object(hb, "MONITORED_ACCOUNTS", list(accounts)), \
         patch.object(hb.nova_config, "_keychain", return_value=key), \
         patch.object(hb, "check_account", return_value=breaches), \
         patch.object(hb.psycopg2, "connect", return_value=conn) as connect, \
         patch.object(hb.time, "sleep"), redirect_stdout(io.StringIO()):
        rc = hb.main()
    return rc, cur, connect


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_addresses(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", SRC.replace("'<email1>,<email2>'", "")))
        self.assertIn('"nova-hibp-api-key"', SRC)
        self.assertIn('"nova-hibp-monitored-accounts"', SRC)

    def test_email_is_url_quoted_and_key_in_header(self):
        with patch.object(hb.urllib.request, "urlopen", return_value=_resp([])) as uo:
            hb.check_account("a/../b?x=1@example.org", "SEKRET")
        req = uo.call_args[0][0]
        self.assertIn("b%3Fx%3D1%40example.org?truncateResponse=false", req.full_url)   # no query smuggling
        self.assertNotIn("SEKRET", req.full_url)
        self.assertEqual(req.get_header("Hibp-api-key"), "SEKRET")

    def test_inserts_are_parameterized(self):
        _, cur, _ = _main([{"Name": "x'); DROP TABLE osint_findings;--"}])
        for c in cur.execute.call_args_list:
            self.assertNotIn("DROP", c[0][0])


class TestPerformance(unittest.TestCase):
    def test_diff_10k_breaches(self):
        breaches = [{"Name": f"B{i}"} for i in range(10_000)]
        t0 = time.perf_counter()
        _, cur, _ = _main(breaches, known=[f"B{i}" for i in range(9_990)])
        self.assertLess(time.perf_counter() - t0, 5.0)
        obs = [c for c in cur.execute.call_args_list if "shared_observations" in c[0][0]]
        self.assertEqual(len(json.loads(obs[0][0][1][1])["new_breaches"]), 10)


class TestRetry(unittest.TestCase):
    def test_http_errors_fail_open(self):
        # RETRY GAP: check_account — one GET per account; 404 means clean, other errors log and return []
        for exc in (_http(404), _http(429), OSError("down")):
            with patch.object(hb.urllib.request, "urlopen", side_effect=exc) as uo, redirect_stdout(io.StringIO()):
                self.assertEqual(hb.check_account(ACCT, "k"), [])
            self.assertEqual(uo.call_count, 1)

    def test_notify_failure_is_logged(self):
        cur = MagicMock(); cur.fetchall.return_value = []
        conn = MagicMock(); conn.cursor.return_value = cur
        hb.notify.side_effect = RuntimeError("bus down")
        try:
            with patch.object(hb, "MONITORED_ACCOUNTS", [ACCT]), patch.object(hb.nova_config, "_keychain", return_value="k"), \
                 patch.object(hb, "check_account", return_value=[{"Name": "New"}]), \
                 patch.object(hb.psycopg2, "connect", return_value=conn), patch.object(hb.time, "sleep"), \
                 redirect_stdout(io.StringIO()) as out:
                hb.main()
        finally:
            hb.notify.side_effect = None
        self.assertIn("notify failed: bus down", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_accounts_parsed_from_secret(self):
        m = _load(accounts=f" {ACCT} , ,b@example.org ")
        self.assertEqual(m.MONITORED_ACCOUNTS, [ACCT, "b@example.org"])
        self.assertEqual(_load(accounts="").MONITORED_ACCOUNTS, [])

    def test_log_writes_to_redirected_file(self):
        with redirect_stdout(io.StringIO()):
            hb.log("hello")
        self.assertIn("hello", hb.LOG_FILE.read_text())


class TestIntegration(unittest.TestCase):
    def test_new_vs_known_severity(self):
        _, cur, _ = _main([{"Name": "Old", "BreachDate": "2019"}, {"Name": "New"}], known=["Old"])
        ins = {c[0][1][1]: c[0][1][2] for c in cur.execute.call_args_list if "INSERT INTO osint_findings" in c[0][0]}
        self.assertEqual(ins, {"Old": "info", "New": "critical"})
        self.assertIn("tool='hibp'", cur.execute.call_args_list[0][0][0])


class TestFunctional(unittest.TestCase):
    def test_new_breach_records_and_alerts(self):
        rc, cur, _ = _main([{"Name": "Adobe"}])
        self.assertIsNone(rc)
        self.assertTrue(any("shared_observations" in c[0][0] for c in cur.execute.call_args_list))
        kw = hb.notify.call_args.kwargs
        self.assertEqual((kw["level"], kw["dedup_key"]), ("critical", "osint-hibp-new"))
        self.assertIn(f"{ACCT}: Adobe", kw["body"])

    def test_no_key_skips_without_pg_or_network(self):
        rc, _, connect = _main([], key="")
        self.assertEqual(rc, 1)
        connect.assert_not_called()
        hb.notify.assert_not_called()

    def test_all_known_is_quiet(self):
        _main([{"Name": "Old"}], known=["Old"])
        hb.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_smoke_without_keychain(self):
        # no --help: a bare run queries HIBP, so the frame check is an import with the secret store stubbed
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import nova_config; "
                "nova_config._keychain = lambda *a, **k: ''; import nova_osint_hibp as h; print(h.MONITORED_ACCOUNTS)")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "[]")

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(psycopg2, "connect", side_effect=AssertionError("import must not connect")):
            _load(accounts=ACCT)


if __name__ == "__main__":
    unittest.main()
