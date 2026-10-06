#!/usr/bin/env python3
"""Tests for nova_pihole_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_pihole_poller.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="pihole-poller-test-"))


def _load(env=None):
    spec = importlib.util.spec_from_file_location("pihole_poller_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("urllib.request.urlopen", side_effect=AssertionError("net at import")), patch.dict(os.environ, env or {}):
        spec.loader.exec_module(mod)
    return mod


pp = _load()
SUMMARY = {"dns_queries_today": "12345", "ads_blocked_today": 678, "ads_percentage_today": "5.49",
           "domains_being_blocked": 100000, "queries_cached": 4000, "queries_forwarded": 8000,
           "unique_clients": 23, "status": "enabled"}
TOP = {"top_queries": {"apple.com": 50, "slack.com": "12"}, "top_ads": {"ads.example": 7}}


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return json.dumps(self.body).encode()


class _Cur:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.autocommit = False; self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _one_iteration(summary=SUMMARY, top=TOP, fetch_exc=None):
    """Run main() for exactly one poll: the first sleep flips _running off."""
    cur = _Cur()

    def stop(_):
        pp._running = False
    pp._running = True

    def urlopen(req, timeout=None):
        if fetch_exc:
            raise fetch_exc
        return _Resp(summary if "summary" in req.full_url else top)
    with patch.object(psycopg2, "connect", return_value=_Conn(cur)), patch.object(pp.signal, "signal"), \
         patch.object(pp.time, "sleep", side_effect=stop) as slp, patch("urllib.request.urlopen", side_effect=urlopen), \
         redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
        pp.main()
    pp._running = True
    return cur, slp, out.getvalue(), err.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("subprocess", SRC); self.assertNotIn("password", pp.PG_DSN)

    def test_api_values_are_coerced_and_bound_never_interpolated(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        cur = _Cur()
        evil = dict(SUMMARY, status="x'); DROP TABLE pihole_stats; --")
        with patch.object(psycopg2, "connect", return_value=_Conn(cur)):
            pp._store_stats(evil)
            pp._store_top_items({"top_queries": {"a'); DROP TABLE x; --": "3"}})
        for sql, params in cur.sql:
            self.assertNotIn("DROP", sql)
        self.assertEqual(cur.sql[0][1], (12345, 678, 5.49, 100000, 4000, 8000, 23, evil["status"]))
        self.assertEqual(cur.sql[1][1], ("a'); DROP TABLE x; --", 3))

    def test_bad_numeric_from_api_fails_before_any_insert(self):
        cur = _Cur()
        with patch.object(psycopg2, "connect", return_value=_Conn(cur)):
            with self.assertRaises(ValueError):
                pp._store_stats(dict(SUMMARY, dns_queries_today="lots"))
        self.assertEqual(cur.sql, [])


class TestPerformance(unittest.TestCase):
    def test_store_10k_top_domains_fast(self):
        cur = _Cur()
        top = {"top_queries": {f"q{i}.example": i for i in range(5000)}, "top_ads": {f"a{i}.example": i for i in range(5000)}}
        t0 = time.perf_counter()
        with patch.object(psycopg2, "connect", return_value=_Conn(cur)):
            pp._store_top_items(top)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(cur.sql), 10_000)
        self.assertIn("VALUES (%s, %s, true)", cur.sql[-1][0])


class TestRetry(unittest.TestCase):
    def test_api_fetches_fail_open_to_none(self):
        # RETRY GAP: _fetch_summary / _fetch_top_items — one urlopen each; failure returns None and the
        # loop simply waits for the next POLL_INTERVAL
        with patch("urllib.request.urlopen", side_effect=OSError("pihole down")) as u, redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(pp._fetch_summary()); self.assertIsNone(pp._fetch_top_items())
        self.assertEqual(u.call_count, 2)
        self.assertIn("API fetch failed", err.getvalue())

    def test_a_dead_api_means_no_pg_writes_but_the_loop_survives(self):
        cur, slp, out, err = _one_iteration(fetch_exc=OSError("down"))
        self.assertEqual([s for s, _ in cur.sql if s.startswith("INSERT")], [])
        self.assertEqual(slp.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_fetch_parsing_and_endpoints(self):
        with patch("urllib.request.urlopen", return_value=_Resp(SUMMARY)) as u:
            self.assertEqual(pp._fetch_summary(), SUMMARY)
        self.assertEqual(u.call_args[0][0].full_url, f"{pp.PIHOLE_API}?summary")
        with patch("urllib.request.urlopen", return_value=_Resp(TOP)) as u:
            self.assertEqual(pp._fetch_top_items(), TOP)
        self.assertEqual(u.call_args[0][0].full_url, f"{pp.PIHOLE_API}?topItems=20")

    def test_store_stats_defaults_missing_fields(self):
        cur = _Cur()
        with patch.object(psycopg2, "connect", return_value=_Conn(cur)):
            pp._store_stats({})
        self.assertEqual(cur.sql[0][1], (0, 0, 0.0, 0, 0, 0, 0, "unknown"))

    def test_store_top_items_handles_null_sections(self):
        cur = _Cur()
        with patch.object(psycopg2, "connect", return_value=_Conn(cur)):
            pp._store_top_items({"top_queries": None, "top_ads": {"x": 1}})
        self.assertEqual(cur.sql, [("INSERT INTO pihole_top_domains (domain, count, blocked) VALUES (%s, %s, true)", ("x", 1))])

    def test_dsn_is_env_overridable_for_portability(self):
        self.assertEqual(pp.PG_DSN, "dbname=nova_ops user=kochj host=pg-primary.digitalnoise.net")
        self.assertEqual(_load({"NOVA_PG_DSN": "host=192.168.1.6 dbname=nova_ops user=kochj"}).PG_DSN,
                         "host=192.168.1.6 dbname=nova_ops user=kochj")


class TestIntegration(unittest.TestCase):
    def test_tables_created_before_polling_and_both_writes_follow_each_fetch(self):
        cur, slp, out, err = _one_iteration()
        heads = [s[:40] for s, _ in cur.sql]
        self.assertTrue(heads[0].startswith("CREATE TABLE IF NOT EXISTS pihole_stats"))
        self.assertTrue(heads[1].startswith("CREATE TABLE IF NOT EXISTS pihole_top_do"))
        self.assertTrue(heads[2].startswith("INSERT INTO pihole_stats"))
        self.assertEqual(sum(1 for h in heads if h.startswith("INSERT INTO pihole_top_domains")), 3)
        self.assertEqual(cur.sql[-1][1], ("ads.example", 7))
        self.assertEqual(slp.call_args[0][0], pp.POLL_INTERVAL)

    def test_connect_uses_the_module_dsn_with_a_short_timeout(self):
        with patch.object(psycopg2, "connect", return_value=_Conn(_Cur())) as c:
            pp._create_table()
        c.assert_called_once_with(pp.PG_DSN, connect_timeout=5)


class TestFunctional(unittest.TestCase):
    def test_golden_poll_logs_the_headline_numbers(self):
        cur, slp, out, err = _one_iteration()
        self.assertIn("[pihole-poller] Starting (polling every 60s)", out)
        self.assertIn("[pihole-poller] 12345 queries, 678 blocked, 23 clients", out)
        stats = [p for s, p in cur.sql if s.startswith("INSERT INTO pihole_stats")]
        self.assertEqual(stats, [(12345, 678, 5.49, 100000, 4000, 8000, 23, "enabled")])

    def test_summary_without_top_items_still_stores_stats(self):
        cur = _Cur(); pp._running = True

        def stop(_):
            pp._running = False

        def urlopen(req, timeout=None):
            if "summary" in req.full_url:
                return _Resp(SUMMARY)
            raise OSError("top items 500")
        with patch.object(psycopg2, "connect", return_value=_Conn(cur)), patch.object(pp.signal, "signal"), \
             patch.object(pp.time, "sleep", side_effect=stop), patch("urllib.request.urlopen", side_effect=urlopen), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            pp.main()
        pp._running = True
        self.assertEqual(len([s for s, _ in cur.sql if "pihole_stats" in s and s.startswith("INSERT")]), 1)
        self.assertEqual([s for s, _ in cur.sql if "pihole_top_domains" in s and s.startswith("INSERT")], [])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        boot = ("import sys, unittest.mock as um, psycopg2, urllib.request, runpy; "
                "psycopg2.connect = um.MagicMock(side_effect=AssertionError('pg at import')); "
                "urllib.request.urlopen = um.MagicMock(side_effect=AssertionError('net at import')); "
                "runpy.run_path(sys.argv[1], run_name='imported'); print('IMPORT_OK')")
        r = subprocess.run([sys.executable, "-c", boot, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "IMPORT_OK")


if __name__ == "__main__":
    unittest.main()
