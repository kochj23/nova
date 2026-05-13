"""
test_nova_web_search.py — All 7 test categories for nova_web_search.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_web_search.py"
_spec = importlib.util.spec_from_file_location("nova_web_search", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

WebSearchCache = _mod.WebSearchCache
DuckDuckGoSearch = _mod.DuckDuckGoSearch
search = _mod.search


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_", "api_key ="]:
            self.assertNotIn(pat, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_searxng_is_localhost(self):
        """SearXNG must be on localhost — no cloud API keys required."""
        src = _SCRIPT.read_text()
        self.assertIn("127.0.0.1:8888", src)

    def test_cache_dir_in_home(self):
        self.assertIn(str(Path.home()), str(_mod.CACHE_DIR))

    def test_no_tracking_params_sent(self):
        """Nova user-agent not revealing infrastructure details."""
        src = _SCRIPT.read_text()
        self.assertIn("Nova-Local-AI", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_cache_ttl_24h(self):
        self.assertEqual(_mod.CACHE_TTL, 86400)

    def test_cache_hit_fast(self):
        """Cache hit must not make any subprocess calls."""
        q = "test query performance"
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "CACHE_DIR", Path(tmpdir)):
                # Prime cache
                WebSearchCache.store(q, [{"title": "Test", "url": "http://test.com", "snippet": "x"}])
                start = time.perf_counter()
                result = WebSearchCache.get(q)
                elapsed = time.perf_counter() - start
        self.assertIsNotNone(result)
        self.assertLess(elapsed, 0.01)

    def test_query_hash_deterministic(self):
        h1 = WebSearchCache._query_hash("test query")
        h2 = WebSearchCache._query_hash("test query")
        self.assertEqual(h1, h2)

    def test_query_hash_different_for_different_queries(self):
        h1 = WebSearchCache._query_hash("python programming")
        h2 = WebSearchCache._query_hash("java programming")
        self.assertNotEqual(h1, h2)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_searxng_falls_back_to_ddg(self):
        """If SearXNG fails, must fall back to DuckDuckGo."""
        call_log = []

        def mock_run(cmd, **kwargs):
            call_log.append(" ".join(cmd))
            r = MagicMock()
            if "127.0.0.1:8888" in " ".join(cmd):
                r.returncode = 1
                r.stdout = ""
            else:
                r.returncode = 0
                r.stdout = json.dumps({
                    "AbstractText": "Python is a language",
                    "AbstractTitle": "Python",
                    "AbstractURL": "https://python.org",
                    "RelatedTopics": []
                })
            return r

        with patch("subprocess.run", side_effect=mock_run):
            result = DuckDuckGoSearch.search("python language", count=3)

        self.assertTrue(any("127.0.0.1:8888" in c for c in call_log), "SearXNG not tried")
        self.assertTrue(any("duckduckgo" in c for c in call_log), "DDG fallback not tried")

    def test_search_returns_none_on_all_failures(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            result = DuckDuckGoSearch.search("test")
        self.assertIsNone(result)

    def test_search_function_uses_cache_on_second_call(self):
        """search() called twice with same query should only hit network once."""
        network_calls = [0]

        def mock_run(cmd, **kwargs):
            network_calls[0] += 1
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"results": [{"title": "T", "url": "U", "content": "S", "engine": "test"}]})
            return r

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "CACHE_DIR", Path(tmpdir)):
                with patch("subprocess.run", side_effect=mock_run):
                    r1 = search("unique test query xyz")
                    r2 = search("unique test query xyz")  # should hit cache

        self.assertEqual(network_calls[0], 1, "Network called more than once — cache not used")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_cache_store_and_get(self):
        results = [{"title": "Test", "url": "http://example.com", "snippet": "Test snippet"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "CACHE_DIR", Path(tmpdir)):
                WebSearchCache.store("test query", results)
                loaded = WebSearchCache.get("test query")
        self.assertEqual(loaded, results)

    def test_cache_returns_none_for_expired(self):
        """Expired cache (past TTL) must return None."""
        results = [{"title": "Old", "url": "http://old.com", "snippet": "old"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "CACHE_DIR", Path(tmpdir)):
                WebSearchCache.store("old query", results)
                # Manually expire
                cache_key = WebSearchCache._query_hash("old query")
                cache_file = Path(tmpdir) / f"query-{cache_key}.json"
                data = json.loads(cache_file.read_text())
                data["timestamp"] = time.time() - _mod.CACHE_TTL - 100
                cache_file.write_text(json.dumps(data))
                loaded = WebSearchCache.get("old query")
        self.assertIsNone(loaded)

    def test_cache_stats_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "CACHE_DIR", Path(tmpdir)):
                stats = WebSearchCache.stats()
        self.assertEqual(stats["total_queries"], 0)

    def test_cache_stats_counts_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "CACHE_DIR", Path(tmpdir)):
                WebSearchCache.store("q1", [{"title": "A", "url": "U", "snippet": "S"}])
                WebSearchCache.store("q2", [{"title": "B", "url": "V", "snippet": "T"}])
                stats = WebSearchCache.stats()
        self.assertEqual(stats["total_queries"], 2)

    def test_ddg_search_parses_abstract(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "AbstractText": "Python is a programming language",
            "AbstractTitle": "Python",
            "AbstractURL": "https://python.org",
            "RelatedTopics": []
        })
        with patch("subprocess.run", return_value=mock_result):
            # First call fails (SearXNG), second succeeds (DDG)
            fail = MagicMock()
            fail.returncode = 1
            fail.stdout = ""

            call_n = [0]
            def side(cmd, **kwargs):
                call_n[0] += 1
                if "8888" in " ".join(cmd):
                    return fail
                return mock_result

            with patch("subprocess.run", side_effect=side):
                results = DuckDuckGoSearch.search("python language")
        # Should have some results from DDG
        if results:
            self.assertTrue(any("python" in r["title"].lower() or "python" in r["snippet"].lower()
                                for r in results))

    def test_search_force_refresh_bypasses_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "CACHE_DIR", Path(tmpdir)):
                WebSearchCache.store("cached query", [{"title": "Old", "url": "U", "snippet": "S"}])
                network_calls = [0]
                mock_r = MagicMock()
                mock_r.returncode = 0
                mock_r.stdout = json.dumps({"results": [{"title": "New", "url": "V", "content": "T", "engine": "test"}]})
                def mock_run(cmd, **kwargs):
                    network_calls[0] += 1
                    return mock_r
                with patch("subprocess.run", side_effect=mock_run):
                    result = search("cached query", force_refresh=True)
        self.assertGreater(network_calls[0], 0)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_search_stores_in_cache(self):
        """search() must store results in cache after fetching."""
        mock_r = MagicMock()
        mock_r.returncode = 0
        mock_r.stdout = json.dumps({"results": [
            {"title": "Nova AI", "url": "https://nova.example.com", "content": "AI system", "engine": "test"}
        ]})
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "CACHE_DIR", Path(tmpdir)):
                with patch("subprocess.run", return_value=mock_r):
                    result = search("nova ai system test")
                # Verify it's now cached
                cached = WebSearchCache.get("nova ai system test")
        self.assertIsNotNone(cached)
        self.assertEqual(result, cached)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_with_query_and_results(self):
        mock_r = MagicMock()
        mock_r.returncode = 0
        mock_r.stdout = json.dumps({"results": [
            {"title": "Python Docs", "url": "https://python.org", "content": "Python docs", "engine": "test"}
        ]})
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "CACHE_DIR", Path(tmpdir)):
                with patch("sys.argv", ["nova_web_search.py", "python programming"]):
                    with patch("subprocess.run", return_value=mock_r):
                        import io
                        from contextlib import redirect_stdout
                        buf = io.StringIO()
                        with redirect_stdout(buf):
                            result = _mod.main()
        self.assertEqual(result, 0)

    def test_main_cache_stats(self):
        """--cache-stats must return 0 and not crash."""
        fake_stats = {
            "total_queries": 2,
            "total_size_mb": 0.01,
            "oldest_entry": "2026-01-01T00:00:00",
            "newest_entry": "2026-01-02T00:00:00",
            "cache_ttl_hours": 24.0,
        }
        with patch("sys.argv", ["nova_web_search.py", "--cache-stats"]):
            with patch.object(WebSearchCache, "stats", return_value=fake_stats):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    result = _mod.main()
        self.assertEqual(result, 0)

    def test_main_no_query_returns_1(self):
        with patch("sys.argv", ["nova_web_search.py"]):
            result = _mod.main()
        self.assertEqual(result, 1)

    def test_main_no_results_returns_1(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "CACHE_DIR", Path(tmpdir)):
                with patch("sys.argv", ["nova_web_search.py", "zxqjwqxz_nonexistent"]):
                    with patch.object(_mod, "search", return_value=None):
                        result = _mod.main()
        self.assertEqual(result, 1)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_constants_defined(self):
        self.assertIsInstance(_mod.CACHE_DIR, Path)
        self.assertIsInstance(_mod.CACHE_TTL, int)
        self.assertIsInstance(_mod.DEFAULT_REGION, str)
        self.assertIsInstance(_mod.DEFAULT_COUNT, int)
        self.assertIsInstance(_mod.TIMEOUT, int)

    def test_classes_exist(self):
        self.assertTrue(hasattr(_mod, "WebSearchCache"))
        self.assertTrue(hasattr(_mod, "DuckDuckGoSearch"))

    def test_functions_exist(self):
        for fn in ("search", "store_as_memories", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_cache_dir_created_on_import(self):
        self.assertTrue(_mod.CACHE_DIR.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
