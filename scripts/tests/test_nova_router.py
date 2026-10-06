#!/usr/bin/env python3
"""Tests for nova_router.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import runpy
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_router.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rt = _load("router_under_test", SCRIPT)


class _Resp:
    def read(self):
        return b"ok"


def _probe(up):
    """A urlopen stand-in: `up` is the set of router IPs that answer /health; the rest raise."""
    calls = []

    def urlopen(url, timeout=None):
        calls.append((url, timeout))
        ip = url.split("//")[1].split(":")[0]
        if ip in up:
            return _Resp()
        raise OSError(f"{ip} down")
    return urlopen, calls


def _base(up, **kw):
    uo, calls = _probe(up)
    with patch.object(rt.urllib.request, "urlopen", uo):       # context-managed: the real urlopen is restored
        return rt.base(**kw), calls


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_only_private_lan_routers_and_no_caller_input_in_urls(self):
        for ip in rt.ROUTERS:
            self.assertTrue(ip.startswith("192.168.1."), ip)
        # the URL is built from the ROUTERS constant only; no argument ever reaches the host part
        self.assertNotIn("shell=True", SRC)
        self.assertEqual(rt.base.__code__.co_varnames[:1], ("timeout",))
        self.assertIsNone(re.search(r"urlopen\(\s*[a-z_]+\s*[,)]", SRC))   # never a caller-supplied URL


class TestPerformance(unittest.TestCase):
    def test_resolution_fast_on_10k_calls(self):
        uo, calls = _probe({"192.168.1.2"})
        with patch.object(rt.urllib.request, "urlopen", uo):
            t0 = time.perf_counter()
            for _ in range(10_000):
                rt.chat_url()
            dt = time.perf_counter() - t0
        self.assertLess(dt, 2.0)
        self.assertEqual(len(calls), 10_000)                 # one probe per call: the loop is bounded by ROUTERS


class TestRetry(unittest.TestCase):
    def test_primary_down_falls_back_to_standby_in_one_pass(self):
        # RETRY GAP: base() — each router is probed exactly once, no backoff; the "retry" is the hot standby
        b, calls = _base({"192.168.1.10"})
        self.assertEqual(b, "http://192.168.1.10:37475")
        self.assertEqual([c[0] for c in calls], ["http://192.168.1.2:37475/health", "http://192.168.1.10:37475/health"])

    def test_both_down_fails_open_to_primary_without_raising(self):
        # RETRY GAP: base() — when nothing answers the caller still gets the primary URL, never an exception
        b, calls = _base(set())
        self.assertEqual(b, "http://192.168.1.2:37475")
        self.assertEqual(len(calls), len(rt.ROUTERS))

    def test_slow_probe_honours_timeout_argument(self):
        _, calls = _base({"192.168.1.2"}, timeout=0.25)
        self.assertEqual(calls[0][1], 0.25)


class TestUnit(unittest.TestCase):
    def test_primary_up_wins_and_standby_is_not_probed(self):
        b, calls = _base({"192.168.1.2", "192.168.1.10"})
        self.assertEqual(b, "http://192.168.1.2:37475")
        self.assertEqual(len(calls), 1)

    def test_chat_url_suffix(self):
        uo, _ = _probe({"192.168.1.2"})
        with patch.object(rt.urllib.request, "urlopen", uo):
            self.assertEqual(rt.chat_url(), "http://192.168.1.2:37475/v1/chat/completions")

    def test_routers_order_is_primary_then_standby(self):
        self.assertEqual(rt.ROUTERS, ["192.168.1.2", "192.168.1.10"])

    def test_non_oserror_failures_are_also_skipped(self):
        uo = MagicMock(side_effect=[ValueError("junk"), _Resp()])
        with patch.object(rt.urllib.request, "urlopen", uo):
            self.assertEqual(rt.base(), "http://192.168.1.10:37475")


class TestIntegration(unittest.TestCase):
    def test_chat_url_composes_base_with_same_timeout(self):
        with patch.object(rt, "base", MagicMock(return_value="http://h:1")) as b:
            self.assertEqual(rt.chat_url(timeout=7), "http://h:1/v1/chat/completions")
        b.assert_called_once_with(7)

    def test_health_endpoint_and_port_match_the_fabric_router(self):
        _, calls = _base({"192.168.1.2"})
        self.assertEqual(calls[0][0], "http://192.168.1.2:37475/health")
        self.assertTrue(all(":37475/health" in c[0] for c in calls))


class TestFunctional(unittest.TestCase):
    def test_golden_path_failover_resolves_standby_chat_url(self):
        uo, calls = _probe({"192.168.1.10"})
        with patch.object(rt.urllib.request, "urlopen", uo):
            url = rt.chat_url()
        self.assertEqual(url, "http://192.168.1.10:37475/v1/chat/completions")
        self.assertEqual(len(calls), 2)

    def test_main_selfcheck_prints_endpoint_with_primary_up(self):
        uo, _ = _probe({"192.168.1.2"})
        buf = io.StringIO()
        with patch("urllib.request.urlopen", uo), redirect_stdout(buf):
            runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(buf.getvalue().strip(), "router endpoint -> http://192.168.1.2:37475")

    def test_main_selfcheck_survives_total_outage(self):
        uo, _ = _probe(set())
        buf = io.StringIO()
        with patch("urllib.request.urlopen", uo), redirect_stdout(buf):
            runpy.run_path(str(SCRIPT), run_name="__main__")      # asserts in __main__ hold on the primary fallback
        self.assertIn("192.168.1.2", buf.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_probes_the_network(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_router"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_module_exposes_the_two_entry_points(self):
        self.assertTrue(callable(rt.base) and callable(rt.chat_url))


if __name__ == "__main__":
    unittest.main()
