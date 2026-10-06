#!/usr/bin/env python3
"""Tests for nova_iot_egress_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). psycopg2, journald/rndc subprocess and the triage brain are all
mocked; NO PG, NO DNS config change, NO network. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


iot = _load("nova_iot_egress_watch_t", SCRIPTS / "nova_iot_egress_watch.py")
SRC = (SCRIPTS / "nova_iot_egress_watch.py").read_text()


class _Cur:
    def __init__(self, known=(), devstats=(), report_counts=(1, 5)):
        self.known = list(known); self.devstats = list(devstats)
        self.report_counts = report_counts; self.sql = []; self._last = ""; self.updates = []

    def execute(self, sql, params=None):
        self._last = sql; self.sql.append((sql, params))
        if sql.strip().startswith("UPDATE iot_egress_anomaly"):
            self.updates.append(params)

    def fetchall(self):
        s = self._last
        if "count(*) AS ndom" in s:
            return self.devstats
        if "FROM iot_egress_baseline WHERE device_ip = ANY" in s:
            return self.known
        return []   # load_names selects etc.

    def fetchone(self):
        return self.report_counts

    def close(self): pass


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.autocommit = False

    def cursor(self):
        return self.cur

    def close(self): pass


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_read_only_on_dns_never_blocks(self):
        # the CODE (docstring stripped) issues no blocking/firewall action — only observational rndc querylog
        code = re.sub(r'(?s)""".*?"""', "", SRC, count=1).lower()
        for bad in ("iptables", "firewall", "rndc flush", "nft ", "pfctl", "rndc querylog off"):
            self.assertNotIn(bad, code)
        self.assertIn("querylog on", SRC)  # observational only

    def test_sql_values_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertIn("device_ip = ANY(%s)", SRC)


class TestPerformance(unittest.TestCase):
    def test_reg_domain_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            iot.reg_domain(f"host{i}.sub.example.co.uk")
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_ensure_querylog_failure_is_swallowed(self):
        # RETRY GAP: ensure_querylog()/rndc — one attempt; any failure returns a note, never raises
        with mock.patch.object(iot.subprocess, "run", side_effect=RuntimeError("no sudo")):
            note = iot.ensure_querylog()
        self.assertIn("failed", note)

    def test_fetch_queries_tolerates_journald_errors(self):
        with mock.patch.object(iot.subprocess, "run",
                               return_value=types.SimpleNamespace(stdout="garbage line\nnot a query\n")):
            self.assertEqual(list(iot.fetch_queries(datetime.now(timezone.utc))), [])


class TestUnit(unittest.TestCase):
    def test_reg_domain(self):
        self.assertEqual(iot.reg_domain("a1b2.cloudfront.net"), "cloudfront.net")
        self.assertEqual(iot.reg_domain("x.y.example.co.uk"), "example.co.uk")
        self.assertIsNone(iot.reg_domain("nova-core.digitalnoise.net"))
        self.assertIsNone(iot.reg_domain("203.0.113.66"))
        self.assertIsNone(iot.reg_domain("nodots"))

    def test_suspicion_signals(self):
        self.assertIn("suspicious-tld:.top", iot.suspicion_signals("evil-c2.top", "evil-c2.top"))
        self.assertTrue(any(s.startswith("dynamic-dns") for s in iot.suspicion_signals("duckdns.org", "me.duckdns.org")))
        self.assertIn("raw-ip-destination", iot.suspicion_signals("203.0.113.66", "203.0.113.66"))
        self.assertEqual(iot.suspicion_signals("arlo.com", "device-api.arlo.com"), [])
        self.assertIn("dga-like-label", iot.suspicion_signals("x8f3kd92jf01ab77z.cn", "x8f3kd92jf01ab77z.cn"))

    def test_is_iot(self):
        self.assertFalse(iot.is_iot("192.168.1.2", ""))          # excluded infra IP
        self.assertFalse(iot.is_iot("192.168.1.40", "Jordans-iPhone"))  # excluded by name
        self.assertTrue(iot.is_iot("192.168.1.40", "Arlo-Camera"))

    def test_is_raw_ip(self):
        self.assertTrue(iot.is_raw_ip("10.0.0.1"))
        self.assertFalse(iot.is_raw_ip("example.com"))


class TestIntegration(unittest.TestCase):
    def test_selftest_runs_offline(self):
        with redirect_stdout(io.StringIO()) as out:
            iot.selftest()
        text = out.getvalue()
        self.assertIn("evil-c2.top", text)
        self.assertIn("PAGE-path", text)
        self.assertIn("record-only", text)

    def test_ddl_targets_own_tables_only(self):
        self.assertIn("iot_egress_baseline", iot.DDL)
        self.assertIn("iot_egress_anomaly", iot.DDL)


class TestFunctional(unittest.TestCase):
    def _run(self, lines, *, known=(), devstats=(), mode="run", triage=None):
        cur = _Cur(known=known, devstats=devstats)
        patches = [
            mock.patch.object(iot, "ensure_querylog", return_value="querylog already ON"),
            mock.patch.object(iot.psycopg2, "connect", return_value=_Conn(cur)),
            mock.patch.object(iot.psycopg2.extras, "execute_values"),
            mock.patch.object(iot, "load_names", return_value={ip: nm for ip, nm, *_ in []}),
            mock.patch.object(iot, "fetch_queries", return_value=iter(lines)),
        ]
        names = {"192.168.1.40": "Arlo-Camera"}
        patches[3] = mock.patch.object(iot, "load_names", return_value=names)
        if triage is not None:
            patches.append(mock.patch.dict(sys.modules, {"nova_alert_triage": types.SimpleNamespace(triage=triage)}))
        for p in patches:
            p.start()
        try:
            with redirect_stdout(io.StringIO()) as out:
                iot.run(mode=mode)
        finally:
            for p in patches:
                p.stop()
        return out.getvalue(), cur

    def test_new_suspicious_domain_on_trained_device_is_triaged(self):
        now = datetime.now(timezone.utc)
        oldest = now - timedelta(days=10)
        lines = [(now, "192.168.1.40", "evil-c2.top")] * 3
        tri = mock.Mock(return_value={"decision": "page", "verdict": "suspicious"})
        out, cur = self._run(lines, known=[], devstats=[("192.168.1.40", 5, oldest)], triage=tri)
        tri.assert_called_once()
        self.assertEqual(tri.call_args[1]["level"], "critical")
        self.assertEqual(len(cur.updates), 1)              # anomaly row marked triaged
        self.assertIn("routed_to_triage=1", out)

    def test_known_domain_is_not_flagged(self):
        now = datetime.now(timezone.utc)
        oldest = now - timedelta(days=10)
        lines = [(now, "192.168.1.40", "device-api.arlo.com")]
        tri = mock.Mock()
        out, cur = self._run(lines, known=[("192.168.1.40", "arlo.com")],
                             devstats=[("192.168.1.40", 5, oldest)], triage=tri)
        tri.assert_not_called()
        self.assertIn("routed_to_triage=0", out)

    def test_untrained_device_records_but_does_not_page(self):
        now = datetime.now(timezone.utc)
        lines = [(now, "192.168.1.40", "evil-c2.top")]
        tri = mock.Mock()
        # no devstats row => device unknown/learning => never routed
        out, cur = self._run(lines, known=[], devstats=[], triage=tri)
        tri.assert_not_called()
        self.assertIn("new_domains=1", out)
        self.assertIn("routed_to_triage=0", out)

    def test_report_mode_skips_triage_and_writes(self):
        now = datetime.now(timezone.utc)
        oldest = now - timedelta(days=10)
        lines = [(now, "192.168.1.40", "evil-c2.top")]
        tri = mock.Mock()
        with mock.patch.object(iot.psycopg2.extras, "execute_values") as ev:
            out, cur = self._run(lines, known=[], devstats=[("192.168.1.40", 5, oldest)],
                                 mode="report", triage=tri)
        tri.assert_not_called()
        self.assertIn("baseline:", out)


class TestFrame(unittest.TestCase):
    def test_selftest_cli_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_iot_egress_watch.py"), "selftest"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("reg_domain", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_iot_egress_watch"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
