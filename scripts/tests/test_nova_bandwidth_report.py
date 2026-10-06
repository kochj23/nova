#!/usr/bin/env python3
"""Tests for nova_bandwidth_report.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_bandwidth_report.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_bandwidth_report_t", SCRIPTS / "nova_bandwidth_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bw = _load()
bw.notify = mock.MagicMock()   # never page from a test


def _resp(data):
    r = mock.MagicMock()
    r.read.return_value = json.dumps({"data": data}).encode()
    r.__enter__ = lambda s: s
    r.__exit__ = lambda s, *a: False
    return r


CLIENTS = [{"hostname": f"dev{i}", "tx_bytes": i * 2**30, "rx_bytes": i * 2**29} for i in range(1, 15)]
HEALTH = [{"subsystem": "wan", "status": "ok", "latency": 12, "uptime": 90000, "speedtest_download": 900.0,
           "speedtest_upload": 40.0, "speedtest_ping": 9.0, "rx_bytes-r": 125000, "tx_bytes-r": 0,
           "gateways": [{"isp_name": "ISP", "wan_ip": "203.0.113.5"}]}]


def _router(url_obj, *a, **k):
    url = url_obj.full_url if hasattr(url_obj, "full_url") else str(url_obj)
    if "stat/sta" in url:
        return _resp(CLIENTS)
    if "hourly.site" in url:
        return _resp([{"wan-rx_bytes": 3 * 2**30, "wan-tx_bytes": 2**30}])
    if "stat/health" in url:
        return _resp(HEALTH)
    if "stat/device" in url:
        return _resp([{"type": "udm", "uptime": 5}])
    return _resp([])


class _Env:
    """Redirect Path.home to a tempdir and capture notify + urlopen."""
    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        self.home = Path(self.td.name)
        self.ps = [mock.patch.object(Path, "home", return_value=self.home),
                   mock.patch.object(bw, "get_api_key", return_value="k"),
                   mock.patch.object(bw, "notify"),
                   mock.patch.object(bw.urllib.request, "urlopen", side_effect=_router),
                   mock.patch("sys.stdout", new_callable=io.StringIO)]
        self.m = [p.start() for p in self.ps]
        self.notify, self.urlopen = self.m[2], self.m[3]
        return self

    def __exit__(self, *a):
        for p in reversed(self.ps):
            p.stop()
        self.td.cleanup()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_api_key_comes_from_keychain(self):
        with mock.patch.object(bw.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout="secret-key\n")
            self.assertEqual(bw.get_api_key(), "secret-key")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ["security", "find-generic-password"])
        self.assertIn("nova-unifi-api-key", argv)

    def test_no_sql_and_key_sent_as_header_not_url(self):
        self.assertNotRegex(SRC, r"\b(INSERT INTO|DELETE FROM|UPDATE \w+ SET)\b")
        with mock.patch.object(bw.urllib.request, "urlopen", side_effect=_router) as uo:
            bw.api_get("stat/sta", "KEY123")
        req = uo.call_args[0][0]
        self.assertNotIn("KEY123", req.full_url)
        self.assertEqual(req.headers.get("X-api-key"), "KEY123")


class TestPerformance(unittest.TestCase):
    def test_main_ranks_10k_clients_quickly(self):
        global CLIENTS
        saved = CLIENTS
        CLIENTS = [{"hostname": f"h{i}", "tx_bytes": i, "rx_bytes": i} for i in range(10_000)]
        try:
            with _Env() as e:
                t0 = time.perf_counter()
                bw.main()
                self.assertLess(time.perf_counter() - t0, 3.0)
                self.assertIn("h9999", e.notify.call_args[1]["body"])
        finally:
            CLIENTS = saved


class TestRetry(unittest.TestCase):
    def test_wan_daily_fails_open(self):
        # RETRY GAP: get_wan_daily/api_post — a single attempt; on failure returns (0, 0)
        with mock.patch.object(bw.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertEqual(bw.get_wan_daily("k"), (0, 0))
        self.assertEqual(uo.call_count, 1)

    def test_wan_health_fails_open(self):
        # RETRY GAP: get_wan_health/api_get — each endpoint tried once, empty dict on failure
        with mock.patch.object(bw.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertEqual(bw.get_wan_health("k"), {})
        self.assertEqual(uo.call_count, 3)

    def test_main_client_api_error_returns_without_posting(self):
        # RETRY GAP: main()/stat/sta — one attempt, prints error, never notifies
        with _Env() as e:
            e.urlopen.side_effect = OSError("refused")
            self.assertIsNone(bw.main())
            e.notify.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_slack_post_splits_title_and_body(self):
        with mock.patch.object(bw, "notify") as n:
            bw.slack_post("*Title*\nline1\nline2")
            self.assertEqual(n.call_args[0][0], "Title")
            self.assertEqual(n.call_args[1]["body"], "line1\nline2")
            self.assertEqual(n.call_args[1]["dedup_key"], "bandwidth-daily-report")
            bw.slack_post("only title")
            self.assertIsNone(n.call_args[1]["body"])

    def test_wan_health_parses_subsystem(self):
        with mock.patch.object(bw.urllib.request, "urlopen", side_effect=_router):
            h = bw.get_wan_health("k")
        self.assertEqual(h["status"], "ok")
        self.assertEqual(h["isp"], "ISP")
        self.assertAlmostEqual(h["rx_rate_mbps"], 1.0)

    def test_empty_key_short_circuits(self):
        with mock.patch.object(bw, "get_api_key", return_value=""), \
             mock.patch.object(bw.urllib.request, "urlopen") as uo:
            bw.main()
        uo.assert_not_called()


class TestIntegration(unittest.TestCase):
    def test_uses_shared_config_and_notify(self):
        self.assertIn("import nova_config", SRC)
        self.assertIn("from nova_notify import notify", SRC)
        self.assertEqual(bw.VECTOR_URL, bw.nova_config.VECTOR_URL)

    def test_daily_then_report_chains_wan_totals(self):
        with mock.patch.object(bw.urllib.request, "urlopen", side_effect=_router):
            down, up = bw.get_wan_daily("k")
        self.assertEqual((down, up), (3 * 2**30, 2**30))


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_writes_state_and_memory(self):
        with _Env() as e:
            bw.main()
            body = e.notify.call_args[1]["body"]
            self.assertIn("dev14", body)
            self.assertIn("Today: 3.0G down / 1.0G up", body)
            state = json.loads((e.home / ".openclaw/workspace/state/bandwidth_yesterday.json").read_text())
            self.assertEqual(state["client_count"], 14)
            mem = list((e.home / ".openclaw/workspace/memory").glob("*.md"))
            self.assertEqual(len(mem), 1)
            self.assertIn("Network tonight", mem[0].read_text())
            urls = [c[0][0].full_url for c in e.urlopen.call_args_list]
            self.assertTrue(any("?async=1" in u for u in urls))

    def test_delta_vs_yesterday_and_no_clients_no_crash(self):
        global CLIENTS
        with _Env() as e:
            sd = e.home / ".openclaw/workspace/state"
            sd.mkdir(parents=True)
            (sd / "bandwidth_yesterday.json").write_text(json.dumps({"wan_total_gb": 2.0, "devices": []}))
            bw.main()
            self.assertIn("100% vs yesterday", e.notify.call_args[1]["body"])
            saved, CLIENTS = CLIENTS, []
            try:
                bw.main()   # used to IndexError on top10[0]
            finally:
                CLIENTS = saved
            self.assertIn("0 clients connected", e.notify.call_args[1]["body"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_bandwidth_report"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
