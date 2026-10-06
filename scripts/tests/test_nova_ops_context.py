#!/usr/bin/env python3
"""Tests for nova_ops_context.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2         # noqa: F401
import psycopg2.extras  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ops_context.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("noc", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


oc = _load()


def _fake_pg(answer):
    """Patch psycopg2.connect with a cursor whose fetchall() is answer(sql); records SQL."""
    seen = []

    def connect(*a, **k):
        cur = MagicMock()
        cur.execute.side_effect = lambda sql, params=(): seen.append(sql)
        cur.fetchall.side_effect = lambda: answer(seen[-1])
        conn = MagicMock(); conn.cursor.return_value = cur
        return conn
    return patch("psycopg2.connect", side_effect=connect), seen


def _answers(sql):
    if "FROM host_threat_scores" in sql:
        return [{"host_name": "a", "score": 3}, {"host_name": "b", "score": 9}]
    if "rule_level, COUNT(*)" in sql:
        return [{"rule_level": 12, "cnt": 2}]
    if "auto_response IS NOT NULL" in sql:
        return [{"agent_name": "fw", "rule_description": "brute", "auto_response": "block", "ts": 1}]
    if "FROM security_events" in sql:
        return [{"agent_name": "core", "rule_level": 12, "rule_description": "sudo", "rule_groups": "", "ts": 1},
                {"agent_name": "pi", "rule_level": 3, "rule_description": "login", "rule_groups": "", "ts": 1}]
    if "FROM incidents" in sql:
        return [{"title": "SSH storm", "severity": "high"}]
    if "threat_type, COUNT" in sql:
        return [{"threat_type": "scan", "cnt": 4}]
    if "action IN" in sql:
        return [{"cnt": 17}]
    if "app_name = 'sshd'" in sql:
        return [{"hostname": "core", "cnt": 5}]
    if "FROM scheduler_runs" in sql:
        return [{"total_runs": 10, "success": 9, "failures": 1, "avg_duration": 2.5}]
    if "COUNT(*) as total" in sql:
        return [{"total": 100, "warnings": 7, "errors": 1}]
    if "FROM capacity_snapshots" in sql:
        return [{"device_name": "core", "overall_status": "ok", "cpu_headroom_pct": 50},
                {"device_name": "nas", "overall_status": "warning", "cpu_headroom_pct": 5}]
    if "FROM scheduler_runs" in sql:
        return [{"total_runs": 10, "success": 9, "failures": 1, "avg_duration": 2.5}]
    if "grafana_annotations" in sql:
        return [{"title": "healed redis", "ts": 1, "tags": []}]
    if "shared_observations" in sql:
        return [{"observer": "x", "category": "network", "observation": "y" * 300}]
    return []


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", oc.DB_DSN)

    def test_hours_is_coerced_to_int_before_sql(self):
        # the window is %-interpolated into INTERVAL; a non-numeric value must never reach the SQL
        p, seen = _fake_pg(lambda s: [])
        with p:
            for fn in (oc.get_security_context, oc.get_syslog_context, oc.get_infra_context):
                with self.assertRaises(ValueError):
                    fn("1 hours' UNION SELECT 1 --")
        self.assertEqual(seen, [])

    def test_read_only(self):
        self.assertNotRegex(SRC, r"\b(INSERT INTO|UPDATE\s+\w+\s+SET|DELETE FROM|DROP\s)")


class TestPerformance(unittest.TestCase):
    def test_format_briefs_on_10k_rows_bounded(self):
        ctx = {"security": {"security_events": [{"rule_level": 5, "agent_name": "a", "rule_description": "d"}] * 10_000,
                            "open_incidents": [{"title": "t"}] * 10_000, "auto_responses": [{}] * 10_000},
               "infra": {"hosts": {f"h{i}": {"overall_status": "ok"} for i in range(10_000)},
                         "observations": [{"observation": "o"}] * 10_000}}
        t0 = time.perf_counter()
        sb = oc.format_security_brief(ctx); ib = oc.format_infra_brief(ctx)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(sb.count("  - [L5]"), 15)           # capped
        self.assertEqual(ib.count("  h"), 10_000)


class TestRetry(unittest.TestCase):
    def test_pg_down_fails_open_to_empty(self):
        # RETRY GAP: _pg_query() — one connect, no retry; any error returns [] so article scripts still run
        with patch("psycopg2.connect", side_effect=psycopg2.OperationalError("down")) as c:
            self.assertEqual(oc._pg_query("SELECT 1"), [])
            full = oc.get_full_context(6)
        self.assertGreaterEqual(c.call_count, 1 + 13)
        self.assertEqual(full["security"]["security_event_count"], 0)
        self.assertIsNone(full["security"]["highest_threat_host"])
        self.assertEqual(full["syslog"]["firewall_blocks"], 0)
        self.assertEqual(full["infra"]["degraded_hosts"], [])


class TestUnit(unittest.TestCase):
    def test_security_context_derivations(self):
        p, _ = _fake_pg(_answers)
        with p:
            s = oc.get_security_context(24)
        self.assertEqual(s["security_event_count"], 2)
        self.assertEqual(s["high_severity_count"], 1)
        self.assertEqual(s["threat_scores"], {"a": 3, "b": 9})
        self.assertEqual(s["highest_threat_host"]["host_name"], "b")
        self.assertEqual(s["alert_levels"], {12: 2})

    def test_empty_briefs(self):
        self.assertIn("Security events (last 24h): 0", oc.format_security_brief({}))
        self.assertEqual(oc.format_infra_brief({}), "=== INFRASTRUCTURE STATUS ===")

    def test_query_passes_params_and_closes(self):
        cur = MagicMock(); cur.fetchall.return_value = [{"x": 1}]
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch("psycopg2.connect", return_value=conn):
            self.assertEqual(oc._pg_query("SELECT %s", (5,)), [{"x": 1}])
        cur.execute.assert_called_once_with("SELECT %s", (5,))
        conn.close.assert_called_once()


class TestIntegration(unittest.TestCase):
    def test_window_reaches_every_interval_and_right_tables(self):
        p, seen = _fake_pg(lambda s: [])
        with p:
            oc.get_full_context(7)
        joined = "\n".join(seen)
        self.assertNotIn("'24 hours'", joined)
        self.assertGreaterEqual(joined.count("INTERVAL '7 hours'"), 9)
        for t in ("security_events", "incidents", "host_threat_scores", "syslog_events", "capacity_snapshots",
                  "scheduler_runs", "grafana_annotations", "shared_observations"):
            self.assertIn(f"FROM {t}", joined)

    def test_article_scripts_import_this_helper(self):
        for caller in ("nova_postmortem.py", "nova_journal_security.py"):
            self.assertIn("nova_ops_context", (SCRIPTS / caller).read_text())


class TestFunctional(unittest.TestCase):
    def test_full_context_to_briefs(self):
        p, _ = _fake_pg(_answers)
        with p:
            ctx = oc.get_full_context(12)
        self.assertEqual(ctx["window_hours"], 12)
        sb = oc.format_security_brief(ctx)
        self.assertIn("Security events (last 12h): 2", sb)
        self.assertIn("Firewall blocks: 17", sb)
        self.assertIn("[high] SSH storm", sb)
        self.assertIn("fw: block (brute)", sb)
        ib = oc.format_infra_brief(ctx)
        self.assertIn("DEGRADED HOSTS: nas", ib)
        self.assertIn("Scheduler: 10 runs, 1 failures, avg 2.5s", ib)
        self.assertIn("- healed redis", ib)
        self.assertIn("y" * 100, ib); self.assertNotIn("y" * 101, ib)


class TestFrame(unittest.TestCase):
    def test_import_is_clean_library(self):
        self.assertNotIn("__main__", SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_ops_context as m; assert callable(m.get_full_context)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
