#!/usr/bin/env python3
"""Tests for nova_cert_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Original 7-category classify() checks (pure logic, no DB/Slack) are kept below:
  1. far-off cert (>14d)        -> no alert
  2. within 14d                 -> warning
  3. within 3d                  -> critical
  4. already expired            -> critical + expired flag
  5. NULL days_until_expiry     -> skipped, no crash
  6. boundary values (==14 warn, ==3 crit, ==0 critical+expired)
  7. soonest-first ordering + malformed-row robustness
"""
import datetime
import importlib
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))           # so `python3 tests/test_nova_cert_watch.py` finds the script too
SCRIPT = SCRIPTS / "nova_cert_watch.py"
SRC = SCRIPT.read_text()

m = importlib.import_module("nova_cert_watch")

NOW = datetime.datetime(2026, 9, 8, 12, 0, tzinfo=datetime.timezone.utc)


def _row(ep, days):
    na = None if days is None else NOW + datetime.timedelta(days=days)
    return {"endpoint": ep, "host": f"h-{ep}", "port": 443,
            "subject": f"CN={ep}", "not_after": na, "days_until_expiry": days}


# 1 ──────────────────────────────────────────────────────────────────────────
def test_far_cert_no_alert():
    assert m.classify([_row("far", 90)]) == []


# 2 ──────────────────────────────────────────────────────────────────────────
def test_within_14d_warning():
    a = m.classify([_row("warn", 9.4)])
    assert len(a) == 1 and a[0]["level"] == "warning" and a[0]["expired"] is False


# 3 ──────────────────────────────────────────────────────────────────────────
def test_within_3d_critical():
    a = m.classify([_row("soon", 2)])
    assert a[0]["level"] == "critical" and a[0]["expired"] is False


# 4 ──────────────────────────────────────────────────────────────────────────
def test_expired_critical():
    a = m.classify([_row("dead", -5)])
    assert a[0]["level"] == "critical" and a[0]["expired"] is True


# 5 ──────────────────────────────────────────────────────────────────────────
def test_null_days_skipped():
    assert m.classify([_row("unknown", None)]) == []


# 6 ──────────────────────────────────────────────────────────────────────────
def test_boundaries():
    a = {x["endpoint"]: x for x in m.classify([_row("b14", 14), _row("b3", 3), _row("b0", 0)])}
    assert a["b14"]["level"] == "warning"
    assert a["b3"]["level"] == "critical"
    assert a["b0"]["level"] == "critical" and a["b0"]["expired"] is True


# 7 ──────────────────────────────────────────────────────────────────────────
def test_ordering_and_malformed_rows():
    rows = [_row("mid", 10), _row("worst", -3), _row("near", 1),
            {"endpoint": "junk"},          # no days key -> skipped
            {"days_until_expiry": "NaN"}]  # bad type -> skipped, no raise
    a = m.classify(rows)
    assert [x["endpoint"] for x in a] == ["worst", "near", "mid"]


# ── house categories added 2026-10-05 ───────────────────────────────────────

class _Cur:
    def __init__(self, rows, fail=False):
        self.rows = rows; self.fail = fail; self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("db down")
        self.sql.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False

    def cursor(self, **kw):
        return self.cur

    def close(self):
        self.closed = True


def _notify_stub(result=True, exc=None):
    mod = types.ModuleType("nova_notify")

    def notify(*a, **k):
        calls.append((a, k))
        if exc:
            raise exc
        return result
    calls = []
    mod.notify = notify; mod.calls = calls
    return mod


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", m.DSN)

    def test_read_only_parameterized_sql(self):
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", SRC))
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        cur = _Cur([])
        m.latest_per_endpoint(_Conn(cur), stale_days=7)
        self.assertEqual(cur.sql[0][1], (7,))
        self.assertIn("make_interval(days => %s)", cur.sql[0][0])

    def test_endpoint_names_are_data_in_notify_payloads(self):
        evil = "x'; select 1; --"
        row = m.classify([_row(evil, 1)])[0]
        self.assertIn(evil, m._title(row)); self.assertIn(evil, m._body(row))


class TestPerformance(unittest.TestCase):
    def test_classify_10k_rows_under_bound(self):
        rows = [_row(f"ep{i}", (i % 40) - 5) for i in range(10_000)]
        t0 = time.perf_counter()
        out = m.classify(rows)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(out), sum(1 for i in range(10_000) if (i % 40) - 5 <= 14))
        self.assertLessEqual(out[0]["days_until_expiry"], out[-1]["days_until_expiry"])


class TestRetry(unittest.TestCase):
    def test_connect_failure_fails_open_none(self):
        # RETRY GAP: _connect()/psycopg2.connect — single attempt, returns None (main exits 1; next cron run retries)
        attempts = []
        fake_pg = types.ModuleType("psycopg2")

        def boom(*a, **k):
            attempts.append(k); raise OSError("pg down")
        fake_pg.connect = boom
        with patch.dict(sys.modules, {"psycopg2": fake_pg}), redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(m._connect())
        self.assertEqual(len(attempts), 1); self.assertEqual(attempts[0], {"connect_timeout": 5})
        self.assertIn("DB connect failed", err.getvalue())

    def test_query_failure_fails_open_empty(self):
        # RETRY GAP: latest_per_endpoint() — one query; any error → [] so scan() alerts on nothing rather than crashing
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(m.latest_per_endpoint(_Conn(_Cur([], fail=True))), [])
        self.assertIn("latest_per_endpoint failed", err.getvalue())

    def test_notify_failure_is_swallowed(self):
        # RETRY GAP: _notify()/nova_notify.notify — one attempt, exceptions → False; scan still returns the alerts
        stub = _notify_stub(exc=RuntimeError("bus down"))
        with patch.dict(sys.modules, {"nova_notify": stub}):
            alerts = m.scan(_Conn(_Cur([_row("a", 1)])), emit=True)
        self.assertEqual(len(alerts), 1); self.assertEqual(len(stub.calls), 1)


class TestUnit(unittest.TestCase):
    def test_selftest_passes(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertTrue(m._selftest())
        self.assertNotIn("FAIL", out.getvalue())

    def test_title_and_body_shapes(self):
        rows = {r["endpoint"]: r for r in m.classify([_row("w", 9.44), _row("c", 2.0), _row("e", -1)])}
        self.assertEqual(m._title(rows["w"]), "TLS cert for w expires in 9.4 days")
        self.assertEqual(m._title(rows["c"]), "TLS cert for c expires in 2.0 days — CRITICAL")
        self.assertEqual(m._title(rows["e"]), "TLS cert for e has EXPIRED")
        self.assertIn("Endpoint 'w' (h-w:443) — 9.4 days left.", m._body(rows["w"]))
        self.assertIn("already EXPIRED", m._body(rows["e"]))
        self.assertEqual(m._title({"host": "only-host", "level": "warning", "days_until_expiry": 1.0}), "TLS cert for only-host expires in 1.0 days")

    def test_classify_infinite_and_custom_thresholds(self):
        self.assertEqual(m.classify([{**_row("inf", None), "days_until_expiry": float("inf")}]), [])   # timedelta can't hold inf
        self.assertEqual(m.classify([{**_row("nan", None), "days_until_expiry": float("nan")}]), [])
        a = m.classify([_row("x", 20)], warn_days=30, crit_days=25)
        self.assertEqual(a[0]["level"], "critical")

    def test_report_sorts_and_limits(self):
        rows = [_row("b", 5), _row("a", 1), _row("n", None), _row("c", 9)]
        out = m.report(_Conn(_Cur(rows)), limit=2)
        self.assertEqual([r["endpoint"] for r in out], ["a", "b"])


class TestIntegration(unittest.TestCase):
    def test_reads_cert_expiry_written_by_cert_monitor(self):
        cur = _Cur([])
        m.latest_per_endpoint(_Conn(cur))
        self.assertIn("FROM telemetry.cert_expiry", cur.sql[0][0])
        self.assertIn("SELECT DISTINCT ON (endpoint)", cur.sql[0][0])
        self.assertIn("cert_expiry", (SCRIPTS / "nova_cert_monitor.py").read_text())
        self.assertIn("nova_ops", m.DSN)

    def test_scan_emits_one_deduped_notify_per_alert(self):
        stub = _notify_stub()
        with patch.dict(sys.modules, {"nova_notify": stub}):
            alerts = m.scan(_Conn(_Cur([_row("far", 90), _row("warn", 9), _row("dead", -1)])), emit=True)
        self.assertEqual([a["endpoint"] for a in alerts], ["dead", "warn"])
        self.assertEqual(len(stub.calls), 2)
        args, kw = stub.calls[0]
        self.assertEqual(args[0], "TLS cert for dead has EXPIRED")
        self.assertEqual((kw["level"], kw["category"], kw["dedup_key"], kw["source"]),
                         ("critical", "cert-expiry", "cert-expiry-dead", "nova_cert_watch.py"))
        self.assertTrue(kw["meta"]["expired"])

    def test_dry_scan_emits_nothing(self):
        stub = _notify_stub()
        with patch.dict(sys.modules, {"nova_notify": stub}):
            alerts = m.scan(_Conn(_Cur([_row("warn", 9)])), emit=False)
        self.assertEqual(len(alerts), 1); self.assertEqual(stub.calls, [])


class TestFunctional(unittest.TestCase):
    def _main(self, argv, rows, stub=None):
        conn = _Conn(_Cur(rows))
        stub = stub or _notify_stub()
        with patch.object(m, "_connect", return_value=conn), patch.dict(sys.modules, {"nova_notify": stub}), \
             patch.object(sys, "argv", ["nova_cert_watch.py"] + argv), redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as cm:
                m.main()
        return cm.exception.code, out.getvalue(), conn, stub

    def test_scan_golden_path_alerts_and_closes(self):
        rc, out, conn, stub = self._main(["--scan"], [_row("warn", 9), _row("far", 60)])
        self.assertEqual(rc, 0); self.assertTrue(conn.closed)
        self.assertIn("alerted on 1 expiring cert(s):", out); self.assertIn("[warning] warn: 9.0d", out)
        self.assertEqual(len(stub.calls), 1)

    def test_dry_run_prints_would_alert_and_report_mode(self):
        rc, out, _, stub = self._main(["--dry-run"], [_row("dead", -2)])
        self.assertEqual(rc, 0); self.assertIn("WOULD alert on 1", out); self.assertEqual(stub.calls, [])
        rc, out, _, _ = self._main(["--report"], [_row("b", 5), _row("a", 1)])
        self.assertEqual(rc, 0); self.assertLess(out.index("a "), out.index("b "))
        rc, out, _, _ = self._main(["--scan"], [])
        self.assertIn("no certs expiring within 14 days", out)

    def test_db_down_exits_1(self):
        with patch.object(m, "_connect", return_value=None), patch.object(sys, "argv", ["nova_cert_watch.py", "--scan"]):
            with self.assertRaises(SystemExit) as cm:
                m.main()
        self.assertEqual(cm.exception.code, 1)


class TestFrame(unittest.TestCase):
    def test_selftest_and_help_exit_zero(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertNotIn("FAIL", r.stdout)
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0); self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_cert_watch"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
