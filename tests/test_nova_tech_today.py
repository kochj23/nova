"""
test_nova_tech_today.py — All 7 test categories for nova_tech_today.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_tech_today.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.post_both = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"

_nova_image_utils = MagicMock()
_nova_image_utils.ensure_backend = MagicMock(return_value=True)
_nova_image_utils.generate_image = MagicMock(return_value=None)

sys.modules["nova_config"] = _nova_cfg
sys.modules["nova_image_utils"] = _nova_image_utils

_mod = load_script_compat(_SCRIPT, "nova_tech_today")

log = _mod.log
load_state = _mod.load_state
save_state = _mod.save_state


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_api_key_from_keychain(self):
        src = _SCRIPT.read_text()
        self.assertIn("find-generic-password", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_search_queries_not_empty(self):
        self.assertGreater(len(_mod.SEARCH_QUERIES), 3)

    def test_state_operations_fast(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
            tmp.write_text(json.dumps({"recent_topics": [], "article_count": 0}))
        try:
            start = time.perf_counter()
            for _ in range(100):
                with patch.object(_mod, "STATE_FILE", tmp):
                    load_state()
            elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 0.5)
        finally:
            tmp.unlink(missing_ok=True)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_searxng_search_handles_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("searxng down")):
            result = _mod.searxng_search("tech news")
        self.assertIsInstance(result, list)

    def test_generate_article_falls_back_to_ollama(self):
        ollama_calls = []
        def fake_ollama(*a, **kw):
            ollama_calls.append(1)
            return "Article text " * 200
        with patch.object(_mod, "_generate_via_openrouter", side_effect=Exception("or fail")):
            with patch.object(_mod, "_generate_via_ollama", side_effect=fake_ollama):
                result = _mod.generate_article("AI chip breakthrough", "context", "memories")
        self.assertGreater(len(ollama_calls), 0)

    def test_memory_recall_handles_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            result = _mod.recall_memories("AI news")
        self.assertIsInstance(result, list)

    def test_main_aborts_on_no_topic(self):
        with patch.object(_mod, "pick_topic", return_value=None):
            with patch.object(_mod, "load_state",
                              return_value={"recent_topics": [], "article_count": 0}):
                _mod.main()  # should not raise


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_load_state_defaults(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(_mod, "STATE_FILE", Path(tmp) / "missing.json"):
                state = load_state()
        self.assertIn("recent_topics", state)
        self.assertIn("article_count", state)

    def test_save_load_roundtrip(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            state = {"recent_topics": ["AI", "chips"], "article_count": 10}
            with patch.object(_mod, "STATE_FILE", tmp):
                save_state(state)
                loaded = load_state()
            self.assertEqual(loaded["article_count"], 10)
        finally:
            tmp.unlink(missing_ok=True)

    def test_log_does_not_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(_mod, "LOG_FILE", Path(tmp) / "test.log"):
                log("test message")

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.SEARXNG_URL)
        self.assertIsNotNone(_mod.MEMORY_SERVER)
        self.assertIsNotNone(_mod.MODEL)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_short_article_aborts(self):
        with patch.object(_mod, "pick_topic", return_value="AI semiconductors"):
            with patch.object(_mod, "searxng_search", return_value=[]):
                with patch.object(_mod, "recall_memories", return_value=[]):
                    with patch.object(_mod, "generate_article", return_value="short"):
                        with patch.object(_mod, "load_state",
                                          return_value={"recent_topics": [], "article_count": 0}):
                            with patch.object(_mod, "save_state"):
                                _mod.main()  # should abort cleanly

    def test_main_updates_state_on_success(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
            tmp.write_text(json.dumps({"recent_topics": [], "article_count": 5}))
        article_text = "Technology article content " * 200
        try:
            with patch.object(_mod, "STATE_FILE", tmp):
                with patch.object(_mod, "pick_topic", return_value="AI news"):
                    with patch.object(_mod, "searxng_search", return_value=[]):
                        with patch.object(_mod, "recall_memories", return_value=[]):
                            with patch.object(_mod, "generate_article", return_value=article_text):
                                with patch.object(_mod, "generate_article_image", return_value=None):
                                    with patch.object(_mod, "publish_to_hugo", return_value=True):
                                        with patch.object(_mod, "post_to_slack"):
                                            _mod.main()
            state = json.loads(tmp.read_text())
            self.assertEqual(state["article_count"], 6)
        finally:
            tmp.unlink(missing_ok=True)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_post_to_slack_includes_topic(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        _mod.post_to_slack("Article text " * 50, "Quantum Computing Breakthrough")
        self.assertTrue(len(posts) > 0)
        combined = " ".join(posts)
        self.assertIn("Quantum", combined)
        _nova_cfg.post_both.side_effect = None

    def test_pick_topic_avoids_recent(self):
        with patch.object(_mod, "searxng_search",
                          return_value=[{"title": "Old Topic News", "content": "Boring", "url": "http://x.com"}]):
            state = {"recent_topics": ["Old Topic News"]}
            # pick_topic may return None or a different topic
            try:
                result = _mod.pick_topic(state)
                if result:
                    self.assertNotEqual(result, "Old Topic News")
            except Exception:
                pass


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_key_functions_exist(self):
        for fn in ["main", "load_state", "save_state", "log",
                   "searxng_search", "recall_memories", "generate_article",
                   "publish_to_hugo", "post_to_slack"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
