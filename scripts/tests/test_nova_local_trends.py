#!/usr/bin/env python3
"""Tests for nova_local_trends.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). PG, the LLM, image generation, Hugo publish and git push are mocked.
Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_local_trends.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("localtrends", SCRIPTS / "nova_local_trends.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lt = _load()
lt.log = lambda *a, **k: None


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)

    def fetchall(self):
        return self.conn.rows


class _Conn:
    def __init__(self, rows=None):
        self.rows = rows or []; self.sql = []

    def cursor(self, **k):
        return _Cur(self)

    def close(self):
        pass


SAMPLE = {"lora": {"this_week": 12, "last_week": 10, "active_24h": 4},
          "lora_new": [{"long_name": "BurbankNode"}],
          "rf": {"ssids_this_week": 80, "ssids_last_week": 80, "open_aps": 3},
          "rf_new_ssids": [{"ssid": "FreeWifi"}],
          "rogue_flags": [{"ssid": "MyAP", "kind": "own-open"}],
          "flights": {"this_week": 5, "last_week": 0},
          "airwaves": [{"source": "fire", "this_week": 50, "last_week": 40},
                       {"source": "fire_ops", "this_week": 10, "last_week": 0},
                       {"source": "chp", "this_week": 300, "last_week": 2},
                       {"source": "rail", "this_week": 0, "last_week": 0}]}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_queries_are_static_read_only(self):
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))
        self.assertIsNone(re.search(r"_q\(\s*f[\"']", SRC))
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE \w+ SET|DELETE FROM)\b", SRC))


class TestPerformance(unittest.TestCase):
    def test_brief_with_10k_airwave_rows_fast(self):
        p = dict(SAMPLE, airwaves=[{"source": ["fire", "chp", "scanner"][i % 3], "this_week": i, "last_week": i}
                                   for i in range(10_000)])
        t0 = time.perf_counter()
        lt._brief(p)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_query_failure_fails_open(self):
        # RETRY GAP: _q / _airwaves_trend — one connect per query; failure returns an empty shape
        calls = []

        def boom(*a, **k):
            calls.append(1); raise lt.psycopg2.OperationalError("down")

        with patch.object(lt.psycopg2, "connect", side_effect=boom):
            self.assertEqual(lt._q("SELECT 1", one=True), {})
            self.assertEqual(lt._q("SELECT 1"), [])
            self.assertEqual(lt._airwaves_trend(), [])
        self.assertEqual(len(calls), 3)

    def test_publish_failure_never_raises(self):
        with patch.object(lt, "gather_local_trends", return_value={}), \
             patch.object(lt, "generate_article", side_effect=RuntimeError("openrouter 500")), \
             patch.object(lt.nj, "publish_hugo") as pub:
            self.assertIsNone(lt.main())
        pub.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_delta(self):
        self.assertEqual(lt._delta(5, 0), "no prior-week baseline")
        self.assertEqual(lt._delta(12, 10), "up 2 (+20%)")
        self.assertEqual(lt._delta(8, 10), "down 2 (-20%)")
        self.assertEqual(lt._delta(10, 10), "flat 0 (+0%)")

    def test_brief_content(self):
        b = lt._brief(SAMPLE)
        self.assertIn("12 distinct nodes", b)
        self.assertIn("BurbankNode", b)
        self.assertIn("'FreeWifi'", b)
        self.assertIn("MyAP (own-open)", b)
        self.assertIn("fire 60 (up 20 (+50%))", b)           # fire + fire_ops aggregated
        self.assertIn("CHP 300 (newly active", b)
        self.assertNotIn("rail", b.split("Airwaves")[1])      # silent feed omitted

    def test_brief_empty(self):
        b = lt._brief({})
        self.assertIn("0 distinct nodes", b)
        self.assertIn("flagged: 0.", b)


class TestIntegration(unittest.TestCase):
    def test_gather_reads_ops_and_memories_dbs(self):
        dsns = []

        def conn(dsn):
            dsns.append(dsn); return _Conn([{"this_week": 1, "last_week": 1}])

        with patch.object(lt.psycopg2, "connect", side_effect=conn), patch.object(lt, "sentinel", None):
            p = lt.gather_local_trends()
        self.assertEqual(set(p), {"lora", "lora_new", "rf", "rf_new_ssids", "rogue_flags", "flights", "airwaves"})
        self.assertEqual(dsns.count(lt.MEMDB_DSN), 1)
        self.assertTrue(lt.MEMDB_DSN.endswith("user=kochj") and "nova_memories" in lt.MEMDB_DSN)
        self.assertEqual(p["rogue_flags"], [])

    def test_uses_shared_journal_helpers(self):
        import nova_journal
        self.assertIs(lt.nj, nova_journal)
        self.assertIn("nj.call_openrouter", SRC)


class TestFunctional(unittest.TestCase):
    def test_main_generates_publishes_and_pushes(self):
        calls = []
        fake_hist = types.SimpleNamespace(recent_articles_context=lambda s: "PAST ARTICLES")
        with patch.object(lt, "gather_local_trends", return_value=SAMPLE), \
             patch.object(lt.nova_voice, "system_prompt", return_value="SYS"), \
             patch.object(lt.nj, "call_openrouter", side_effect=lambda s, u: calls.append(u) or '"A Title"'), \
             patch.object(lt, "generate_image", return_value="/tmp/img.webp"), \
             patch.object(lt.nj, "publish_hugo") as pub, patch.object(lt.nj, "git_push") as push, \
             patch.dict(sys.modules, {"nova_article_history": fake_hist}):
            lt.main()
        self.assertIn("BurbankNode", calls[0])
        self.assertIn("PAST ARTICLES", calls[0])
        self.assertEqual(pub.call_args[0][:3], ("A Title", '"A Title"', "local"))
        self.assertEqual(pub.call_args[1]["image_path"], "/tmp/img.webp")
        push.assert_called_once_with("local", "A Title")

    def test_image_failure_still_publishes(self):
        with patch.object(lt, "gather_local_trends", return_value={}), \
             patch.object(lt, "generate_article", return_value=("T", "body")), \
             patch.object(lt, "generate_image", side_effect=RuntimeError("gpu busy")), \
             patch.object(lt.nj, "publish_hugo") as pub, patch.object(lt.nj, "git_push"):
            lt.main()
        self.assertIsNone(pub.call_args[1]["image_path"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # main() queries PG, calls the LLM and git-pushes, so the smoke is an import only
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_local_trends"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
