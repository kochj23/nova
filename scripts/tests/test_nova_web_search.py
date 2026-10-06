#!/usr/bin/env python3
"""Tests for nova_web_search.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import atexit
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
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2                    # real module first, so patch("psycopg2.connect") hits the one the script imports lazily

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_web_search.py"
SRC = SCRIPT.read_text()

_TMP = tempfile.TemporaryDirectory()            # WEBSEARCH_CACHE_DIR -> tempdir, so no file lands in ~/.openclaw/workspace
atexit.register(_TMP.cleanup)
CACHE = Path(_TMP.name) / "cache"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"WEBSEARCH_CACHE_DIR": str(CACHE), "NOVA_OPS_DSN": "dbname=test"}):
        spec.loader.exec_module(mod)
    return mod


ws = _load("ws", SCRIPT)
SEARX = {"results": [{"title": "Nova docs", "url": "https://nova.example/docs", "content": "all about nova", "engine": "google"},
                     {"title": "Other", "url": "https://o.example", "content": "meh"}]}
DDG = {"AbstractText": "Nova is an AI.", "AbstractTitle": "Nova", "AbstractURL": "https://ddg.example/nova",
       "RelatedTopics": [{"Text": "related one", "FirstURL": "https://ddg.example/r/One"}]}


class _Curl:
    """subprocess.run stand-in for the two curl calls: SearXNG first, DuckDuckGo fallback second."""
    def __init__(self, searx=SEARX, ddg=DDG, searx_rc=0, ddg_rc=0, exc=None):
        self.searx, self.ddg, self.searx_rc, self.ddg_rc, self.exc = searx, ddg, searx_rc, ddg_rc, exc; self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if self.exc:
            raise self.exc
        if "api.duckduckgo.com" in argv[-1]:
            return subprocess.CompletedProcess(argv, self.ddg_rc, stdout=json.dumps(self.ddg) if not self.ddg_rc else "", stderr="")
        return subprocess.CompletedProcess(argv, self.searx_rc, stdout=json.dumps(self.searx) if not self.searx_rc else "", stderr="")


class _PG:
    def __init__(self, exc=None):
        self.exc = exc; self.executed = []
        self.cur = MagicMock(); self.cur.__enter__ = lambda s: s; self.cur.__exit__ = lambda s, *a: False
        self.cur.execute = lambda sql, params=None: self.executed.append((" ".join(sql.split()), params))
        self.conn = MagicMock(); self.conn.__enter__ = lambda s: s; self.conn.__exit__ = lambda s, *a: False
        self.conn.cursor = lambda: self.cur

    def __call__(self, dsn, **kw):
        self.dsn, self.kw = dsn, kw
        if self.exc:
            raise self.exc
        return self.conn


@contextmanager
def _env(curl=None, pg=None):
    """Every outbound path of search(): curl via subprocess, psycopg2.connect, nova_resolve/nova_untrusted lazies."""
    curl = curl or _Curl(); pg = pg or _PG()
    resolve = types.ModuleType("nova_resolve"); resolve.resolve_url = lambda svc, path: f"http://searxng.test{path}"
    untrusted = types.ModuleType("nova_untrusted"); untrusted.scan_results = MagicMock(side_effect=lambda r, **k: r)
    with patch("subprocess.run", curl), patch("psycopg2.connect", pg), \
         patch.dict(sys.modules, {"nova_resolve": resolve, "nova_untrusted": untrusted}), redirect_stderr(io.StringIO()):
        yield curl, pg, untrusted


def _clear():
    for f in CACHE.glob("query-*.json"):
        f.unlink()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)
        self.assertNotIn("password", ws.PG_DSN)

    def test_query_is_urlencoded_and_curl_takes_it_after_a_double_dash(self):
        _clear()
        with _env() as (curl, pg, _):
            ws.search("x; rm -rf / && echo 'pwned' #", force_refresh=True)
        argv = curl.calls[0]
        self.assertEqual(argv[:2], ["curl", "-s"]); self.assertEqual(argv[-2], "--")
        self.assertIn("q=x%3B+rm+-rf+%2F+%26%26+echo+%27pwned%27+%23", argv[-1])
        self.assertTrue(argv[-1].startswith("http://searxng.test/search?"))

    def test_pg_insert_is_parameterized_and_truncated(self):
        _clear()
        with _env() as (curl, pg, _):
            ws.search("q" * 5000, force_refresh=True)
        sql, params = pg.executed[0]
        self.assertIn("INSERT INTO web_searches (query, backend, result_count, latency_ms, cache_hit, region, ok) VALUES (%s, %s, %s, %s, %s, %s, %s)", sql)
        self.assertEqual(len(params[0]), 2000)
        self.assertEqual(pg.kw, {"connect_timeout": 2})

    def test_cache_key_is_a_hash_so_a_query_cannot_choose_its_file(self):
        _clear()
        ws.WebSearchCache.store("../../etc/passwd", [{"title": "t"}])
        files = list(CACHE.glob("query-*.json"))
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^query-[0-9a-f]{12}\.json$")
        self.assertEqual(files[0].parent, CACHE)
        _clear()


class TestPerformance(unittest.TestCase):
    def test_hashing_10k_queries_and_a_200_entry_cache_roundtrip(self):
        _clear()
        t0 = time.perf_counter()
        keys = {ws.WebSearchCache._query_hash(f"query {i}") for i in range(10_000)}
        for i in range(200):
            ws.WebSearchCache.store(f"q{i}", [{"title": f"t{i}", "url": "u", "snippet": "s"}])
        hits = sum(1 for i in range(200) if ws.WebSearchCache.get(f"q{i}"))
        stats = ws.WebSearchCache.stats()
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(keys), 10_000); self.assertEqual(hits, 200); self.assertEqual(stats["total_queries"], 200)
        _clear()


class TestRetry(unittest.TestCase):
    def test_searxng_failure_falls_back_to_duckduckgo_once_then_gives_up(self):
        # RETRY GAP: DuckDuckGoSearch.search — SearXNG is tried once, DDG once; both failing returns None
        _clear()
        with _env(_Curl(searx_rc=7)) as (curl, _, _):
            r = ws.search("nova", force_refresh=True)
        self.assertEqual(len(curl.calls), 2)
        self.assertEqual([x["source"] for x in r], ["duckduckgo", "duckduckgo"])
        self.assertEqual(r[0]["title"], "Nova")
        with _env(_Curl(searx_rc=7, ddg_rc=28)) as (curl, pg, _):
            self.assertIsNone(ws.search("nova", force_refresh=True))
        self.assertEqual(len(curl.calls), 2)
        self.assertEqual(pg.executed[0][1][1], "none")                # the miss is still logged, backend "none"

    def test_curl_exception_fails_open_to_none(self):
        with _env(_Curl(exc=subprocess.TimeoutExpired("curl", 10))) as (curl, _, _):
            self.assertIsNone(ws.search("nova", force_refresh=True))

    def test_pg_logging_never_breaks_search(self):
        # RETRY GAP: _log_search_pg — one connect attempt, every error swallowed
        _clear()
        with _env(pg=_PG(exc=psycopg2.OperationalError("down"))) as (curl, _, _):
            r = ws.search("nova", force_refresh=True)
        self.assertEqual(len(r), 2)


class TestUnit(unittest.TestCase):
    def test_cache_get_store_expiry_and_clear_entry(self):
        _clear()
        self.assertIsNone(ws.WebSearchCache.get("nope"))
        ws.WebSearchCache.store("q", [{"title": "t"}])
        self.assertEqual(ws.WebSearchCache.get("q"), [{"title": "t"}])
        f = CACHE / f"query-{ws.WebSearchCache._query_hash('q')}.json"
        data = json.loads(f.read_text()); data["timestamp"] -= ws.CACHE_TTL + 1; f.write_text(json.dumps(data))
        self.assertIsNone(ws.WebSearchCache.get("q"))
        f.write_text("{corrupt")
        self.assertIsNone(ws.WebSearchCache.get("q"))
        with redirect_stdout(io.StringIO()):
            ws.WebSearchCache.clear_entry("q")
        self.assertFalse(f.exists())

    def test_stats_empty_and_populated(self):
        _clear()
        self.assertEqual(ws.WebSearchCache.stats()["total_queries"], 0)
        ws.WebSearchCache.store("a", []); ws.WebSearchCache.store("b", [])
        s = ws.WebSearchCache.stats()
        self.assertEqual(s["total_queries"], 2); self.assertEqual(s["cache_ttl_hours"], ws.CACHE_TTL / 3600)
        self.assertIsNotNone(s["newest_entry"])
        with redirect_stdout(io.StringIO()):
            ws.WebSearchCache.clear_all()
        self.assertEqual(ws.WebSearchCache.stats()["total_queries"], 0)

    def test_hash_is_12_hex_and_stable(self):
        self.assertEqual(ws.WebSearchCache._query_hash("x"), ws.WebSearchCache._query_hash("x"))
        self.assertRegex(ws.WebSearchCache._query_hash("x"), r"^[0-9a-f]{12}$")

    def test_count_and_safe_search_mapping(self):
        _clear()
        with _env() as (curl, _, _):
            r = ws.search("nova", count=1, safe_search="strict", force_refresh=True)
        self.assertEqual(len(r), 1)
        self.assertIn("safesearch=2", curl.calls[0][-1])
        with _env() as (curl, _, _):
            ws.search("nova", safe_search="bogus", force_refresh=True)
        self.assertIn("safesearch=1", curl.calls[0][-1])


class TestIntegration(unittest.TestCase):
    def test_cache_hit_skips_curl_and_logs_a_hit(self):
        _clear()
        ws.WebSearchCache.store("nova", [{"title": "cached", "url": "u", "snippet": "s", "source": "searxng"}])
        with _env() as (curl, pg, _):
            r = ws.search("nova")
        self.assertEqual(r[0]["title"], "cached"); self.assertEqual(curl.calls, [])
        self.assertEqual(pg.executed[0][1], ("nova", "searxng", 1, 0, True, ws.DEFAULT_REGION, True))
        with _env() as (curl, pg, _):
            self.assertIsNone(ws.search("other", cache_only=True))
        self.assertEqual(curl.calls, []); self.assertEqual(pg.executed, [])

    def test_miss_searches_screens_stores_and_logs(self):
        _clear()
        with _env() as (curl, pg, untrusted):
            r = ws.search("nova", force_refresh=True)
        untrusted.scan_results.assert_called_once()
        self.assertEqual(untrusted.scan_results.call_args.kwargs, {"key": "snippet", "title_key": "title"})
        self.assertEqual(ws.WebSearchCache.get("nova"), r)
        q, backend, n, latency, hit, region, ok = pg.executed[0][1]
        self.assertEqual((q, backend, n, hit, ok), ("nova", "searxng", 2, False, True))
        self.assertIn("from nova_resolve import resolve_url", SRC); self.assertIn("nova_untrusted.scan_results", SRC)

    def test_store_as_memories_needs_the_shared_remember_script(self):
        with patch.object(ws.Path, "expanduser", lambda self: Path(_TMP.name) / "missing.sh"), redirect_stderr(io.StringIO()) as err, \
             patch("subprocess.run") as run:
            ws.store_as_memories([{"title": "t", "snippet": "s"}], topic="x")
        self.assertIn("Memory script not found", err.getvalue()); run.assert_not_called()
        self.assertIn("nova_remember.sh", SRC)


def _main(argv, curl=None, pg=None):
    out = io.StringIO()
    with patch.object(sys, "argv", ["nova_web_search.py", *argv]), _env(curl, pg) as env, redirect_stdout(out):
        code = ws.main()
    return code, out.getvalue(), env


class TestFunctional(unittest.TestCase):
    def test_golden_path_prints_and_json_modes(self):
        _clear()
        code, out, _ = _main(["nova", "--force-refresh", "--verbose"])
        self.assertEqual(code, 0)
        self.assertIn("Search: nova (2 results)", out); self.assertIn("1. Nova docs\n   https://nova.example/docs\n   all about nova", out)
        code, out, _ = _main(["nova", "--json"])
        self.assertEqual(json.loads(out)["count"], 2)
        code, out, _ = _main(["--cache-stats", "--json"])
        self.assertEqual((code, json.loads(out)["total_queries"]), (0, 1))
        _clear()

    def test_error_paths(self):
        _clear()
        code, out, _ = _main(["nothing", "--force-refresh", "--json"], _Curl(searx_rc=1, ddg_rc=1))
        self.assertEqual((code, json.loads(out)["success"]), (1, False))
        code, _, _ = _main(["--clear-cache-entry"])
        self.assertEqual(code, 1)
        code, out, _ = _main([])
        self.assertEqual(code, 1); self.assertIn("usage:", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1", "WEBSEARCH_CACHE_DIR": str(CACHE)}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--cache-stats", r.stdout)
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_web_search"], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
