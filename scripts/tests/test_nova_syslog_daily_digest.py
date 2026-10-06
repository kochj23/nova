#!/usr/bin/env python3
"""Tests for nova_syslog_daily_digest.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_syslog_daily_digest.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_syslog_digest_test_"))


def _load():
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    spec = importlib.util.spec_from_file_location("nsyslogdigest", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_notify": nn}), patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    mod.LOG_FILE = TMP / "syslog_daily_digest.log"
    mod.notify = MagicMock(return_value=True)
    return mod


sd = _load()
NOW = datetime(2026, 1, 2, 5, 0, tzinfo=timezone.utc)


def _data(**kw):
    d = {"total": 1500, "top_sources": [("udm-pro", 900), ("nova-core", 600)],
         "threats": [("ips", "ET SCAN Nmap" + "x" * 100, "1.2.3.4", "192.168.1.2", "blocked", NOW)],
         "threat_summary": [("ips", 3)], "devices_seen": ["udm-pro", "nova-core"],
         "severity_breakdown": [(3, 10), (6, 1490), (9, 1)], "top_apps": [("named", 700)], "alerts_fired": 2}
    d.update(kw); return d


class _Cur:
    def __init__(self, total=1500):
        self.total = total; self.sql = []; self.params = []

    def execute(self, sql, params=None): self.sql.append(sql); self.params.append(params); self._last = sql
    def fetchone(self): return (self.total,) if "alert_fired" not in self._last else (2,)
    def fetchall(self):
        q = self._last
        if "DISTINCT" in q: return [("udm-pro",), (None,), ("nova-core",)]
        if "GROUP BY device" in q: return [("udm-pro", 900)]
        if "GROUP BY threat_type" in q: return [("ips", 3)]
        if "threat_type IS NOT NULL" in q: return [("ips", "sig", "1.2.3.4", "5.6.7.8", "blocked", NOW)]
        if "GROUP BY severity" in q: return [(6, 1500)]
        if "GROUP BY app_name" in q: return [("named", 700)]
        return []
    def close(self): pass


def _main(argv=(), total=1500, remember_ok=True):
    cur = _Cur(total)
    conn = MagicMock(); conn.cursor.return_value = cur
    sd.notify.reset_mock()
    with patch.object(sys, "argv", ["x", *argv]), patch.object(sd.psycopg2, "connect", return_value=conn), \
         patch.object(sd, "remember", return_value=remember_ok) as rem, redirect_stdout(io.StringIO()) as out:
        sd.main()
    return cur, rem, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"postgresql://\w+:[^@]+@")       # no password in the DSN

    def test_every_query_is_parameterized(self):
        cur = _Cur()
        sd.generate_digest(MagicMock(cursor=MagicMock(return_value=cur)), NOW - timedelta(days=1), NOW)
        self.assertEqual(len(cur.sql), 8)
        for sql, p in zip(cur.sql, cur.params):
            self.assertEqual(sql.count("%s"), 2)
            self.assertEqual(len(p), 2)
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')

    def test_threat_signature_truncated(self):
        txt = sd.format_digest(_data(), "d")
        sig_line = [l for l in txt.splitlines() if l.strip().startswith("[ips]")][0]
        self.assertLessEqual(sig_line.count("x"), 80)


class TestPerformance(unittest.TestCase):
    def test_format_10k_threat_rows(self):
        big = _data(threats=[("ips", f"sig{i}", "a", "b", "c", NOW) for i in range(10_000)],
                    devices_seen=[f"d{i}" for i in range(10_000)])
        t0 = time.perf_counter()
        txt = sd.format_digest(big, "d")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(txt.count("[ips]"), 5)


class TestRetry(unittest.TestCase):
    def test_remember_fails_open(self):
        # RETRY GAP: remember — one POST to the memory server; failure logs and returns False
        fresh = _load()
        with patch.object(fresh.urllib.request, "urlopen", side_effect=OSError("down")) as uo, redirect_stdout(io.StringIO()):
            self.assertFalse(fresh.remember("t", {}))
        self.assertEqual(uo.call_count, 1)

    def test_ingest_failure_still_notifies(self):
        _, _, out = _main(remember_ok=False)
        self.assertIn("Failed to ingest digest", out)
        sd.notify.assert_called_once()


class TestUnit(unittest.TestCase):
    def test_format_sections(self):
        txt = sd.format_digest(_data(), "2026-01-01")
        self.assertIn("Total events: 1,500", txt)
        self.assertIn("err: 10", txt); self.assertIn("sev9: 1", txt)
        self.assertIn("Threat summary:\n  ips: 3", txt)

    def test_clean_network_line(self):
        txt = sd.format_digest(_data(threat_summary=[], threats=[]), "d")
        self.assertIn("No security threats detected. Network clean.", txt)

    def test_log_goes_to_redirected_file(self):
        with redirect_stdout(io.StringIO()):
            sd.log("hello digest")
        self.assertIn("hello digest", sd.LOG_FILE.read_text())


class TestIntegration(unittest.TestCase):
    def test_remember_payload_uses_shared_truncation(self):
        fresh = _load()
        with patch.object(fresh.urllib.request, "urlopen") as uo, \
             patch.object(fresh.nova_config, "truncate_at_boundary", side_effect=lambda t: t[:10]) as tr:
            self.assertTrue(fresh.remember("x" * 50, {"type": "syslog_daily_digest"}))
        body = json.loads(uo.call_args[0][0].data)
        self.assertEqual((body["text"], body["source"], body["tier"]), ("x" * 10, "infrastructure", "long_term"))
        tr.assert_called_once()

    def test_generate_digest_shape(self):
        d = sd.generate_digest(MagicMock(cursor=MagicMock(return_value=_Cur())), NOW, NOW)
        self.assertEqual(d["devices_seen"], ["udm-pro", "nova-core"])
        self.assertEqual(d["alerts_fired"], 2)


class TestFunctional(unittest.TestCase):
    def test_yesterday_digest_ingests_and_notifies(self):
        cur, rem, _ = _main()
        start, end = cur.params[0]
        self.assertEqual(end - start, timedelta(days=1))
        self.assertEqual((end.hour, end.minute), (0, 0))
        self.assertEqual(rem.call_args[0][1]["threats_count"], 3)
        kw = sd.notify.call_args.kwargs
        self.assertEqual((kw["category"], kw["dedup_key"]), ("syslog", "syslog-daily-digest"))
        self.assertIn("Threats: ips(3)", kw["body"])

    def test_empty_period_skips_everything(self):
        _, rem, out = _main(total=0)
        rem.assert_not_called()
        sd.notify.assert_not_called()
        self.assertIn("No syslog events in period", out)


class TestFrame(unittest.TestCase):
    def test_import_smoke(self):
        # no --help: a bare run reads PG and posts, so the frame check is the import
        code = "import sys; sys.path.insert(0, sys.argv[1]); import nova_syslog_daily_digest as m; print(m.MEMORY_URL.rsplit('/', 1)[-1])"
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "remember")

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(psycopg2, "connect", side_effect=AssertionError("import must not connect")):
            _load()


if __name__ == "__main__":
    unittest.main()
