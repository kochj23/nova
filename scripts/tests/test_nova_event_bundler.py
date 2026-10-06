#!/usr/bin/env python3
"""Tests for nova_event_bundler.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_event_bundler.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="event-bundler-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("event_bundler_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("urllib.request.urlopen", side_effect=AssertionError("net at import")):
        spec.loader.exec_module(mod)
    return mod


eb = _load()
NOW = lambda: datetime.now(timezone.utc)  # noqa: E731 — computed at call time, never a module clock


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def _query_by_table(tables):
    """_query stand-in: picks the row list by the FROM table named in the SQL."""
    def q(sql, params=None):
        for name, rows in tables.items():
            if f"FROM {name}" in sql:
                return rows
        return []
    return MagicMock(side_effect=q)


def _ts(hours_ago):
    return NOW() - timedelta(hours=hours_ago)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_read_only(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", SRC))
        self.assertNotIn("password", eb.OPS_DSN)

    def test_window_is_a_bound_parameter_not_interpolated(self):
        self.assertIsNone(re.search(r'_query\(\s*f"', SRC))
        q = _query_by_table({})
        with patch.object(eb, "_query", q):
            eb.bundle_syslog_threats(hours="1; DROP TABLE syslog_events")
        sql, params = q.call_args[0]
        self.assertIn("make_interval(hours => %s)", sql)
        self.assertEqual(params, ("1; DROP TABLE syslog_events",))
        self.assertNotIn("DROP", sql)


class TestPerformance(unittest.TestCase):
    def test_10k_rows_bundle_and_format_fast(self):
        rows = [{"source_ip": f"10.0.{i // 255}.{i % 255}", "threat_type": "scan", "signature": "s", "count": i % 7,
                 "first_seen": _ts(2), "last_seen": _ts(1)} for i in range(10_000)]
        with patch.object(eb, "_query", _query_by_table({"syslog_events": rows})), \
             patch("urllib.request.urlopen", side_effect=OSError("bb down")):
            t0 = time.perf_counter()
            res = eb.bundle_all_events(12)
            txt = eb.format_for_brief(res)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(res["alerts"]) + len(res["detections"]), 10_000)
        self.assertLessEqual(txt.count(":rotating_light:"), 10)
        self.assertLessEqual(txt.count(":information_source:"), 5)


class TestRetry(unittest.TestCase):
    def test_query_fails_open_to_empty(self):
        # RETRY GAP: _query — one psycopg2.connect; any error returns [] so the brief still renders
        with patch.object(eb.psycopg2, "connect", side_effect=RuntimeError("pg down")) as c:
            self.assertEqual(eb._query("SELECT 1"), [])
        self.assertEqual(c.call_count, 1)

    def test_bb_fetch_fails_open_to_empty(self):
        # RETRY GAP: bundle_bb_events — single urlopen to Big Brother; failure yields no bundles
        with patch("urllib.request.urlopen", side_effect=OSError("bb down")) as u:
            self.assertEqual(eb.bundle_bb_events(), [])
        self.assertEqual(u.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_syslog_priority_rules(self):
        rows = [{"source_ip": "1.1.1.1", "threat_type": "scan", "signature": None, "count": 2, "first_seen": None, "last_seen": None},
                {"source_ip": None, "threat_type": "malware", "signature": "x" * 60, "count": 1, "first_seen": _ts(1), "last_seen": _ts(0)},
                {"source_ip": "2.2.2.2", "threat_type": "scan", "signature": "", "count": 5, "first_seen": None, "last_seen": None}]
        with patch.object(eb, "_query", return_value=rows):
            b = eb.bundle_syslog_threats()
        self.assertEqual([x["priority"] for x in b], ["detection", "alert", "alert"])
        self.assertEqual(b[0]["summary"], "2x scan from 1.1.1.1"); self.assertEqual(b[0]["first_seen"], "")
        self.assertEqual(b[1]["source_ip"], "unknown"); self.assertTrue(b[1]["summary"].endswith("(" + "x" * 40 + ")"))

    def test_scheduler_persistent_vs_retried(self):
        rows = [{"task_id": "a", "fail_count": 3, "success_count": 0, "last_attempt": _ts(1)},
                {"task_id": "b", "fail_count": 1, "success_count": 2, "last_attempt": None}]
        with patch.object(eb, "_query", return_value=rows):
            b = eb.bundle_scheduler_failures()
        self.assertEqual(b[0]["priority"], "alert"); self.assertIn("(persistent)", b[0]["summary"])
        self.assertEqual(b[1]["priority"], "detection"); self.assertIn("(retried ok)", b[1]["summary"])
        self.assertEqual(b[1]["last_attempt"], "")

    def test_bb_events_window_severity_and_resolution(self):
        old = (NOW() - timedelta(hours=30)).isoformat().replace("+00:00", "Z")
        new = (NOW() - timedelta(hours=1)).isoformat()
        events = [{"ts": old, "service": "plex", "severity": "critical", "resolved": False},
                  {"ts": new, "service": "plex", "severity": "warning", "resolved": True},
                  {"timestamp": new, "issue": "disk", "severity": "weird", "resolved": False},
                  {"ts": "not-a-date", "service": "plex", "severity": "info", "resolved": False}]
        with patch("urllib.request.urlopen", return_value=_Resp({"events": events})):
            b = {x["service"]: x for x in eb.bundle_bb_events(12)}
        self.assertEqual(b["plex"]["count"], 2)                       # the 30h-old one is cut
        self.assertEqual(b["plex"]["severity"], "warning"); self.assertEqual(b["plex"]["priority"], "alert")
        self.assertEqual(b["disk"]["priority"], "detection")          # unknown severity ranks as info
        with patch("urllib.request.urlopen", return_value=_Resp([])):
            self.assertEqual(eb.bundle_bb_events(), [])

    def test_snmp_and_brief_formatting(self):
        rows = [{"device_name": "sw1", "alert_type": "cpu", "alert_value": 91.26, "threshold": 90.0, "triggered_at": _ts(1)}]
        with patch.object(eb, "_query", return_value=rows):
            b = eb.bundle_snmp_alerts()
        self.assertEqual(b[0]["summary"], "sw1: cpu (91.3 > 90.0)"); self.assertEqual(b[0]["priority"], "alert")
        txt = eb.format_for_brief({"alerts": [], "detections": [{"summary": f"d{i}"} for i in range(7)]})
        self.assertTrue(txt.startswith(":white_check_mark: No alerts requiring action"))
        self.assertIn("_...and 2 more_", txt)


class TestIntegration(unittest.TestCase):
    def test_bundle_all_composes_every_source_and_counts_events(self):
        tables = {"syslog_events": [{"source_ip": "1.1.1.1", "threat_type": "exploit", "signature": "", "count": 4, "first_seen": None, "last_seen": None}],
                  "scheduler_runs": [{"task_id": "t", "fail_count": 1, "success_count": 1, "last_attempt": None}],
                  "snmp_alert_state": [{"device_name": "d", "alert_type": "mem", "alert_value": 95.0, "threshold": 90.0, "triggered_at": None}]}
        with patch.object(eb, "_query", _query_by_table(tables)), patch("urllib.request.urlopen", side_effect=OSError()):
            res = eb.bundle_all_events(6)
        self.assertEqual(res["summary"], "2 alerts, 1 detections (6 total events)")
        self.assertEqual(res["total_events"], 6)
        self.assertEqual({a["type"] for a in res["alerts"]}, {"syslog_threat", "snmp_alert"})
        self.assertEqual(res["detections"][0]["type"], "scheduler_failure")

    def test_query_maps_columns_to_dicts(self):
        cur = MagicMock(); cur.description = [("a",), ("b",)]; cur.fetchall.return_value = [(1, 2)]
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(eb.psycopg2, "connect", return_value=conn):
            self.assertEqual(eb._query("SELECT a, b FROM x WHERE h=%s", (1,)), [{"a": 1, "b": 2}])
        cur.execute.assert_called_once_with("SELECT a, b FROM x WHERE h=%s", (1,))
        conn.close.assert_called_once()


class TestFunctional(unittest.TestCase):
    def test_cli_json_golden_path_is_offline_safe(self):
        boot = ("import sys, unittest.mock as um, psycopg2, urllib.request, runpy; "
                "psycopg2.connect = um.MagicMock(side_effect=OSError('pg down')); "
                "urllib.request.urlopen = um.MagicMock(side_effect=OSError('bb down')); "
                "sys.argv = [sys.argv[1], '--hours', '3', '--json']; runpy.run_path(sys.argv[0], run_name='__main__')")
        r = subprocess.run([sys.executable, "-c", boot, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"alerts": [], "detections": [], "summary": "0 alerts, 0 detections (0 total events)", "total_events": 0})

    def test_brief_text_for_a_real_mix(self):
        tables = {"syslog_events": [{"source_ip": "9.9.9.9", "threat_type": "trojan", "signature": "", "count": 1, "first_seen": None, "last_seen": None}],
                  "scheduler_runs": [{"task_id": "weather", "fail_count": 2, "success_count": 3, "last_attempt": None}]}
        with patch.object(eb, "_query", _query_by_table(tables)), patch("urllib.request.urlopen", side_effect=OSError()):
            txt = eb.format_for_brief(eb.bundle_all_events())
        self.assertIn("*Alerts (needs action):*\n  :rotating_light: 1x trojan from 9.9.9.9", txt)
        self.assertIn("*Detections (1 FYI):*\n  :information_source: weather: 2 failures (retried ok)", txt)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_the_cli(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--hours", r.stdout); self.assertIn("--json", r.stdout)


if __name__ == "__main__":
    unittest.main()
