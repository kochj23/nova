#!/usr/bin/env python3
"""Tests for nova_resolve.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import runpy
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_resolve.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rs = _load("resolve_under_test", SCRIPT)
_PG_DOWN = patch("psycopg2.connect", side_effect=OSError("pg unreachable"))   # safety net: no test may reach PG


def setUpModule():
    _PG_DOWN.start()


def tearDownModule():
    _PG_DOWN.stop()                                        # restored so later test files see the real psycopg2

SERVICE_ROWS = [("ollama", "192.168.1.6", 11434), ("ollama", "192.168.1.125", 11434),
                ("ollama", "192.168.1.5", 11434), ("searxng", "192.168.1.2", 8080)]
NODE_ROWS = [  # node_name, node_ip, cpu_cores, ram_gb, load_1m, mem%, status, age_secs
    ("mac-studio", "192.168.1.6", 32, 512, 30.0, 90.0, "up", 10),       # nearly saturated
    ("ryzen-a", "192.168.1.125", 24, 64, 1.0, 20.0, "up", 15),          # idle
    ("ryzen-b", "192.168.1.5", 24, 64, 2.0, 25.0, "up", 500),           # stale heartbeat -> excluded
    ("nova-core", "192.168.1.2", 8, 32, 0.5, 40.0, "down", 5),          # down -> excluded
    ("ghost", "192.168.1.99", 0, 8, 0.0, 10.0, "up", 5),                # zero cores -> score 0 -> excluded
]


class _Cur:
    def __init__(self, service_rows, node_rows):
        self.answers = [list(service_rows), list(node_rows)]; self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.answers.pop(0)

    def close(self):
        pass


def _conn(cur):
    return types.SimpleNamespace(cursor=lambda: cur, close=lambda: None)


def _reset():
    rs._cache, rs._multi_cache, rs._node_headrooms, rs._rr_counters, rs._cache_ts = {}, {}, {}, {}, 0.0


def _warm(service_rows=SERVICE_ROWS, node_rows=NODE_ROWS):
    """Fill the caches from a fake PG and return the cursor + connect mock."""
    _reset()
    cur = _Cur(service_rows, node_rows)
    with patch("psycopg2.connect", return_value=_conn(cur)) as pc:
        rs._refresh_cache()
    return cur, pc


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", rs._PG_DSN)

    def test_sql_is_parameterized_and_read_only(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", SRC))
        _reset(); rs._cache_ts = time.time()                 # cache fresh -> the only query is resolve_all's own
        cur = _Cur([], [])
        with patch("psycopg2.connect", return_value=_conn(cur)):
            rs.resolve_all("x'; DROP TABLE service_registry; --")
        sql, params = cur.sql[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params, ("x'; DROP TABLE service_registry; --",))

    def test_static_map_is_private_lan_only(self):
        for name, (host, port) in rs._STATIC_MAP.items():
            self.assertTrue(host.startswith(("192.168.", "127.")) or name == "openrouter", name)
            self.assertIsInstance(port, int)


class TestPerformance(unittest.TestCase):
    def test_resolution_and_headroom_fast_on_10k(self):
        _warm()
        rs._cache_ts = time.time()                       # keep the cache fresh for the whole loop
        t0 = time.perf_counter()
        for i in range(10_000):
            rs._compute_headroom(24, 64, i % 30, i % 100)
            rs.resolve("ollama", load_balance=True); rs.resolve_url("gateway", "/health")
        self.assertLess(time.perf_counter() - t0, 1.5)


class TestRetry(unittest.TestCase):
    def test_refresh_cache_fails_open_to_static_map(self):
        # RETRY GAP: _refresh_cache — one psycopg2.connect attempt per TTL; failure keeps the static map
        _reset()
        with patch("psycopg2.connect", side_effect=OSError("pg unreachable")) as pc:
            self.assertEqual(rs.resolve("memory_server"), ("192.168.1.6", 18790))
            self.assertEqual(rs.resolve_url("openrouter", "/v1"), "https://openrouter.ai/v1")
            self.assertEqual(rs.node_headrooms(), {})
            self.assertEqual(rs.list_services(), rs._STATIC_MAP)
        self.assertEqual(pc.call_count, 4)                   # every call retried the connect (ts never advanced)
        self.assertEqual(rs.resolve("nonexistent"), ("127.0.0.1", 0))

    def test_resolve_all_fails_open_to_static_entry(self):
        # RETRY GAP: resolve_all — one attempt; the static entry is returned with status='static'
        _reset()
        with patch("psycopg2.connect", side_effect=OSError("pg unreachable")):
            self.assertEqual(rs.resolve_all("gateway"),
                             [{"host": "192.168.1.2", "port": 18792, "node": "unknown", "status": "static"}])
            self.assertEqual(rs.resolve_all("nope"), [])


class TestUnit(unittest.TestCase):
    def test_compute_headroom_edges(self):
        self.assertEqual(rs._compute_headroom(0, 8, 0, 0), 0.0)
        self.assertEqual(rs._compute_headroom(None, 8, 0, 0), 0.0)
        self.assertEqual(rs._compute_headroom("junk", 8, 0, 0), 0.0)
        self.assertAlmostEqual(rs._compute_headroom(8, 8, None, None), 8.0)
        self.assertAlmostEqual(rs._compute_headroom(8, 8, 16, 50), 0.0)          # overloaded CPU
        self.assertAlmostEqual(rs._compute_headroom(8, 8, 0, 100), 8 * 0.05)     # memory floor at 5%
        self.assertAlmostEqual(rs._compute_headroom(24, 64, 6, 25), 24 * 0.75 * 0.75)

    def test_headroom_for_host_default(self):
        _reset()
        self.assertEqual(rs._headroom_for_host("10.0.0.1"), rs._DEFAULT_HEADROOM)
        rs._node_headrooms = {"10.0.0.1": 3.5}
        self.assertEqual(rs._headroom_for_host("10.0.0.1"), 3.5)

    def test_select_least_load(self):
        _reset()
        self.assertIsNone(rs._select_least_load([]))
        self.assertEqual(rs._select_least_load([("a", 1)]), ("a", 1))
        self.assertIsNone(rs._select_least_load([("a", 1), ("b", 2)]))           # no node data -> RR signal
        rs._node_headrooms = {"a": 0.01, "b": 20.0}
        picks = {rs._select_least_load([("a", 1), ("b", 2)]) for _ in range(50)}
        self.assertEqual(picks, {("b", 2)})                                      # power-of-two always lands on b

    def test_resolve_url_scheme(self):
        _reset()
        with patch.object(rs, "_ensure_fresh", lambda: None):
            self.assertEqual(rs.resolve_url("openrouter"), "https://openrouter.ai")
            self.assertEqual(rs.resolve_url("plex", "/web"), "http://192.168.1.2:32400/web")


class TestIntegration(unittest.TestCase):
    def test_refresh_reads_registry_and_node_status(self):
        cur, pc = _warm()
        self.assertEqual(pc.call_args[0][0], rs._PG_DSN)
        self.assertIn("FROM service_registry WHERE status IN ('up', 'unknown') ORDER BY priority ASC", cur.sql[0][0])
        self.assertIn("FROM node_status", cur.sql[1][0])
        self.assertEqual(rs._cache["ollama"], ("192.168.1.6", 11434))            # first by priority wins
        self.assertEqual(len(rs._multi_cache["ollama"]), 3)
        self.assertEqual(set(rs._node_headrooms), {"mac-studio", "192.168.1.6", "ryzen-a", "192.168.1.125"})
        self.assertGreater(rs._node_headrooms["ryzen-a"], rs._node_headrooms["mac-studio"])
        self.assertGreater(rs._cache_ts, 0)

    def test_load_balance_prefers_idle_node_and_single_instance_short_circuits(self):
        _warm(); rs._cache_ts = time.time()
        picks = [rs.resolve("ollama", load_balance=True) for _ in range(40)]
        self.assertGreater(picks.count(("192.168.1.125", 11434)), picks.count(("192.168.1.6", 11434)))
        self.assertEqual(rs.resolve("searxng", load_balance=True), ("192.168.1.2", 8080))
        self.assertEqual(rs.resolve("ollama"), ("192.168.1.6", 11434))          # non-LB path = priority 1

    def test_round_robin_when_no_node_data(self):
        _warm(node_rows=[]); rs._cache_ts = time.time()
        seq = [rs.resolve("ollama", load_balance=True) for _ in range(6)]
        self.assertEqual(seq[:3], rs._multi_cache["ollama"])
        self.assertEqual(seq[:3], seq[3:])

    def test_ttl_gates_the_refresh(self):
        _warm(); rs._cache_ts = time.time()
        with patch("psycopg2.connect") as pc:
            rs.resolve("ollama"); rs.list_services()
            pc.assert_not_called()
            rs._cache_ts = time.time() - rs._CACHE_TTL - 1
            rs.resolve("ollama")
            pc.assert_called_once()


class TestFunctional(unittest.TestCase):
    def _main(self, argv, connect):
        buf = io.StringIO()
        with patch("psycopg2.connect", connect), patch.object(sys, "argv", ["nova_resolve.py", *argv]), redirect_stdout(buf):
            runpy.run_path(str(SCRIPT), run_name="__main__")
        return buf.getvalue()

    def test_cli_single_service_uses_registry(self):
        out = self._main(["ollama"], MagicMock(return_value=_conn(_Cur(SERVICE_ROWS, NODE_ROWS))))
        self.assertEqual(out.strip(), "ollama -> 192.168.1.6:11434")

    def test_cli_overview_lists_services_headroom_and_lb_smoke(self):
        out = self._main([], MagicMock(return_value=_conn(_Cur(SERVICE_ROWS, NODE_ROWS))))
        self.assertIn("Nova Mesh Service Resolution", out)
        self.assertIn("ollama               -> 192.168.1.6:11434", out)
        self.assertRegex(out, r"192\.168\.1\.125\s+headroom=")
        self.assertNotIn("ryzen-b", out)
        self.assertRegex(out, r"ollama: \{.*\('192\.168\.1\.125', 11434\): \d+")

    def test_cli_with_pg_down_falls_back_to_static(self):
        out = self._main(["gateway"], MagicMock(side_effect=OSError("pg down")))
        self.assertEqual(out.strip(), "gateway -> 192.168.1.2:18792")


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_resolve; assert not nova_resolve._cache"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_import_never_connects(self):
        with patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            m = _load("resolve_frame_probe", SCRIPT)
        self.assertEqual(m._cache_ts, 0.0)


if __name__ == "__main__":
    unittest.main()
