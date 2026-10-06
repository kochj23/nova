#!/usr/bin/env python3
"""Tests for nova_mesh_agent.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_mesh_agent.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="mesh_test_"))


def _load():
    spec = importlib.util.spec_from_file_location("mesh_agent", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ma = _load()


class _Cur:
    def __init__(self, rows=()):
        self.rows = list(rows); self.sql = []
    def execute(self, sql, params=None): self.sql.append((sql, params))
    def fetchall(self): return self.rows
    def close(self): pass


def _conn(cur):
    c = MagicMock(); c.cursor.return_value = cur
    return c


def _cfg(**kw):
    return patch.dict(ma.config, {**ma.DEFAULT_CONFIG, **kw})


def _get(path):
    h = ma.MeshHandler.__new__(ma.MeshHandler)
    h.path = path; h.wfile = io.BytesIO(); h.status = None
    h.send_response = lambda c: setattr(h, "status", c); h.send_header = lambda *a: None; h.end_headers = lambda: None
    h.do_GET()
    return h.status, json.loads(h.wfile.getvalue())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password=", ma.DEFAULT_CONFIG["pg_dsn"])

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))
        cur = _Cur(rows=[("svc'); --injected", "n1", "10.0.0.1", 80, None, 0)])
        with _cfg(node_name="mac-studio"), patch("psycopg2.connect", return_value=_conn(cur)), \
             patch.object(ma, "probe_registry_service", return_value=(True, 3)), redirect_stdout(io.StringIO()):
            ma.reconcile_registry_health()
        for sql, params in cur.sql:
            self.assertNotIn("--injected", sql)
        self.assertEqual(cur.sql[1][1], ("svc'); --injected", "n1"))

    def test_only_the_authority_writes_registry_status(self):
        with _cfg(node_name="worker-1"), patch("psycopg2.connect") as pc:
            ma.reconcile_registry_health()
        pc.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_reconcile_10k_rows_fast(self):
        rows = [(f"s{i}", "n", "10.0.0.1", 80, None, 120) for i in range(10_000)]
        cur = _Cur(rows)
        with _cfg(node_name="mac-studio"), patch("psycopg2.connect", return_value=_conn(cur)), \
             patch.object(ma, "probe_registry_service", side_effect=lambda h, p, u, l: (True, 1)), \
             patch.object(ma, "_local_ips", return_value=set()), redirect_stdout(io.StringIO()) as out:
            t0 = time.perf_counter()
            ma.reconcile_registry_health()
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertIn("10000 up, 0 down", out.getvalue())


class TestRetry(unittest.TestCase):
    def test_probe_falls_back_to_loopback_for_local_service(self):
        tried = []
        with patch.object(ma, "_tcp_ok", side_effect=lambda h, p: tried.append(h) or h == "127.0.0.1"):
            up, _ = ma.probe_registry_service("192.168.1.6", 37461, None, {"192.168.1.6"})
        self.assertTrue(up)
        self.assertEqual(tried, ["192.168.1.6", "127.0.0.1"])

    def test_heartbeat_pg_failure_fails_open(self):
        # RETRY GAP: heartbeat_to_pg/psycopg2.connect — one attempt per cycle, error printed, loop continues
        with patch("psycopg2.connect", side_effect=OSError("pg down")) as pc, redirect_stderr(io.StringIO()) as err:
            ma.heartbeat_to_pg()
        self.assertEqual(pc.call_count, 1)
        self.assertIn("PG heartbeat failed", err.getvalue())

    def test_one_bad_probe_does_not_stop_the_pass(self):
        cur = _Cur([("a", "n", "h", 1, None, 0), ("b", "n", "h", 2, None, 0)])
        with _cfg(node_name="mac-studio"), patch("psycopg2.connect", return_value=_conn(cur)), \
             patch.object(ma, "probe_registry_service", side_effect=[RuntimeError("boom"), (True, 1)]), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            ma.reconcile_registry_health()
        self.assertEqual([p for s, p in cur.sql if "UPDATE" in s], [("b", "n")])


class TestUnit(unittest.TestCase):
    def test_transient_miss_is_degraded_sustained_is_down(self):
        cur = _Cur([("a", "n", "h", 1, None, 10), ("b", "n", "h", 2, None, 600), ("c", "n", "h", 3, None, None)])
        with _cfg(node_name="mac-studio"), patch("psycopg2.connect", return_value=_conn(cur)), \
             patch.object(ma, "probe_registry_service", return_value=(False, 1)), redirect_stdout(io.StringIO()):
            ma.reconcile_registry_health()
        statuses = [p[0] for s, p in cur.sql if "SET status = %s" in s]
        self.assertEqual(statuses, ["degraded", "down", "down"])

    def test_http_ok_semantics(self):
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with patch.object(ma.urllib.request, "urlopen", side_effect=err):
            self.assertTrue(ma._http_ok("http://x/y"))          # answered -> alive
        with patch.object(ma.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("u", 503, "x", {}, None)):
            self.assertFalse(ma._http_ok("http://x/y"))
        with patch.object(ma.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.assertFalse(ma._http_ok("https://x/y"))

    def test_absolute_health_url_host_rewritten(self):
        seen = []
        with patch.object(ma, "_http_ok", side_effect=lambda u: seen.append(u) or False):
            ma.probe_registry_service("10.0.0.5", 9200, "https://wazuh:55000/health", {"10.0.0.5"})
        self.assertEqual(seen, ["https://10.0.0.5:55000/health", "https://127.0.0.1:55000/health"])

    def test_config_fallback_parser(self):
        p = TMP / "mesh.yaml"
        p.write_text("node_name: nuk-1\nheartbeat_interval: 30\n# comment\n- ignored\nunknown: x\n")
        real_import = __import__

        def no_yaml(name, *a, **k):
            if name == "yaml":
                raise ImportError
            return real_import(name, *a, **k)
        with patch.object(ma, "CONFIG_PATHS", [p]), patch("builtins.__import__", no_yaml), _cfg():
            ma.load_config()
            self.assertEqual((ma.config["node_name"], ma.config["heartbeat_interval"]), ("nuk-1", 30))
            self.assertNotIn("unknown", ma.config)


class TestIntegration(unittest.TestCase):
    def test_heartbeat_writes_node_status_and_flags_peer(self):
        cur = _Cur()
        with _cfg(node_name="n1", peer="10.0.0.9", peer_node_name="n2"), patch("psycopg2.connect", return_value=_conn(cur)), \
             patch.object(ma, "check_peer", return_value={"status": "unreachable"}):
            ma.heartbeat_to_pg()
        self.assertIn("node_status SET", cur.sql[0][0])
        self.assertEqual(cur.sql[0][1][-1], "n1")
        self.assertEqual(cur.sql[1][1], ("n2",))
        self.assertFalse(any("service_registry" in s for s, _ in cur.sql))

    def test_check_all_services_and_endpoints(self):
        with _cfg(services=[{"name": "redis", "port": 1}, "junk"]), \
             patch.object(ma, "check_service", return_value={"name": "redis", "status": "up", "latency_ms": 1}):
            ma.check_all_services()
            self.assertEqual(_get("/services")[1]["services"]["redis"]["status"], "up")
            self.assertEqual(_get("/health")[1]["status"], "ok")
            self.assertEqual(_get("/x")[0], 404)


class TestFunctional(unittest.TestCase):
    def test_main_wires_thread_and_server_without_binding(self):
        server = MagicMock()
        with patch.object(ma.signal, "signal") as sig, patch.object(ma, "load_config"), \
             patch.object(ma.threading, "Thread") as th, patch.object(ma, "HTTPServer", return_value=server) as hs, \
             redirect_stdout(io.StringIO()):
            ma.main()
        self.assertEqual(sig.call_count, 3)
        th.assert_called_once_with(target=ma.heartbeat_loop, daemon=True)
        self.assertEqual(hs.call_args.args[0], ("0.0.0.0", ma.config["port"]))
        server.serve_forever.assert_called_once(); server.server_close.assert_called_once()

    def test_collect_metrics_linux_path(self):
        st = MagicMock(f_blocks=100, f_bfree=25)
        meminfo = io.StringIO("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
        with patch.object(ma.platform, "system", return_value="Linux"), patch("builtins.open", return_value=meminfo), \
             patch.object(ma.os, "statvfs", return_value=st), patch.object(ma.os, "getloadavg", return_value=(1.234, 0, 0)):
            ma.collect_metrics()
        self.assertEqual(ma.node_metrics, {"cpu_load_1m": 1.23, "memory_percent": 75.0, "disk_percent": 75.0})


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_mesh_agent as m; print(m.PORT)"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "37470")


if __name__ == "__main__":
    unittest.main()
