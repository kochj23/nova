#!/usr/bin/env python3
"""Tests for nova_analytics_aggregate.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with patch("psycopg2.connect", side_effect=OSError("offline test")):   # nova_config's registry lookup
    aa = _load("analytics_agg_t", SCRIPTS / "nova_analytics_aggregate.py")
SRC = (SCRIPTS / "nova_analytics_aggregate.py").read_text()
aa.notify = MagicMock()   # never page from a test
aa.log = MagicMock()
aa.psycopg2 = MagicMock(extras=MagicMock(RealDictCursor=object))
aa.psycopg2.connect.side_effect = RuntimeError("psycopg2.connect not mocked in test")


class _Cur:
    """Routes queries by SQL substring to canned answers; records every execute."""
    def __init__(self, routes=None, one=None):
        self.routes = routes or {}; self.one = one or {}; self.sql = []; self.params = []; self._last = ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _pick(self, table):
        for k, v in table.items():
            if k in self._last:
                return v
        return None

    def fetchall(self):
        return self._pick(self.routes) or []

    def fetchone(self):
        return self._pick(self.one)

    def close(self):
        pass


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.commits = 0; self.closed = False

    def cursor(self, cursor_factory=None):
        return self.cur

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


HS = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
PV = "COUNT(DISTINCT visitor_hash)"


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password=", PG_DSN := aa.PG_DSN)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        self.assertNotRegex(SRC, r'execute\([^)]*%\s*\(')


class TestPerformance(unittest.TestCase):
    def test_json_default_10k(self):
        t0 = time.perf_counter()
        s = json.dumps([{"v": Decimal(i) / 3} for i in range(10_000)], default=aa._json_default)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertTrue(s.startswith("[{"))


class TestRetry(unittest.TestCase):
    def test_probe_falls_back_head_to_get(self):
        resp = MagicMock(status=200); resp.__enter__ = lambda s: s; resp.__exit__ = lambda *a: False
        uo = MagicMock(side_effect=[ConnectionResetError("no HEAD"), resp])
        with patch("urllib.request.urlopen", uo):
            self.assertTrue(aa._probe_site("x.example"))
        self.assertEqual([c.args[0].get_method() for c in uo.call_args_list], ["HEAD", "GET"])

    def test_probe_fails_open_to_unreachable(self):
        # RETRY GAP: _probe_site — two attempts (HEAD, GET), no backoff; total failure returns False, never raises
        uo = MagicMock(side_effect=OSError("down"))
        with patch("urllib.request.urlopen", uo):
            self.assertFalse(aa._probe_site("x.example"))
        self.assertEqual(uo.call_count, 2)


class TestUnit(unittest.TestCase):
    def test_probe_http_error_status(self):
        e404 = urllib.error.HTTPError("u", 404, "nf", {}, None)
        e503 = urllib.error.HTTPError("u", 503, "x", {}, None)
        with patch("urllib.request.urlopen", side_effect=e404):
            self.assertTrue(aa._probe_site("a"))
        with patch("urllib.request.urlopen", side_effect=e503):
            self.assertFalse(aa._probe_site("a"))

    def test_json_default_rejects_unknown(self):
        self.assertEqual(aa._json_default(Decimal("1.5")), 1.5)
        with self.assertRaises(TypeError):
            aa._json_default(object())

    def test_aggregate_empty_hour(self):
        conn = _Conn(_Cur())
        self.assertEqual(aa.aggregate_hour(conn, HS, HS + timedelta(hours=1)), {})
        self.assertEqual(conn.commits, 0)

    def test_fire_alerts_noop_on_empty(self):
        conn = _Conn(_Cur())
        aa.fire_alerts(conn, [])
        self.assertEqual(conn.cur.sql, [])


class TestIntegration(unittest.TestCase):
    def test_aggregate_feeds_anomaly_spike(self):
        cur = _Cur(routes={"PARTITION BY site": [], PV: [{"site": "s", "path": "/", "views": 500, "unique_visitors": 3, "avg_response_ms": 12.7}],
                           "referrer_domain, COUNT": [{"site": "s", "referrer_domain": "r.com", "cnt": 4}]},
                   one={"AVG(views)": {"avg_views": Decimal("10")}})
        conn = _Conn(cur)
        sv = aa.aggregate_hour(conn, HS, HS + timedelta(hours=1))
        self.assertEqual(sv, {"s": 500})
        ins = [p for q, p in zip(cur.sql, cur.params) if "INSERT INTO analytics_hourly" in q][0]
        self.assertEqual(ins[7], 12)
        self.assertEqual(json.loads(ins[8]), [{"domain": "r.com", "count": 4}])
        alerts = aa.check_anomalies(conn, HS.replace(hour=3), sv)   # off-hours: skip dark check
        self.assertEqual([a["type"] for a in alerts], ["traffic_spike"])
        self.assertEqual(alerts[0]["detail"]["multiplier"], 50.0)


class TestFunctional(unittest.TestCase):
    def setUp(self):
        aa.notify.reset_mock()

    def test_unreachable_site_pages_critical_once(self):
        cur = _Cur(one={})
        aa.fire_alerts(_Conn(cur), [{"type": "site_dark", "site": "x", "detail": {"site_reachable": False, "hours_silent": 30}}])
        self.assertEqual(aa.notify.call_args.kwargs["level"], "critical")
        self.assertIn("UNREACHABLE", aa.notify.call_args.args[0])
        self.assertTrue(any("INSERT INTO analytics_alerts" in q for q in cur.sql))

    def test_unreachable_in_cooldown_is_suppressed_and_quiet_is_info(self):
        cur = _Cur(one={"SELECT ts FROM analytics_alerts": (1,)})
        aa.fire_alerts(_Conn(cur), [{"type": "site_dark", "site": "x", "detail": {"site_reachable": False}}])
        aa.notify.assert_not_called()
        aa.fire_alerts(_Conn(_Cur()), [{"type": "site_dark", "site": "y", "detail": {"site_reachable": True}}])
        self.assertEqual(aa.notify.call_args.kwargs["level"], "info")

    def test_run_golden_path_and_retention(self):
        cur = _Cur()
        conn = _Conn(cur)
        with patch.object(aa, "get_conn", return_value=conn):
            aa.run()
        self.assertTrue(any("DELETE FROM analytics_hourly" in q for q in cur.sql))
        self.assertTrue(conn.closed)
        aa.notify.assert_not_called()

    def test_run_error_path_db_down(self):
        with patch.object(aa, "get_conn", side_effect=RuntimeError("pg down")):
            with self.assertRaises(RuntimeError):
                aa.run()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import importlib.util,sys;sys.path.insert(0,'.');import psycopg2;"
                "psycopg2.connect=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
                "s=importlib.util.spec_from_file_location('m','nova_analytics_aggregate.py');"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print('ok')")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
