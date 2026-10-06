#!/usr/bin/env python3
"""Tests for nova_service_down_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_service_down_watch.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="service-down-test-"))
_MISSING = object()


@contextlib.contextmanager
def _stub_modules(stubs):
    saved = {k: sys.modules.get(k, _MISSING) for k in stubs}
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load():
    spec = importlib.util.spec_from_file_location("service_down_watch_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    with _stub_modules({"nova_notify": nn}):
        spec.loader.exec_module(mod)
    return mod


sw = _load()


class _Cur:
    """`down` rows answer DOWN_QUERY; `recent` is the set of (service,node) alerted inside REALERT_HOURS."""
    def __init__(self, down, recent=()):
        self.down = down; self.recent = set(recent); self.sql = []; self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        sql = " ".join(sql.split()); self.sql.append((sql, params))
        if sql.startswith("SELECT NOT EXISTS"):
            self._last = (params not in self.recent,)
        elif "WITH latest" in sql:
            self._last = None

    def fetchall(self):
        return self.down

    def fetchone(self):
        return self._last

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False; self.autocommit = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _run(down, recent=()):
    cur = _Cur(down, recent); conn = _Conn(cur)
    sw.notify = MagicMock(return_value=True)
    with patch.object(sw.psycopg2, "connect", return_value=conn), redirect_stdout(io.StringIO()) as out:
        rc = sw.main()
    return rc, cur, conn, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", sw.DSN)

    def test_sql_values_are_parameterized_and_interval_constants_are_ints(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        for c in (sw.DOWN_AFTER_HOURS, sw.REALERT_HOURS, sw.LOOKBACK_DAYS):
            self.assertIsInstance(c, int)        # the only %-formatted values are these module ints
        rc, cur, _, _ = _run([("svc'; DROP TABLE health_checks; --", "node", None, 3.0)])
        for sql, params in cur.sql:
            self.assertNotIn("DROP", sql)
        self.assertEqual(cur.ran("SELECT NOT EXISTS")[0][1], ("svc'; DROP TABLE health_checks; --", "node"))

    def test_error_message_is_truncated_before_it_reaches_the_bus(self):
        rc, cur, _, _ = _run([("ollama", "mac-mini", "e" * 1000, 736.0)])
        body = sw.notify.call_args[1]["body"]
        self.assertLessEqual(len(body.split("Last error: ")[1]), 200)


class TestPerformance(unittest.TestCase):
    def test_10k_down_services_are_processed_fast(self):
        down = [(f"svc{i}", "node", None, 5.0) for i in range(10_000)]
        t0 = time.perf_counter()
        rc, cur, _, out = _run(down)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(sw.notify.call_count, 10_000)
        self.assertIn("alerted=10000", out)


class TestRetry(unittest.TestCase):
    def test_pg_connect_is_one_shot_and_fails_loud(self):
        # RETRY GAP: psycopg2.connect — a single attempt; failure propagates so the scheduler records the run as failed
        with patch.object(sw.psycopg2, "connect", side_effect=RuntimeError("pg down")) as c:
            with self.assertRaises(RuntimeError):
                sw.main()
        self.assertEqual(c.call_count, 1)

    def test_notify_failure_does_not_record_a_phantom_alert(self):
        # RETRY GAP: notify() — not retried; on failure the service_down_alerts row is NOT written, so the
        # next 30-minute run will alert again instead of silently losing the page; conn is still closed.
        cur = _Cur([("ollama", "mac-mini", None, 3.0)]); conn = _Conn(cur)
        sw.notify = MagicMock(side_effect=RuntimeError("bus down"))
        with patch.object(sw.psycopg2, "connect", return_value=conn):
            with self.assertRaises(RuntimeError):
                sw.main()
        self.assertEqual(cur.ran("INSERT INTO telemetry.service_down_alerts"), [])
        self.assertTrue(conn.closed)


class TestUnit(unittest.TestCase):
    def test_queries_encode_the_documented_thresholds(self):
        self.assertIn("interval '2 hours'", sw.DOWN_QUERY)
        self.assertEqual(sw.DOWN_QUERY.count("interval '7 days'"), 4)
        self.assertIn("interval '12 hours'", sw.SHOULD_ALERT)
        self.assertIn("ON CONFLICT (service_name, node_name) DO UPDATE", sw.RECORD_ALERT)
        self.assertIn("CREATE TABLE IF NOT EXISTS telemetry.service_down_alerts", sw.DDL)

    def test_no_down_services_means_no_alerts(self):
        rc, cur, conn, out = _run([])
        self.assertEqual(rc, 0)
        sw.notify.assert_not_called()
        self.assertIn("down_over_2h=0 alerted=0", out)
        self.assertTrue(conn.closed)

    def test_title_and_body_shape(self):
        rc, cur, _, _ = _run([("plex", "nova-core", "connection refused", 4.5)])
        title = sw.notify.call_args[0][0]
        kw = sw.notify.call_args[1]
        self.assertEqual(title, "SERVICE DOWN: plex on nova-core (4.5h)")
        self.assertEqual(kw["body"], "health_checks shows plex@nova-core down for ~4.5h (threshold 2h). Last error: connection refused")
        self.assertEqual(kw["meta"], {"service": "plex", "node": "nova-core", "down_hours": 4.5})


class TestIntegration(unittest.TestCase):
    def test_uses_the_central_bus_with_fleet_category_and_stable_dedup(self):
        self.assertIn("from nova_notify import notify", SRC)
        rc, cur, _, _ = _run([("ollama", "mac-mini", None, 736.0)])
        kw = sw.notify.call_args[1]
        self.assertEqual((kw["level"], kw["category"], kw["dedup_key"]), ("warning", "fleet", "service-down-ollama-mac-mini"))

    def test_ddl_runs_before_the_scan_and_alert_is_recorded_after_notify(self):
        rc, cur, _, _ = _run([("a", "n", None, 3.0)])
        kinds = [s.split()[0] for s, _ in cur.sql]
        self.assertEqual(kinds, ["CREATE", "WITH", "SELECT", "INSERT"])
        self.assertEqual(cur.ran("INSERT INTO telemetry.service_down_alerts")[0][1], ("a", "n"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_alerts_once_per_window(self):
        down = [("ollama", "mac-mini", "timeout", 736.0), ("plex", "nova-core", None, 3.0)]
        rc, cur, conn, out = _run(down, recent={("plex", "nova-core")})
        self.assertEqual(rc, 0)
        self.assertEqual(sw.notify.call_count, 1)
        self.assertEqual(sw.notify.call_args[0][0], "SERVICE DOWN: ollama on mac-mini (736.0h)")
        self.assertEqual([p for _, p in cur.ran("INSERT INTO telemetry.service_down_alerts")], [("ollama", "mac-mini")])
        self.assertIn("down_over_2h=2 alerted=1", out)
        self.assertTrue(conn.closed)

    def test_all_recently_alerted_means_quiet_run(self):
        rc, cur, _, out = _run([("a", "n", None, 3.0)], recent={("a", "n")})
        sw.notify.assert_not_called()
        self.assertIn("alerted=0", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        boot = ("import sys, unittest.mock as um, psycopg2, runpy; sys.modules['nova_notify'] = um.MagicMock(); "
                "psycopg2.connect = um.MagicMock(side_effect=AssertionError('pg at import')); "
                "runpy.run_path(sys.argv[1], run_name='imported'); print('IMPORT_OK')")
        r = subprocess.run([sys.executable, "-c", boot, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "IMPORT_OK")


if __name__ == "__main__":
    unittest.main()
