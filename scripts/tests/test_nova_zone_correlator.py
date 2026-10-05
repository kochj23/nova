#!/usr/bin/env python3
"""Tests for nova_zone_correlator.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_zone_correlator.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


zc = _load("zc", SCRIPT)
SRC = SCRIPT.read_text()
_TMP = tempfile.TemporaryDirectory()
zc.LOG_FILE = Path(_TMP.name) / "correlator.log"      # never append to the real ~/.openclaw/logs during tests


class _Cur:
    """Cursor stub for _db_query: dict rows come from `description` + fetchall, keyed by SQL substring."""
    def __init__(self, routes):
        self.routes = routes; self.sql = []; self.params = []; self.description = None; self._rows = []

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params)
        for needle, rows in self.routes:
            if needle in sql:
                if isinstance(rows, Exception):
                    raise rows
                self._rows = rows
                self.description = [(k,) for k in rows[0]] if rows else [("x",)]
                return
        self._rows = []; self.description = None

    def fetchall(self):
        return [tuple(r.values()) for r in self._rows]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.closed = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


class _Redis:
    def __init__(self, up=True):
        self.up = up; self.xadds = []

    def ping(self):
        if not self.up:
            raise ConnectionError("redis down")
        return True

    def xadd(self, stream, fields, maxlen=None):
        self.xadds.append((stream, json.loads(fields["data"]), maxlen))


def _routes(threats=(), spikes=(), cpu=(), mem=()):
    return [("FROM syslog_events", list(threats)), ("HAVING AVG(metric_value) > 8.0", list(spikes)),
            ("HAVING AVG(metric_value) > 6.0", list(cpu)), ("metric_name = 'mem_avail_real'", list(mem))]


class _World:
    """Everything external at once: PG, Redis, the PG port probe and the Slack/Discord post."""
    def __init__(self, cur, pg_port_up=True, redis_up=True):
        self.cur = cur; self.conn = _Conn(cur); self.redis = _Redis(redis_up); self.posts = []
        sock = mock.Mock(); sock.connect_ex.return_value = 0 if pg_port_up else 61
        self._ctx = [mock.patch.object(zc.psycopg2, "connect", return_value=self.conn),
                     mock.patch.object(zc.redis, "from_url", return_value=self.redis),
                     mock.patch("socket.socket", return_value=sock),
                     mock.patch.object(zc.nova_config, "post_both", side_effect=lambda m, **k: self.posts.append((m, k)))]

    def __enter__(self):
        for c in self._ctx:
            c.start()
        return self

    def __exit__(self, *exc):
        for c in reversed(self._ctx):
            c.stop()
        return False


THREAT = {"source_ip": "192.168.1.50", "threat_type": "c2_suspect", "count": 3}
SPIKE = {"device_ip": "192.168.1.50", "avg_cpu": 9.5}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn(":@", zc.OPS_DSN); self.assertNotIn("password", zc.OPS_DSN)

    def test_sql_is_static_and_parameterized(self):
        self.assertIsNone(re.search(r'_db_query\(\s*f["\']', SRC))
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        cur = _Cur(_routes())
        with mock.patch.object(zc.psycopg2, "connect", return_value=_Conn(cur)):
            zc._db_query("SELECT 1 WHERE x = %s", ("a'; DROP TABLE syslog_events; --",))
        self.assertEqual(cur.params[0], ("a'; DROP TABLE syslog_events; --",))
        self.assertNotIn("DROP", cur.sql[0])

    def test_read_only_over_pg(self):
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM|DROP|TRUNCATE)\b", SRC))

    def test_hostile_threat_fields_stay_inert_in_the_alert(self):
        evil = {**THREAT, "threat_type": "*}{`whoami`:rotating_light:", "source_ip": "192.168.1.50"}
        cur = _Cur(_routes(threats=[evil], spikes=[SPIKE]))
        with _World(cur) as w, redirect_stdout(io.StringIO()):
            found = zc.run_correlation()
        self.assertEqual(found[0]["details"], "3x *}{`whoami`:rotating_light: from 192.168.1.50 + CPU spike on same device")
        self.assertIn("`whoami`", w.posts[0][0])          # carried as text, never executed


class TestPerformance(unittest.TestCase):
    def test_security_zone_fast_on_10k_threats(self):
        threats = [{"source_ip": f"10.0.{i // 250}.{i % 250}", "threat_type": "ips", "count": 1} for i in range(10_000)]
        spikes = [{"device_ip": f"10.0.{i // 250}.{i % 250}", "avg_cpu": 9.0} for i in range(0, 10_000, 2)]
        cur = _Cur(_routes(threats=threats, spikes=spikes))
        t0 = time.perf_counter()
        with mock.patch.object(zc.psycopg2, "connect", return_value=_Conn(cur)):
            found = zc.correlate_security_zone()
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(found), 5000)


class TestRetry(unittest.TestCase):
    # RETRY GAP: _db_query() — one psycopg2.connect per query, no backoff; fails open to [] and logs.
    def test_db_query_fails_open(self):
        calls = []

        def boom(*a, **k):
            calls.append(1); raise OSError("pg down")
        with mock.patch.object(zc.psycopg2, "connect", boom), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(zc._db_query("SELECT 1"), [])
            self.assertEqual(zc.correlate_security_zone(), [])
            self.assertEqual(zc.correlate_health_zone(), [])
        self.assertEqual(len(calls), 5)
        self.assertIn("[ERROR] DB error: pg down", out.getvalue())

    # RETRY GAP: correlate_infrastructure_zone() — one port probe + one redis ping; a failure is the finding, not an exception.
    def test_infrastructure_probe_failures_become_findings(self):
        with _World(_Cur(_routes()), pg_port_up=False, redis_up=False), redirect_stdout(io.StringIO()):
            found = zc.correlate_infrastructure_zone()
        self.assertEqual([c["root_cause"] for c in found], ["postgresql", "redis"])
        with mock.patch("socket.socket", side_effect=OSError("no sockets")), \
             mock.patch.object(zc.redis, "from_url", side_effect=OSError("no redis lib")), redirect_stdout(io.StringIO()):
            found = zc.correlate_infrastructure_zone()
        self.assertEqual(len(found), 2)

    def test_log_file_errors_are_swallowed(self):
        with mock.patch.object(zc, "LOG_FILE", Path("/dev/null/not-a-dir/x.log")), redirect_stdout(io.StringIO()) as out:
            zc.log("hello", "WARN")
        self.assertIn("[WARN] hello", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_db_query_maps_rows_to_dicts_and_closes(self):
        cur = _Cur([("SELECT a", [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])])
        conn = _Conn(cur)
        with mock.patch.object(zc.psycopg2, "connect", return_value=conn):
            rows = zc._db_query("SELECT a, b")
            self.assertEqual(zc._db_query("SELECT nothing"), [])
        self.assertEqual(rows, [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])
        self.assertTrue(conn.closed)

    def test_security_zone_requires_same_device(self):
        cur = _Cur(_routes(threats=[THREAT, {**THREAT, "source_ip": "192.168.1.51"}, {**THREAT, "source_ip": None}],
                           spikes=[SPIKE]))
        with mock.patch.object(zc.psycopg2, "connect", return_value=_Conn(cur)):
            found = zc.correlate_security_zone()
        self.assertEqual(len(found), 1)
        c = found[0]
        self.assertEqual((c["zone"], c["type"], c["severity"], c["device_ip"], c["sources"]),
                         ("security", "threat_with_cpu_spike", "critical", "192.168.1.50", ["syslog", "snmp"]))
        self.assertIn("3x c2_suspect from 192.168.1.50", c["details"])
        self.assertRegex(c["timestamp"], r"^\d{4}-\d{2}-\d{2}T")

    def test_health_zone_needs_cpu_and_memory_on_the_same_device(self):
        cpu = [{"device_name": "nas", "device_ip": "192.168.1.9", "avg_load": 7.26},
               {"device_name": "mini", "device_ip": "192.168.1.77", "avg_load": 6.5}]
        mem = [{"device_name": "nas", "device_ip": "192.168.1.9"}]
        cur = _Cur(_routes(cpu=cpu, mem=mem))
        with mock.patch.object(zc.psycopg2, "connect", return_value=_Conn(cur)):
            found = zc.correlate_health_zone()
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["type"], found[0]["severity"], found[0]["device"]), ("resource_exhaustion", "warning", "nas"))
        self.assertEqual(found[0]["details"], "nas: high CPU (7.3) + low memory")

    def test_infrastructure_zone_all_up_is_empty(self):
        with _World(_Cur(_routes())) as w:
            self.assertEqual(zc.correlate_infrastructure_zone(), [])
        self.assertEqual(w.posts, [])

    def test_empty_inputs(self):
        cur = _Cur(_routes())
        with mock.patch.object(zc.psycopg2, "connect", return_value=_Conn(cur)):
            self.assertEqual(zc.correlate_security_zone(), [])
            self.assertEqual(zc.correlate_health_zone(), [])


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_are_imported_not_reimplemented(self):
        import nova_config
        self.assertIs(zc.nova_config, nova_config)
        self.assertNotIn("def post_both", SRC)
        self.assertIn("nova_config.SLACK_BB", SRC)
        self.assertEqual(zc.CORRELATED_STREAM, "nova:correlated:events")

    def test_tables_match_the_syslog_and_snmp_producers(self):
        syslog_src = (SCRIPTS / "nova_syslog_server.py").read_text()
        self.assertIn("INSERT INTO syslog_events", syslog_src)
        for col in ("threat_type", "src_addr"):
            self.assertIn(col, syslog_src)
        self.assertIn("FROM syslog_events", SRC); self.assertIn("FROM snmp_metrics", SRC)

    def test_run_chains_zones_into_one_stream(self):
        cur = _Cur(_routes(threats=[THREAT], spikes=[SPIKE],
                           cpu=[{"device_name": "nas", "device_ip": "192.168.1.9", "avg_load": 7.0}],
                           mem=[{"device_name": "nas", "device_ip": "192.168.1.9"}]))
        with _World(cur, redis_up=True) as w, redirect_stdout(io.StringIO()) as out:
            found = zc.run_correlation()
        self.assertEqual([c["zone"] for c in found], ["security", "health"])
        self.assertEqual([s for s, _, _ in w.redis.xadds], [zc.CORRELATED_STREAM] * 2)
        self.assertEqual([m for _, _, m in w.redis.xadds], [1000, 1000])
        self.assertEqual(w.redis.xadds[0][1]["type"], "threat_with_cpu_spike")
        self.assertIn("Found 2 correlations (1 critical)", out.getvalue())


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_and_alerts_on_critical(self):
        cur = _Cur(_routes(threats=[THREAT], spikes=[SPIKE]))
        with _World(cur, pg_port_up=False) as w, redirect_stdout(io.StringIO()):
            found = zc.run_correlation()
        self.assertEqual([c["type"] for c in found], ["threat_with_cpu_spike", "cascade_root_cause"])
        self.assertEqual(len(w.redis.xadds), 2)
        self.assertEqual(len(w.posts), 1)
        msg, kw = w.posts[0]
        self.assertTrue(msg.startswith(":link: *Cross-Source Correlation Alert*"))
        self.assertIn("[security] 3x c2_suspect from 192.168.1.50", msg)
        self.assertIn("[infrastructure] PostgreSQL DOWN", msg)
        self.assertEqual(kw["slack_channel"], zc.nova_config.SLACK_BB)
        self.assertTrue(zc.LOG_FILE.exists() and "Found 2 correlations (2 critical)" in zc.LOG_FILE.read_text())

    def test_warning_only_findings_publish_without_paging(self):
        cur = _Cur(_routes(cpu=[{"device_name": "nas", "device_ip": "192.168.1.9", "avg_load": 7.0}],
                           mem=[{"device_name": "nas", "device_ip": "192.168.1.9"}]))
        with _World(cur) as w, redirect_stdout(io.StringIO()):
            found = zc.run_correlation()
        self.assertEqual([c["severity"] for c in found], ["warning"])
        self.assertEqual(len(w.redis.xadds), 1)
        self.assertEqual(w.posts, [])

    def test_error_path_pg_down_everywhere_still_reports_the_root_cause(self):
        with _World(_Cur(_routes()), pg_port_up=False) as w, \
             mock.patch.object(zc.psycopg2, "connect", side_effect=OSError("pg down")), redirect_stdout(io.StringIO()):
            found = zc.run_correlation()
        self.assertEqual([c["root_cause"] for c in found], ["postgresql"])
        self.assertEqual(len(w.posts), 1)
        self.assertIn("PostgreSQL DOWN", w.posts[0][0])

    def test_nothing_found_publishes_nothing(self):
        with _World(_Cur(_routes())) as w, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(zc.run_correlation(), [])
        self.assertEqual(w.redis.xadds, [])
        self.assertEqual(w.posts, [])
        self.assertNotIn("Found", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_smoke_exits_zero(self):
        # no --help/--selftest: the module's only entry is the __main__ guard; import must be side-effect free
        r = subprocess.run([sys.executable, "-c", "import nova_zone_correlator as z; assert callable(z.run_correlation)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_the_correlation(self):
        self.assertIn('if __name__ == "__main__":\n    results = run_correlation()', SRC)
        with mock.patch.object(zc.psycopg2, "connect", side_effect=AssertionError("ran at import")), \
             mock.patch.object(zc.redis, "from_url", side_effect=AssertionError("ran at import")):
            again = _load("zc_again", SCRIPT)
        self.assertEqual(again.CORRELATION_WINDOW_S, 300)


if __name__ == "__main__":
    unittest.main()
