#!/usr/bin/env python3
"""Tests for nova_security_patch_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import unittest
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_security_patch_watch.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="patch_watch_test_"))

import nova_config  # noqa: E402,F401


def _load():
    spec = importlib.util.spec_from_file_location("nspw", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("urllib.request.urlopen", side_effect=RuntimeError("offline")), \
         patch("subprocess.run", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


pw = _load()
pw.LOG_FILE = TMP / "security_patch_watch.log"
pw.STATE_FILE = TMP / "state" / "patch_watch_seen.json"
pw.subprocess = MagicMock(name="subprocess")          # the journal breaking-alert script is never launched


def _fake_today(d):
    class D(date):
        @classmethod
        def today(cls):
            return d
    return D


def _resp(body):
    r = MagicMock(); r.read.return_value = body if isinstance(body, bytes) else json.dumps(body).encode()
    r.__enter__ = lambda s: s; r.__exit__ = lambda s, *a: False
    return r


def _kernel(version, days_ago=0, moniker="mainline"):
    return {"version": version, "moniker": moniker,
            "released": {"isodate": (date.today() - timedelta(days=days_ago)).isoformat()}}


def _quiet():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sources_are_https_and_alert_uses_argv(self):
        for u in (pw.APPLE_SECURITY_URL, pw.KERNEL_URL, pw.PG_URL):
            self.assertTrue(u.startswith("https://"), u)
        self.assertNotIn("shell=True", SRC)
        pw.subprocess.run.reset_mock()
        with _quiet():
            pw.fire_alert("Kernel `rm -rf /` 7.0", "d; echo pwned")
        argv = pw.subprocess.run.call_args[0][0]
        self.assertEqual(argv[2:], ["breaking", "Kernel `rm -rf /` 7.0", "d; echo pwned"])   # one argv slot, no shell


class TestPerformance(unittest.TestCase):
    def test_apple_page_10k_entries_bounded(self):
        page = " ".join(f"macOS Sequoia 15.{i}" for i in range(10_000)).encode()
        t0 = time.perf_counter()
        with patch.object(pw.urllib.request, "urlopen", return_value=_resp(page)), _quiet():
            alerts = pw.check_apple_security(set())
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(alerts), 1)                       # one Apple alert per run


class TestRetry(unittest.TestCase):
    def test_fetch_failures_are_one_shot_and_fail_open(self):
        # RETRY GAP: check_kernel_releases()/check_apple_security() — one GET each, no retry; failure returns []
        op = MagicMock(side_effect=OSError("dns"))
        with patch.object(pw.urllib.request, "urlopen", op), _quiet():
            self.assertEqual(pw.check_kernel_releases(set()), [])
            self.assertEqual(pw.check_apple_security(set()), [])
        self.assertEqual(op.call_count, 2)

    def test_alert_launch_failure_is_logged(self):
        with patch.object(pw.subprocess, "run", side_effect=OSError("no python")), _quiet():
            pw.fire_alert("t", "d")
        self.assertIn("Alert fire failed: no python", pw.LOG_FILE.read_text())


class TestUnit(unittest.TestCase):
    def test_is_patch_tuesday(self):
        with patch.object(pw, "date", _fake_today(date(2026, 10, 13))):      # 2nd Tuesday
            self.assertTrue(pw.is_patch_tuesday())
        with patch.object(pw, "date", _fake_today(date(2026, 10, 6))):       # 1st Tuesday
            self.assertFalse(pw.is_patch_tuesday())
        with patch.object(pw, "date", _fake_today(date(2026, 10, 14))):      # Wednesday
            self.assertFalse(pw.is_patch_tuesday())

    def test_kernel_filters(self):
        rels = {"releases": [_kernel("7.1"), _kernel("7.0.11"), _kernel("7.2-rc3"), _kernel("6.9", days_ago=10)]}
        seen = set()
        with patch.object(pw.urllib.request, "urlopen", return_value=_resp(rels)), _quiet():
            alerts = pw.check_kernel_releases(seen)
        self.assertEqual([a[0] for a in alerts], ["Linux Kernel 7.1 Released (mainline)"])
        self.assertEqual(seen, {"kernel-7.1", "kernel-7.0.11", "kernel-6.9"})

    def test_microsoft_dedups_per_day(self):
        seen = set()
        with patch.object(pw, "is_patch_tuesday", return_value=True):
            self.assertEqual(len(pw.check_microsoft_patch_tuesday(seen)), 1)
            self.assertEqual(pw.check_microsoft_patch_tuesday(seen), [])

    def test_seen_roundtrip_and_corrupt_state(self):
        pw.save_seen({"a", "b"})
        self.assertEqual(pw.load_seen(), {"a", "b"})
        pw.STATE_FILE.write_text("{corrupt")
        self.assertEqual(pw.load_seen(), set())


class TestIntegration(unittest.TestCase):
    def test_alerts_route_through_journal_breaking_script(self):
        self.assertEqual(pw.JOURNAL_SCRIPT.name, "nova_journal_security.py")
        self.assertIn('"breaking"', SRC)
        self.assertTrue((SCRIPTS / "nova_journal_security.py").exists())


class TestFunctional(unittest.TestCase):
    def test_run_fires_at_most_two_alerts_and_persists_seen(self):
        pw.STATE_FILE.unlink(missing_ok=True)
        pw.subprocess.run.reset_mock()

        def op(req, timeout=None):
            if "kernel.org" in req.full_url:
                return _resp({"releases": [_kernel("7.1"), _kernel("7.2")]})
            return _resp(b"iOS 26.1 and macOS Tahoe 26.1")
        with patch.object(pw, "is_patch_tuesday", return_value=True), \
             patch.object(pw.urllib.request, "urlopen", side_effect=op), patch.object(pw.time, "sleep") as sl, _quiet():
            pw.run()
        self.assertEqual(pw.subprocess.run.call_count, 2)
        self.assertEqual(sl.call_count, 2)
        self.assertTrue(pw.subprocess.run.call_args_list[0][0][0][3].startswith("Microsoft Patch Tuesday"))
        self.assertIn("kernel-7.1", pw.load_seen())

    def test_quiet_day_fires_nothing(self):
        pw.STATE_FILE.unlink(missing_ok=True)
        pw.subprocess.run.reset_mock()
        with patch.object(pw, "is_patch_tuesday", return_value=False), \
             patch.object(pw.urllib.request, "urlopen", side_effect=OSError("offline")), _quiet():
            pw.run()
        pw.subprocess.run.assert_not_called()
        self.assertIn("No new patch events", pw.LOG_FILE.read_text())


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        # no --help: a bare run fetches vendor pages and can fire alerts, so the smoke is an import
        r = subprocess.run([sys.executable, "-c", "import nova_security_patch_watch"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
