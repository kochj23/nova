#!/usr/bin/env python3
"""Tests for nova_shared_observations.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_shared_observations.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


so = _load("shared_obs_under_test", SCRIPT)
# Module-level stub: the real psycopg2 is never reachable from this file.
so.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=OSError("offline: pg stubbed")),
                                    extras=types.SimpleNamespace(RealDictCursor="RealDictCursor"))


class _Cur:
    """Cursor stub: records SQL + params; `rows` answers fetchall, `one` answers fetchone."""
    def __init__(self, rows=None, one=None, fail=False):
        self.rows, self.one, self.fail = rows or [], one, fail
        self.sql, self.params, self.closed, self.factory = [], [], False, None

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("stub failure")
        self.sql.append(" ".join(sql.split())); self.params.append(params)

    def fetchone(self): return self.one
    def fetchall(self): return list(self.rows)
    def close(self): self.closed = True


class _Conn:
    def __init__(self, cur): self.cur = cur; self.closed = False; self.autocommit = False
    def cursor(self, cursor_factory=None): self.cur.factory = cursor_factory; return self.cur
    def close(self): self.closed = True


def _pg(cur):
    conn = _Conn(cur)
    return patch.object(so.psycopg2, "connect", MagicMock(return_value=conn)), conn


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", so.DB_DSN)

    def test_filters_are_bound_parameters_never_interpolated(self):
        evil = "x' OR 1=1; DROP TABLE shared_observations; --"
        cur = _Cur()
        p, _ = _pg(cur)
        with p:
            so.get_observations(observer=evil, category=evil, subject=evil, severity=evil, unacked_only=True, limit=5)
        self.assertNotIn("DROP", cur.sql[0])
        self.assertEqual(cur.params[0], [evil, evil, f"%{evil}%", evil, 5])
        self.assertIn("observer = %s AND category = %s AND subject ILIKE %s AND severity = %s AND acknowledged_by IS NULL", cur.sql[0])

    def test_writes_are_limited_to_shared_observations(self):
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"shared_observations"})
        self.assertIsNone(re.search(r"DELETE FROM", SRC))

    def test_ack_cannot_overwrite_an_existing_ack(self):
        cur = _Cur()
        p, _ = _pg(cur)
        with p:
            so.ack("claude", 7)
        self.assertIn("WHERE id = %s AND acknowledged_by IS NULL", cur.sql[0])
        self.assertEqual(cur.params[0], ("claude", 7))

    def test_http_server_binds_loopback_only(self):
        self.assertIn('HTTPServer(("127.0.0.1", PORT), Handler)', SRC)


class TestPerformance(unittest.TestCase):
    def test_10k_observations_round_trip_quickly(self):
        rows = [{"id": i, "observer": "nova", "observation": f"o{i}", "metadata": {}} for i in range(10_000)]
        cur = _Cur(rows=rows)
        p, _ = _pg(cur)
        t0 = time.perf_counter()
        with p:
            out = so.get_observations(limit=10_000)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(out), 10_000)
        self.assertEqual(len(cur.sql), 1)                       # one query, no per-row round trips

    def test_observe_serializes_metadata_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            json.dumps({"avg_ms": i, "time": "03:00"} or {})
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_connect_failure_propagates_once(self):
        # RETRY GAP: _conn/psycopg2.connect — one attempt; the caller sees the exception
        connect = MagicMock(side_effect=OSError("pg down"))
        with patch.object(so.psycopg2, "connect", connect):
            with self.assertRaises(OSError):
                so.observe("nova", "runtime", "memory-server", "slow")
        self.assertEqual(connect.call_count, 1)

    def test_query_failure_propagates_and_is_not_retried(self):
        # RETRY GAP: get_observations/summary — no retry, no cached fallback
        cur = _Cur(fail=True)
        connect = MagicMock(return_value=_Conn(cur))
        with patch.object(so.psycopg2, "connect", connect):
            with self.assertRaises(RuntimeError):
                so.summary()
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(cur.sql, [])


class TestUnit(unittest.TestCase):
    def test_conn_sets_autocommit(self):
        cur = _Cur()
        p, conn = _pg(cur)
        with p:
            c = so._conn()
        self.assertIs(c, conn)
        self.assertTrue(c.autocommit)

    def test_observe_defaults_and_expiry(self):
        cur = _Cur(one=(42,))
        p, _ = _pg(cur)
        with p:
            before = datetime.now()
            oid = so.observe("nova", "runtime", "memory-server", "spiking", severity="warning",
                             metadata={"avg_ms": 2100}, expires_hours=2)
        self.assertEqual(oid, 42)
        params = cur.params[0]
        self.assertEqual(params[:5], ("nova", "runtime", "memory-server", "spiking", "warning"))
        self.assertEqual(json.loads(params[5]), {"avg_ms": 2100})
        self.assertGreaterEqual((params[6] - before).total_seconds(), 2 * 3600 - 5)
        cur2 = _Cur(one=(1,))
        p2, _ = _pg(cur2)
        with p2:
            so.observe("claude", "code", "x", "y")
        self.assertEqual(cur2.params[0][4:], ("info", "{}", None))

    def test_get_observations_no_filters(self):
        cur = _Cur(rows=[{"id": 1}])
        p, _ = _pg(cur)
        with p:
            out = so.get_observations()
        self.assertEqual(out, [{"id": 1}])
        self.assertIn("WHERE (expires_at IS NULL OR expires_at > now()) ORDER BY observed_at DESC LIMIT %s", cur.sql[0])
        self.assertEqual(cur.params[0], [50])
        self.assertEqual(cur.factory, "RealDictCursor")

    def test_get_unacked_flips_party(self):
        with patch.object(so, "get_observations", MagicMock(return_value=[])) as g:
            so.get_unacked("nova"); so.get_unacked("claude"); so.get_unacked("someone-else")
        self.assertEqual([c[1]["observer"] for c in g.call_args_list], ["claude", "nova", "nova"])
        self.assertTrue(all(c[1]["unacked_only"] for c in g.call_args_list))


class TestIntegration(unittest.TestCase):
    def test_observe_then_get_unacked_chain(self):
        cur = _Cur(one=(9,), rows=[{"id": 9, "observer": "nova", "acknowledged_by": None}])
        p, conn = _pg(cur)
        with p:
            oid = so.observe("nova", "runtime", "memory-server", "slow")
            pending = so.get_unacked("claude")
        self.assertEqual(pending[0]["id"], oid)
        self.assertIn("INSERT INTO shared_observations", cur.sql[0])
        self.assertIn("observer = %s AND acknowledged_by IS NULL", cur.sql[1])
        self.assertEqual(cur.params[1], ["nova", 50])
        self.assertTrue(conn.closed and cur.closed)

    def test_summary_groups_by_observer_category_severity(self):
        cur = _Cur(rows=[{"observer": "nova", "category": "runtime", "severity": "info", "count": 3}])
        p, _ = _pg(cur)
        with p:
            out = so.summary()
        self.assertEqual(out[0]["count"], 3)
        self.assertIn("GROUP BY observer, category, severity", cur.sql[0])
        self.assertIn("FROM shared_observations", cur.sql[0])


class TestFunctional(unittest.TestCase):
    def test_golden_path_observe_ack_read(self):
        cur = _Cur(one=(5,), rows=[])
        p, _ = _pg(cur)
        with p:
            oid = so.observe("claude", "code", "nova_router.py", "dead import", severity="info")
            so.ack("nova", oid)
            self.assertEqual(so.get_unacked("nova"), [])
        self.assertEqual([s.split()[0] for s in cur.sql], ["INSERT", "UPDATE", "SELECT"])
        self.assertEqual(cur.params[1], ("nova", 5))

    def test_error_path_closes_nothing_twice_and_raises(self):
        cur = _Cur(fail=True)
        p, conn = _pg(cur)
        with p:
            with self.assertRaises(RuntimeError):
                so.ack("nova", 1)
        self.assertFalse(conn.closed)                            # documents: no finally/close on failure


class TestFrame(unittest.TestCase):
    def test_import_never_starts_the_http_server(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertIn("serve_forever()", SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_shared_observations; print(nova_shared_observations.DB_DSN)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), so.DB_DSN)
        self.assertNotIn("listening", r.stdout)


if __name__ == "__main__":
    unittest.main()
