#!/usr/bin/env python3
"""Tests for nova_component_metrics.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_component_metrics.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("compmetrics", SCRIPTS / "nova_component_metrics.py")
    mod = importlib.util.module_from_spec(spec)
    with patch("logging.basicConfig"):          # never attach a handler to the real log file
        spec.loader.exec_module(mod)
    return mod


cm = _load()
cm.log = MagicMock()


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)
        if self.conn.fail:
            raise RuntimeError("relation missing")

    def fetchone(self):
        return (self.conn.age,)

    def executemany(self, sql, rows):
        self.conn.many.append((sql, list(rows)))


class _Conn:
    def __init__(self, age=30.0, fail=False):
        self.age = age; self.fail = fail; self.sql = []; self.many = []; self.commits = 0; self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(cm.DB_DSN, r"kochj:[^@]+@")

    def test_insert_is_parameterized_and_write_sql_static(self):
        self.assertIn("VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", SRC)
        for c in cm.COMPONENTS:
            sql = c.get("write_sql") or ""
            self.assertNotIn("{", sql)
            self.assertFalse(re.search(r"\b(INSERT|UPDATE|DELETE|DROP)\b", sql, re.I), c["name"])


class TestPerformance(unittest.TestCase):
    def test_find_process_on_10k_snapshot_fast(self):
        snap = [(None, f"/usr/bin/python3 worker_{i}.py", "python3") for i in range(10_000)]
        t0 = time.perf_counter()
        with patch.object(cm, "HAVE_PSUTIL", True):
            for _ in range(20):
                self.assertEqual(cm.find_process("no_such_daemon", snap), (None, None, None))
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_process_table_snapshotted_once_per_cycle(self):
        comps = [{"name": f"p{i}", "proc": f"p{i}"} for i in range(5)]
        with patch.object(cm, "COMPONENTS", comps), \
             patch.object(cm, "snapshot_processes", return_value=[]) as snap, \
             patch.object(cm, "find_process", return_value=(None, None, None)):
            cm.collect(_Conn())
        self.assertEqual(snap.call_count, 1)


class TestRetry(unittest.TestCase):
    def test_http_probe_fails_open_once(self):
        # RETRY GAP: http_probe — single GET per cycle (1-minute scheduler is the retry); errors -> (0, None)
        with patch.object(cm, "HAVE_REQUESTS", True), \
             patch.object(cm.requests, "get", side_effect=OSError("refused")) as g:
            self.assertEqual(cm.http_probe("127.0.0.1", 1, "/health", "uptime_s"), (0, None))
        self.assertEqual(g.call_count, 1)

    def test_failed_health_falls_back_to_tcp(self):
        comps = [{"name": "svc", "host": "192.168.1.250", "port": 1, "health_path": "/h"}]
        with patch.object(cm, "COMPONENTS", comps), patch.object(cm, "snapshot_processes", return_value=None), \
             patch.object(cm, "http_probe", return_value=(0, None)), patch.object(cm, "tcp_probe", return_value=1) as tp:
            rows = cm.collect(_Conn())
        tp.assert_called_once()
        self.assertEqual(rows[0][3], 1)
        self.assertIn("tcp-only", rows[0][9])

    def test_write_age_rolls_back_on_error(self):
        conn = _Conn(fail=True)
        self.assertIsNone(cm.write_age(conn, "SELECT 1"))
        self.assertEqual(conn.rollbacks, 1)


class TestUnit(unittest.TestCase):
    def test_write_age_edges(self):
        self.assertIsNone(cm.write_age(_Conn(), None))
        self.assertIsNone(cm.write_age(_Conn(age=None), "SELECT 1"))
        self.assertEqual(cm.write_age(_Conn(age=12.345), "SELECT 1"), 12.3)

    def test_http_probe_parses_uptime(self):
        resp = SimpleNamespace(status_code=200, text='{"uptime_s": "42.9"}')
        with patch.object(cm, "HAVE_REQUESTS", True), patch.object(cm.requests, "get", return_value=resp):
            self.assertEqual(cm.http_probe("h", 1, "/health", "uptime_s"), (1, 42))
            self.assertEqual(cm.http_probe("h", 1, "/health", None), (1, None))
        resp500 = SimpleNamespace(status_code=503, text="")
        with patch.object(cm, "HAVE_REQUESTS", True), patch.object(cm.requests, "get", return_value=resp500):
            self.assertEqual(cm.http_probe("h", 1, "/health", "uptime_s"), (0, None))

    def test_find_process_edges(self):
        self.assertEqual(cm.find_process(None), (None, None, None))
        self.assertEqual(cm.find_process(""), (None, None, None))

    def test_tcp_probe_down(self):
        with patch.object(cm.socket, "create_connection", side_effect=OSError("no route")):
            self.assertEqual(cm.tcp_probe("h", 1), 0)


class TestIntegration(unittest.TestCase):
    def test_writes_telemetry_nova_components_row_shape(self):
        comps = [{"name": "poller", "proc": "poller.py", "write_sql": "SELECT 1", "stale_sla_s": 60}]
        conn = _Conn(age=10.0)
        with patch.object(cm, "COMPONENTS", comps), patch.object(cm, "snapshot_processes", return_value=[]), \
             patch.object(cm, "find_process", return_value=(50.0, 1.5, time.time() - 100)):
            rows = cm.collect(conn)
        sql, written = conn.many[0]
        self.assertIn("INSERT INTO telemetry.nova_components", sql)
        self.assertEqual(len(written[0]), 10)
        self.assertEqual(rows[0][1:4], ("poller", cm.HOSTNAME, 1))
        self.assertEqual(rows[0][8], 1)


class TestFunctional(unittest.TestCase):
    def test_stale_and_empty_tables_flagged(self):
        comps = [{"name": "stale", "proc": "a", "write_sql": "SELECT 1", "stale_sla_s": 60},
                 {"name": "down", "host": "192.168.1.250", "port": 9, "health_path": None}]
        with patch.object(cm, "COMPONENTS", comps), patch.object(cm, "snapshot_processes", return_value=[]), \
             patch.object(cm, "find_process", return_value=(1.0, 0.0, None)), \
             patch.object(cm, "tcp_probe", return_value=0):
            rows = cm.collect(_Conn(age=999.0))
        self.assertEqual((rows[0][3], rows[0][8]), (1, 0))
        self.assertIn("STALE", rows[0][9])
        self.assertEqual((rows[1][3], rows[1][8]), (0, 0))
        with patch.object(cm, "COMPONENTS", comps[:1]), patch.object(cm, "snapshot_processes", return_value=[]), \
             patch.object(cm, "find_process", return_value=(1.0, 0.0, None)):
            rows = cm.collect(_Conn(age=None))
        self.assertIn("output table empty", rows[0][9])

    def test_main_once_prints_summary_and_closes(self):
        conn = _Conn()
        comps = [{"name": "svc", "host": "192.168.1.250", "port": 9, "health_path": None}]
        buf = io.StringIO()
        with patch.object(cm, "COMPONENTS", comps), patch.object(cm.psycopg2, "connect", return_value=conn), \
             patch.object(cm, "snapshot_processes", return_value=None), patch.object(cm, "tcp_probe", return_value=1), \
             patch.object(sys, "argv", ["x", "--once"]), redirect_stdout(buf):
            cm.main()
        self.assertIn("1 components, 1 up", buf.getvalue())
        self.assertTrue(conn.closed)
        self.assertEqual(len(conn.many), 1)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_with_isolated_home(self):
        with tempfile.TemporaryDirectory() as home:      # LOG_PATH follows HOME -> real logs untouched
            r = subprocess.run([sys.executable, str(SCRIPTS / "nova_component_metrics.py"), "--help"],
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--once", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", "import nova_component_metrics"], cwd=str(SCRIPTS),
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
            log = Path(home) / ".openclaw/logs/component_metrics.log"
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "")
            self.assertFalse(log.exists() and log.read_text())   # no cycle ran, nothing logged


if __name__ == "__main__":
    unittest.main()
