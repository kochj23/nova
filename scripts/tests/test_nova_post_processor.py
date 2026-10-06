#!/usr/bin/env python3
"""Tests for nova_post_processor.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_post_processor.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    cfg = types.ModuleType("nova_config"); cfg.SLACK_NOTIFY = "C_TEST"; cfg.post_both = mock.MagicMock()
    with mock.patch.dict(sys.modules, {"nova_config": cfg}):
        spec.loader.exec_module(mod)
    return mod


PP = _load("post_processor_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="postproc-test-"))
PP.LOG_FILE = TMP / "nova_post_processor.log"


def _conn(rows, cols):
    cur = mock.MagicMock()
    cur.description = [(c,) for c in cols]
    cur.fetchall.return_value = rows
    c = mock.MagicMock(); c.cursor.return_value = cur
    return c


class _Quiet(unittest.TestCase):
    def setUp(self):
        self._buf = io.StringIO(); self._rs = redirect_stdout(self._buf); self._rs.__enter__()

    def tearDown(self):
        self._rs.__exit__(None, None, None)


class TestSecurity(_Quiet):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"postgresql://\w+:\w+@")      # DSNs carry no password

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'_db_query\([^,]+,\s*f"')
        self.assertEqual(SRC.count("make_interval(hours => %s)"), 3)
        c = _conn([], ["source", "count", "first", "last"])
        with mock.patch.object(PP.psycopg2, "connect", return_value=c):
            PP.summarize_source_activity(48)
        self.assertEqual(c.cursor.return_value.execute.call_args[0][1], (48,))

    def test_endpoints_are_internal_only(self):
        for host in ("pg-primary.digitalnoise.net", "192.168.1.6", "memory-server.digitalnoise.net"):
            self.assertIn(host, SRC)
        self.assertNotRegex(SRC, r"https?://(?!memory-server\.digitalnoise\.net)[a-z0-9.-]+\.(com|io|org)")


class TestPerformance(_Quiet):
    def test_theme_detection_over_10k_rows(self):
        rows = [{"text": f"Nova fleet status report number {i} about the kitchen sensor and patio heat"} for i in range(10_000)]
        with mock.patch.object(PP, "_db_query", return_value=rows):
            t0 = time.perf_counter()
            themes = PP.detect_recurring_themes(24)
            dt = time.perf_counter() - t0
        self.assertLess(dt, 5.0)
        self.assertTrue(any(t["term"] == "nova" and t["occurrences"] == 10_000 for t in themes))
        self.assertLessEqual(len(themes), 30)


class TestRetry(_Quiet):
    def test_db_query_retries_with_backoff_then_succeeds(self):
        good = _conn([("x", 2, None, None)], ["source", "count", "first", "last"])
        with mock.patch.object(PP.psycopg2, "connect", side_effect=[OSError("blip"), OSError("blip"), good]) as pc, \
             mock.patch.object(PP.time, "sleep") as sl:
            rows = PP._db_query("dsn", "SELECT 1")
        self.assertEqual(rows, [{"source": "x", "count": 2, "first": None, "last": None}])
        self.assertEqual(pc.call_count, 3)
        self.assertEqual([c[0][0] for c in sl.call_args_list], [0.5, 1.0])

    def test_db_query_persistent_failure_is_none_not_empty(self):
        with mock.patch.object(PP.psycopg2, "connect", side_effect=OSError("down")) as pc, mock.patch.object(PP.time, "sleep"):
            self.assertIsNone(PP._db_query("dsn", "SELECT 1"))
        self.assertEqual(pc.call_count, 3)
        self.assertIn("attempt 3/3", self._buf.getvalue())

    def test_memory_store_is_one_shot_and_fails_open(self):
        # RETRY GAP: run_post_processing()/urlopen — the meta-memory POST is tried once and silently dropped
        rc = mock.MagicMock()
        with mock.patch.object(PP, "_get_redis", return_value=rc), mock.patch.object(PP, "_db_query", return_value=[]), \
             mock.patch.object(PP.urllib.request, "urlopen", side_effect=OSError("mem down")) as uo:
            res = PP.run_post_processing()
        self.assertEqual(uo.call_count, 1)
        self.assertIn("run_time_s", res)
        rc.setex.assert_called_once()


class TestUnit(_Quiet):
    def test_summary_unavailable_vs_counts(self):
        with mock.patch.object(PP, "_db_query", return_value=None):
            s = PP.summarize_source_activity(24)
        self.assertTrue(s["unavailable"])
        self.assertIsNone(s["sources"]); self.assertIsNone(s["total_memories"])
        rows = [{"source": "a", "count": 5}, {"source": "b", "count": 2}] + [{"source": f"s{i}", "count": 1} for i in range(12)]
        with mock.patch.object(PP, "_db_query", return_value=rows):
            s = PP.summarize_source_activity(24)
        self.assertEqual((s["sources"], s["total_memories"], len(s["top_sources"])), (14, 19, 10))
        self.assertEqual(s["top_sources"][0], {"source": "a", "count": 5})

    def test_recurring_themes_rules(self):
        rows = [{"text": "The kitchen sensor fired again"}] * 3 + [{"text": "the the the"}]
        with mock.patch.object(PP, "_db_query", return_value=rows):
            themes = PP.detect_recurring_themes(24, min_occurrences=3)
        terms = {t["term"]: t for t in themes}
        self.assertIn("kitchen", terms); self.assertEqual(terms["kitchen"]["type"], "word")
        self.assertIn("kitchen sensor", terms); self.assertEqual(terms["kitchen sensor"]["type"], "bigram")
        self.assertNotIn("the", terms)                            # stop word
        self.assertEqual(terms["fired"]["occurrences"], 3)
        self.assertNotIn("sensor fired again", terms)             # only bigrams, never longer n-grams
        with mock.patch.object(PP, "_db_query", return_value=[]):
            self.assertEqual(PP.detect_recurring_themes(), [])
        with mock.patch.object(PP, "_db_query", return_value=None):
            self.assertEqual(PP.detect_recurring_themes(), [])

    def test_cross_domain_links(self):
        rows = [{"source": "news", "text": "Burbank Airport reopened"}, {"source": "flights", "text": "Burbank Airport traffic up"},
                {"source": "security", "text": "Burbank patrol"}, {"source": "news", "text": "Only Here"}]
        with mock.patch.object(PP, "_db_query", return_value=rows):
            links = PP.find_cross_domain_links(24)
        by = {l["term"]: l for l in links}
        self.assertEqual(by["Burbank Airport"]["sources"], ["flights", "news"])   # capitalised phrase is one term
        self.assertEqual(by["Burbank Airport"]["cross_domain_count"], 2)
        self.assertNotIn("Burbank", by)                           # lone 'Burbank' appears in a single source only
        self.assertNotIn("Only Here", by)
        self.assertEqual(links, [by["Burbank Airport"]])

    def test_log_writes_redirected_file(self):
        PP.log("hello", "WARN")
        self.assertIn("[WARN] hello", PP.LOG_FILE.read_text())


class TestIntegration(_Quiet):
    def test_tasks_query_the_memories_db_and_results_land_in_redis(self):
        seen = []
        def _q(dsn, sql, params=None, retries=3):
            seen.append((dsn, sql.strip().split()[0], params)); return []
        rc = mock.MagicMock()
        with mock.patch.object(PP, "_db_query", side_effect=_q), mock.patch.object(PP, "_get_redis", return_value=rc), \
             mock.patch.object(PP.urllib.request, "urlopen"):
            PP.run_post_processing()
        self.assertEqual({d for d, _, _ in seen}, {PP.MEMORIES_DSN})
        self.assertEqual([p for _, _, p in seen], [(24,), (24,), (24,)])
        key, ttl, payload = rc.setex.call_args[0]
        self.assertEqual(key, f"{PP.RESULTS_KEY_PREFIX}:{datetime.now().strftime('%Y-%m-%d')}")
        self.assertEqual(ttl, 86400)
        self.assertEqual(json.loads(payload)["source_activity"]["total_memories"], 0)

    def test_redis_url_helper(self):
        with mock.patch.object(PP.redis, "from_url") as fu:
            PP._get_redis()
        fu.assert_called_once_with(PP.REDIS_URL, decode_responses=True)


class TestFunctional(_Quiet):
    def test_golden_run_posts_meta_memory(self):
        rows = [{"source": "news", "count": 3, "first": None, "last": None}]
        themes = [{"text": "Burbank Airport delays again today"}] * 4
        def _q(dsn, sql, params=None, retries=3):
            return rows if "GROUP BY source" in sql else themes if "SELECT text FROM" in sql else [{"source": "a", "text": "Burbank"}, {"source": "b", "text": "Burbank"}]
        rc = mock.MagicMock()
        with mock.patch.object(PP, "_db_query", side_effect=_q), mock.patch.object(PP, "_get_redis", return_value=rc), \
             mock.patch.object(PP.urllib.request, "urlopen") as uo:
            res = PP.run_post_processing()
        req = uo.call_args[0][0]
        self.assertEqual(req.full_url, f"{PP.MEMORY_URL}/remember?async=1")
        body = json.loads(req.data)
        self.assertEqual(body["source"], "infrastructure")
        self.assertIn("3 new memories", body["text"]); self.assertIn("Top themes:", body["text"])
        self.assertEqual(res["cross_links"][0]["term"], "Burbank")
        self.assertIn("Post-processing complete", self._buf.getvalue())

    def test_task_exception_is_isolated(self):
        rc = mock.MagicMock()
        with mock.patch.object(PP, "summarize_source_activity", side_effect=RuntimeError("bad")), \
             mock.patch.object(PP, "_db_query", return_value=[]), mock.patch.object(PP, "_get_redis", return_value=rc), \
             mock.patch.object(PP.urllib.request, "urlopen"):
            res = PP.run_post_processing()
        self.assertNotIn("source_activity", res)
        self.assertEqual(res["themes"], [])
        self.assertIn("Source activity failed: bad", self._buf.getvalue())
        rc.setex.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_post_processor"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
