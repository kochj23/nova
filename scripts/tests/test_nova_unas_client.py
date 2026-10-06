#!/usr/bin/env python3
"""Tests for nova_unas_client.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Keychain reads (_load_api_key / _load_login_password), urlopen and time.sleep are mocked file-wide."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_unas_client.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("unas_client_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


uc = _load()
KEY = "test-api-key"
_PATCHES = []


def setUpModule():
    for p in (patch.object(uc, "_load_api_key", return_value=KEY),
              patch.object(uc, "_load_login_password", return_value=None),
              patch.object(uc.urllib.request, "urlopen", side_effect=OSError("offline")),
              patch.object(uc.time, "sleep")):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


def _resp(obj=None, raw=None, cookies=()):
    r = MagicMock(); r.__enter__.return_value = r
    r.read.return_value = raw if raw is not None else json.dumps(obj).encode()
    r.headers.get_all.return_value = list(cookies)
    return r


def _http(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


class _Fresh(unittest.TestCase):
    def setUp(self):
        uc._session_token = None


class TestSecurity(_Fresh):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_key_sent_in_header_only(self):
        with patch.object(uc.urllib.request, "urlopen", return_value=_resp({"ok": 1})) as u:
            uc._request("/api/system")
        req = u.call_args.args[0]
        self.assertEqual(req.get_header("X-api-key"), KEY)
        self.assertNotIn(KEY, req.full_url)

    def test_params_url_quoted(self):
        with patch.object(uc.urllib.request, "urlopen", return_value=_resp({})) as u:
            uc._request("/api/x", params={"q": "a b&c=d"})
        self.assertTrue(u.call_args.args[0].full_url.endswith("?q=a%20b%26c%3Dd"))

    def test_missing_key_raises_before_network(self):
        with patch.object(uc, "_load_api_key", return_value=None), patch.object(uc.urllib.request, "urlopen") as u:
            with self.assertRaises(uc.UNASError):
                uc._request("/api/system")
        u.assert_not_called()


class TestPerformance(_Fresh):
    def test_snapshot_with_5k_shares_fast(self):
        shares = [{"id": i, "name": f"s{i}", "status": "active", "usage": i * 10**9} for i in range(5000)]
        c = uc.UNASClient()
        with patch.object(c, "system_info", return_value={}), patch.object(c, "storage_summary", return_value={}), \
                patch.object(c, "shared_drives", return_value=shares):
            t0 = time.perf_counter()
            snap = c.health_snapshot()
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(snap["shares"]), 5000)


class TestRetry(_Fresh):
    def test_fails_twice_then_succeeds_with_backoff(self):
        seq = [urllib.error.URLError("down"), _http(503), _resp({"name": "unas"})]
        with patch.object(uc.urllib.request, "urlopen", side_effect=seq) as u, patch.object(uc.time, "sleep") as sl:
            self.assertEqual(uc._request("/api/system"), {"name": "unas"})
        self.assertEqual(u.call_count, 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [uc.RETRY_DELAY, uc.RETRY_DELAY * 2])

    def test_exhausted_raises(self):
        with patch.object(uc.urllib.request, "urlopen", side_effect=TimeoutError("t")) as u:
            with self.assertRaises(uc.UNASError) as cm:
                uc._request("/api/system", retries=2)
        self.assertEqual(u.call_count, 2)
        self.assertIn("after 2 attempts", str(cm.exception))

    def test_auth_and_404_not_retried(self):
        for code in (401, 403, 404):
            with patch.object(uc.urllib.request, "urlopen", side_effect=_http(code)) as u:
                with self.assertRaises(uc.UNASError):
                    uc._request("/api/system")
            self.assertEqual(u.call_count, 1, code)


class TestUnit(_Fresh):
    def test_empty_body_is_empty_dict(self):
        with patch.object(uc.urllib.request, "urlopen", return_value=_resp(raw=b"")):
            self.assertEqual(uc._request("/x"), {})

    def test_session_token_cookie_and_cache(self):
        with patch.object(uc, "_load_login_password", return_value="pw"), \
                patch.object(uc.urllib.request, "urlopen", return_value=_resp(cookies=["TOKEN=abc; Path=/"])) as u:
            self.assertEqual(uc._get_session_token(), "TOKEN=abc")
            self.assertEqual(uc._get_session_token(), "TOKEN=abc")
        self.assertEqual(u.call_count, 1)

    def test_session_token_from_body_and_no_password(self):
        self.assertIsNone(uc._get_session_token())
        with patch.object(uc, "_load_login_password", return_value="pw"), \
                patch.object(uc.urllib.request, "urlopen", return_value=_resp({"token": "t1"})):
            self.assertEqual(uc._get_session_token(), "t1")


class TestIntegration(_Fresh):
    def test_api_key_comes_from_nova_config_keychain(self):
        import nova_config
        with patch.object(nova_config, "_keychain", return_value="k") as kc:
            self.assertEqual(_load()._load_api_key(), "k")   # fresh copy: the real, unpatched loader
        self.assertEqual(kc.call_args.args[0], "nova")
        self.assertEqual(kc.call_args.kwargs["account"], "nova")

    def test_proxy_paths_carry_session_cookie(self):
        uc._session_token = "TOKEN=zz"
        with patch.object(uc.urllib.request, "urlopen", return_value=_resp({"data": []})) as u:
            self.assertEqual(uc.UNASClient().shared_drives(), [])
        self.assertEqual(u.call_args.args[0].get_header("Cookie"), "TOKEN=zz")


class TestFunctional(_Fresh):
    def test_health_snapshot_golden_path(self):
        c = uc.UNASClient()
        info = {"hardware": {"shortname": "UNASPRO8"}, "name": "UNAS", "deviceState": "setup"}
        storage = {"status": "healthy", "totalQuota": 10 * 10**12, "usage": {"sharedDrives": 6 * 10**12, "system": 10**12}}
        with patch.object(c, "system_info", return_value=info), patch.object(c, "storage_summary", return_value=storage), \
                patch.object(c, "shared_drives", side_effect=uc.UNASError("proxy down")):
            snap = c.health_snapshot()
        self.assertEqual((snap["storage"]["used_pct"], snap["storage"]["free_tb"]), (70.0, 3.0))
        self.assertEqual(snap["shares"], [])
        self.assertEqual(snap["device"]["state"], "setup")

    def test_ping(self):
        c = uc.UNASClient()
        with patch.object(c, "system_info", return_value={"hardware": {"x": 1}}):
            self.assertTrue(c.ping())
        with patch.object(c, "system_info", side_effect=uc.UNASError("x")):
            self.assertFalse(c.ping())


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_unas_client as m; print(m.MAX_RETRIES)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "3"), r.stderr)


if __name__ == "__main__":
    unittest.main()
