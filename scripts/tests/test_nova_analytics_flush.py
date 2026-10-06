#!/usr/bin/env python3
"""Tests for nova_analytics_flush.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_analytics_flush.py"
SRC = SCRIPT.read_text()


def _stub_modules():
    lg = types.ModuleType("nova_logger")
    lg.LOG_INFO, lg.LOG_ERROR = "info", "error"
    lg.log = MagicMock()
    return {"nova_logger": lg}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()):          # logger bound at import; sys.modules restored after
        spec.loader.exec_module(mod)
    return mod


af = _load("analytics_flush_under_test", SCRIPT)


class _Cur:
    def __init__(self, fail_on=None):
        self.calls, self.rowcount, self.closed, self.fail_on = [], 3, False, fail_on

    def executemany(self, sql, rows):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("pg down mid-batch")
        self.calls.append(("many", " ".join(sql.split()), list(rows)))

    def execute(self, sql, params=None):
        self.calls.append(("one", " ".join(sql.split()), params))

    def close(self):
        self.closed = True

    def ran(self, frag):
        return [c for c in self.calls if frag in c[1]]


class _Conn:
    def __init__(self, cur):
        self.cur, self.commits, self.closed = cur, 0, False

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


class _Redis:
    def __init__(self, entries):
        self.entries, self.trims, self.closed = entries, [], False

    def xrange(self, key, count=None):
        self.xrange_args = (key, count)
        return self.entries[:count] if count else self.entries

    def xtrim(self, key, **kw):
        self.trims.append((key, kw))

    def close(self):
        self.closed = True


def _pv(i=1, **kw):
    d = {"type": "pageview", "ts": "1760000000", "site": "nova", "path": f"/p{i}", "referrer_domain": "",
         "country": "US", "ua_bucket": "desktop", "visitor_hash": "abc", "response_ms": "42"}
    d.update(kw)
    return d


def _ev(**kw):
    d = {"type": "event", "ts": "1760000000", "site": "nova", "path": "/e", "event_type": "click",
         "event_data": json.dumps({"k": 1}), "visitor_hash": "abc", "country": ""}
    d.update(kw)
    return d


def _run(entries, cur=None):
    cur = cur or _Cur()
    conn, r = _Conn(cur), _Redis(entries)
    af.log = MagicMock()
    with patch.object(af.redis, "from_url", return_value=r), patch.object(af.psycopg2, "connect", return_value=conn):
        af.flush()
    return cur, conn, r


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", af.PG_DSN)
        self.assertNotIn("@", af.REDIS_URL.split("//", 1)[1])        # no user:pass in the redis URL

    def test_sql_is_parameterized_and_writes_are_bounded(self):
        self.assertIsNone(re.search(r'execute(?:many)?\(\s*f"', SRC))
        self.assertIsNone(re.search(r'execute(?:many)?\([^)]*%\s*\(', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"analytics_pageviews", "analytics_events"})

    def test_hostile_values_stay_in_params_not_sql(self):
        evil = "x'); DROP TABLE analytics_pageviews; --"
        cur, _, _ = _run([("1-0", _pv(path=evil, site=evil))])
        kind, sql, rows = cur.ran("INSERT INTO analytics_pageviews")[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(rows[0][2], evil)


class TestPerformance(unittest.TestCase):
    def test_10k_entries_parse_and_batch_fast(self):
        entries = [(f"{i}-0", _pv(i) if i % 2 else _ev(event_data="{bad json")) for i in range(10_000)]
        t0 = time.perf_counter()
        with patch.object(_Redis, "xrange", lambda self, key, count=None: self.entries):   # bypass the 1000 cap for the hot path
            cur, conn, r = _run(entries)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(cur.ran("analytics_pageviews")[0][2]), 5_000)
        self.assertEqual(len(cur.ran("INSERT INTO analytics_events")[0][2]), 5_000)
        self.assertEqual(af.BATCH_SIZE, 1000)                            # the real read is bounded per run


class TestRetry(unittest.TestCase):
    def test_insert_failure_never_trims_the_stream(self):
        # RETRY GAP: flush() — one attempt per run; a PG failure propagates to the scheduler (loud), but the
        # consumed entries are NOT trimmed from Redis, so the next 5-minute run replays them (no data loss).
        cur = _Cur(fail_on="analytics_pageviews")
        conn, r = _Conn(cur), _Redis([("1-0", _pv())])
        af.log = MagicMock()
        with patch.object(af.redis, "from_url", return_value=r), patch.object(af.psycopg2, "connect", return_value=conn):
            with self.assertRaises(RuntimeError):
                af.flush()
        self.assertEqual(r.trims, [])
        self.assertEqual(conn.commits, 0)

    def test_redis_outage_never_touches_pg(self):
        # RETRY GAP: flush()/redis.from_url — no retry; the connection error escapes before any PG connect
        af.log = MagicMock()
        with patch.object(af.redis, "from_url", side_effect=ConnectionError("redis down")), \
             patch.object(af.psycopg2, "connect") as pg:
            with self.assertRaises(ConnectionError):
                af.flush()
        pg.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_pageview_row_shape_and_null_coercion(self):
        cur, _, _ = _run([("1-0", _pv(referrer_domain="", country="", ua_bucket="", response_ms="0"))])
        row = cur.ran("INSERT INTO analytics_pageviews")[0][2][0]
        self.assertEqual(row[0].isoformat(), "2025-10-09T08:53:20+00:00")
        self.assertEqual(row[1:], ("nova", "/p1", None, None, None, "abc", None))

    def test_event_data_bad_json_becomes_empty_object(self):
        cur, _, _ = _run([("1-0", _ev(event_data="{not json")), ("2-0", _ev(event_data={"already": "dict"}))])
        rows = cur.ran("INSERT INTO analytics_events")[0][2]
        self.assertEqual(rows[0][4], "{}")
        self.assertEqual(json.loads(rows[1][4]), {"already": "dict"})
        self.assertEqual(rows[0][3], "click"); self.assertIsNone(rows[0][6])

    def test_zero_ts_uses_now_and_unknown_type_is_dropped(self):
        before = af.datetime.now(af.timezone.utc)
        cur, _, _ = _run([("1-0", _pv(ts="0")), ("2-0", {"type": "heartbeat"}), ("3-0", {})])
        row = cur.ran("INSERT INTO analytics_pageviews")[0][2][0]
        self.assertGreaterEqual(row[0], before)
        self.assertEqual(cur.ran("INSERT INTO analytics_events"), [])


class TestIntegration(unittest.TestCase):
    def test_logger_is_the_shared_nova_logger(self):
        self.assertIn("from nova_logger import log, LOG_INFO, LOG_ERROR", SRC)
        self.assertNotIn("def log(", SRC)
        cur, _, _ = _run([])
        af.log.assert_any_call("No entries to flush", level="info", source="analytics_flush")

    def test_stream_key_is_read_and_trimmed_consistently(self):
        cur, conn, r = _run([("5-0", _pv()), ("9-1", _ev())])
        self.assertEqual(r.xrange_args, ("analytics:events", 1000))
        self.assertEqual(r.trims, [("analytics:events", {"minid": "9-1", "approximate": False})])

    def test_retention_sums_both_deletes(self):
        cur, conn, r = _run([("1-0", _pv())])
        dels = cur.ran("DELETE FROM")
        self.assertEqual([d[1] for d in dels], ["DELETE FROM analytics_pageviews WHERE ts < now() - interval '90 days'",
                                                "DELETE FROM analytics_events WHERE ts < now() - interval '90 days'"])
        af.log.assert_any_call("Retention cleanup: removed 6 old rows", level="info", source="analytics_flush")


class TestFunctional(unittest.TestCase):
    def test_golden_path_inserts_commits_trims_closes(self):
        cur, conn, r = _run([("1-0", _pv(1)), ("2-0", _pv(2)), ("3-0", _ev())])
        self.assertEqual(len(cur.ran("INSERT INTO analytics_pageviews")[0][2]), 2)
        self.assertEqual(len(cur.ran("INSERT INTO analytics_events")[0][2]), 1)
        self.assertEqual(conn.commits, 2)
        self.assertEqual(r.trims[0][1]["minid"], "3-0")
        self.assertTrue(cur.closed and conn.closed and r.closed)
        af.log.assert_any_call("Flushed 2 pageviews + 1 events", level="info", source="analytics_flush")

    def test_empty_stream_writes_nothing_and_closes(self):
        cur, conn, r = _run([])
        self.assertEqual(cur.calls, [])
        self.assertEqual(conn.commits, 0); self.assertEqual(r.trims, [])
        self.assertTrue(conn.closed and r.closed)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_flush(self):
        self.assertIn('if __name__ == "__main__":\n    flush()', SRC)
        code = ("import sys, types\n"
                "lg = types.ModuleType('nova_logger'); lg.LOG_INFO='info'; lg.LOG_ERROR='error'; lg.log=lambda *a, **k: None\n"
                "sys.modules['nova_logger'] = lg\n"
                "import redis, psycopg2\n"
                "redis.from_url = lambda *a, **k: (_ for _ in ()).throw(AssertionError('flush ran at import'))\n"
                "psycopg2.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError('flush ran at import'))\n"
                "import nova_analytics_flush as m\n"
                "print(m.STREAM_KEY)\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "analytics:events")


if __name__ == "__main__":
    unittest.main()
