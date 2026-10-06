#!/usr/bin/env python3
"""Tests for nova_openrouter_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_openrouter_watch.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_openrouter_watch_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ow = _load()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _resp(credits, usage):
    return _Resp(json.dumps({"data": {"total_credits": credits, "total_usage": usage}}).encode())


class _Base(unittest.TestCase):
    def setUp(self):
        self.notify = MagicMock()
        self.ps = [patch.object(ow.nova_config, "openrouter_api_key", return_value="sk-test"),
                   patch.object(ow.urllib.request, "urlopen", side_effect=OSError("offline")),
                   patch.dict(sys.modules, {"nova_notify": types.SimpleNamespace(notify=self.notify)})]
        _, self.urlopen, _ = [p.start() for p in self.ps]
        self._r = redirect_stdout(io.StringIO())
        self.out = self._r.__enter__()

    def tearDown(self):
        self._r.__exit__(None, None, None)
        for p in self.ps:
            p.stop()

    def main(self, argv=()):
        with patch.object(sys, "argv", ["x", *argv]):
            return ow.main()


class TestSecurity(_Base):
    def test_no_hardcoded_key(self):
        self.assertNotRegex(SRC, r"sk-or-[A-Za-z0-9]")
        self.assertIn("nova_config.openrouter_api_key()", SRC)

    def test_bearer_header_from_keychain_key(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = _resp(100, 1)
        ow.fetch_balance()
        req = self.urlopen.call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bearer sk-test")
        self.assertEqual(req.full_url, ow.CREDITS_URL)

    def test_missing_key_refuses(self):
        with patch.object(ow.nova_config, "openrouter_api_key", return_value=""):
            with self.assertRaises(RuntimeError):
                ow.fetch_balance()
        self.urlopen.assert_not_called()


class TestPerformance(_Base):
    def test_fetch_balance_many(self):
        self.urlopen.side_effect = lambda *a, **k: _resp("2160.02", "2000")
        t0 = time.perf_counter()
        for _ in range(2000):
            ow.fetch_balance()
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(_Base):
    def test_http_error_fails_open(self):
        # RETRY GAP: fetch_balance — one GET per daily run; HTTP/network error -> rc 1, no alert
        self.urlopen.side_effect = urllib.error.HTTPError(ow.CREDITS_URL, 402, "pay", {}, None)
        self.assertEqual(self.main(), 1)
        self.assertIn("HTTP 402", self.out.getvalue())
        self.assertEqual(self.urlopen.call_count, 1)
        self.notify.assert_not_called()

    def test_network_error_fails_open(self):
        self.assertEqual(self.main(), 1)
        self.assertIn("credits check failed", self.out.getvalue())


class TestUnit(_Base):
    def test_balance_math_and_nulls(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = _resp(None, None)
        self.assertEqual(ow.fetch_balance(), (0.0, 0.0, 0.0))
        self.urlopen.return_value = _Resp(b"{}")
        self.assertEqual(ow.fetch_balance(), (0.0, 0.0, 0.0))
        self.urlopen.return_value = _resp("10.5", "3")
        self.assertEqual(ow.fetch_balance(), (10.5, 3.0, 7.5))


class TestIntegration(_Base):
    def test_alert_routes_through_nova_notify(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = _resp(2160.00, 2160.02)
        self.assertEqual(self.main(), 0)
        kw = self.notify.call_args.kwargs
        self.assertEqual((kw["level"], kw["category"], kw["source"], kw["dedup_key"]),
                         ("warning", "finance", ow.SOURCE, "openrouter-low-credit"))
        self.assertEqual(kw["meta"], {"dedup_window_s": 86400})


class TestFunctional(_Base):
    def test_healthy_balance_no_alert(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = _resp(100, 10)
        self.assertEqual(self.main(), 0)
        self.notify.assert_not_called()
        self.assertIn("remaining=$90.00", self.out.getvalue())

    def test_low_balance_alerts_unless_dry_run(self):
        self.urlopen.side_effect = lambda *a, **k: _resp(10, 8)
        self.assertEqual(self.main(["--dry-run"]), 0)
        self.notify.assert_not_called()
        self.assertEqual(self.main(), 0)
        self.assertIn("$2.00 remaining", self.notify.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_openrouter_watch; print('ok')"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
