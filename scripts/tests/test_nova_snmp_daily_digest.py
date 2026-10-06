#!/usr/bin/env python3
"""Tests for nova_snmp_daily_digest.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_snmp_daily_digest.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("snmp_daily_digest_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sd = _load()
_TD = tempfile.TemporaryDirectory()
_PATCHES = []


def setUpModule():
    for p in (patch.object(sd, "LOG_FILE", Path(_TD.name) / "snmp.log"),
              patch.object(sd, "notify", side_effect=AssertionError("unmocked notify")),
              patch.object(sd.urllib.request, "urlopen", side_effect=OSError("offline")),
              patch.object(sd.psycopg2, "connect", side_effect=AssertionError("unmocked PG")),
              patch.object(sd.nova_config, "post_both")):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


def _pg(total=1200, devices=(("router", "192.168.1.1"), ("nas", "192.168.1.5")),
        cpu=(("router", 0.5, 1.9), ("nas", 1.2, 3.4)), mem=(("nas", 16777216, 4194304), ("router", 0, None)),
        traffic=(), alerts=(("nas", "cpu", 3.4, 3.0, datetime(2026, 1, 1, 4, 5)),)):
    cur = MagicMock()
    cur.fetchone.return_value = (total,)
    cur.fetchall.side_effect = [list(devices), list(cpu), list(mem), list(traffic), list(alerts)]
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn, cur


def _ok():
    r = MagicMock(); r.__enter__.return_value = r
    return r


def _run(conn, urlopen=None, notify=None):
    with patch.object(sd.psycopg2, "connect", return_value=conn), \
            patch.object(sd.urllib.request, "urlopen", **(urlopen or {"return_value": _ok()})) as u, \
            patch.object(sd, "notify", **(notify or {})) as n, redirect_stdout(io.StringIO()) as out:
        sd.run()
    return u, n, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_every_query_parameterized_by_window(self):
        conn, cur = _pg()
        _run(conn)
        for c in cur.execute.call_args_list:
            sql, params = c.args
            self.assertEqual(len(params), 2)
            self.assertNotIn(str(params[0].year) + "-", sql)
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))


class TestPerformance(unittest.TestCase):
    def test_digest_with_2k_devices_fast(self):
        devs = [(f"d{i}", f"10.0.{i // 250}.{i % 250}") for i in range(2000)]
        cpu = [(f"d{i}", 1.0, float(i)) for i in range(2000)]
        mem = [(f"d{i}", 8388608, 1048576) for i in range(2000)]
        conn, _ = _pg(devices=devs, cpu=cpu, mem=mem, alerts=())
        t0 = time.perf_counter()
        u, n, out = _run(conn)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertIn("Peak CPU: d1999", n.call_args.kwargs["body"])


class TestRetry(unittest.TestCase):
    def test_memory_ingest_failure_fails_open(self):
        # RETRY GAP: remember() — one POST to the memory server; failure is logged, digest still notified
        conn, _ = _pg()
        u, n, out = _run(conn, urlopen={"side_effect": OSError("503")})
        self.assertEqual(u.call_count, 1)
        self.assertIn("Ingest error", out)
        n.assert_called_once()

    def test_notify_failure_logged(self):
        conn, _ = _pg()
        u, n, out = _run(conn, notify={"side_effect": RuntimeError("bus down")})
        self.assertIn("Notify failed: bus down", out)
        self.assertIn("Digest complete", out)


class TestUnit(unittest.TestCase):
    def test_log_writes_to_redirected_file(self):
        with redirect_stdout(io.StringIO()):
            sd.log("hello-unit")
        self.assertIn("hello-unit", sd.LOG_FILE.read_text())
        self.assertTrue(str(sd.LOG_FILE).startswith(_TD.name))

    def test_remember_payload(self):
        with patch.object(sd.urllib.request, "urlopen", return_value=_ok()) as u:
            self.assertTrue(sd.remember("text", {"k": 1}))
        body = json.loads(u.call_args.args[0].data)
        self.assertEqual((body["source"], body["tier"], body["metadata"]), ("infrastructure", "long_term", {"k": 1}))


class TestIntegration(unittest.TestCase):
    def test_uses_shared_truncate_and_notify(self):
        import nova_notify
        self.assertIn("from nova_notify import notify", SRC)
        self.assertTrue(callable(nova_notify.notify))
        with patch.object(sd.nova_config, "truncate_at_boundary", return_value="CUT") as t, \
                patch.object(sd.urllib.request, "urlopen", return_value=_ok()) as u:
            sd.remember("x" * 50_000, {})
        t.assert_called_once()
        self.assertEqual(json.loads(u.call_args.args[0].data)["text"], "CUT")

    def test_reads_snmp_tables(self):
        conn, cur = _pg()
        _run(conn)
        sql = " ".join(c.args[0] for c in cur.execute.call_args_list)
        self.assertIn("FROM snmp_metrics", sql)
        self.assertIn("FROM snmp_alert_state", sql)


class TestFunctional(unittest.TestCase):
    def test_golden_path_digest_text_and_notify(self):
        conn, _ = _pg()
        u, n, out = _run(conn)
        text = json.loads(u.call_args.args[0].data)["text"]
        self.assertIn("Total data points: 1,200", text)
        self.assertIn("Devices reporting: 2 (router, nas)", text)
        self.assertIn("nas: 16.0GB total, peak usage 75%", text)
        self.assertIn("04:05 nas: cpu (3.4 > 3.0)", text)
        self.assertNotIn("router: 0.0GB", text)
        kw = n.call_args.kwargs
        self.assertEqual((kw["level"], kw["category"], kw["dedup_key"]), ("info", "snmp", "snmp-daily-digest"))
        self.assertIn("1 threshold alerts", kw["body"])
        conn.close.assert_called_once()

    def test_no_metrics_skips_everything(self):
        conn, cur = _pg(total=0)
        u, n, out = _run(conn)
        u.assert_not_called(); n.assert_not_called()
        self.assertIn("skipping digest", out)
        conn.close.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_snmp_daily_digest"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
