"""
test_nova_daily_opinion.py — All 7 test categories for nova_daily_opinion.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_daily_opinion.py"
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

with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
    _mod = load_script_compat(_SCRIPT, "nova_daily_opinion")

fetch_news = _mod.fetch_news
load_state = _mod.load_state
save_state = _mod.save_state
get_openrouter_key = _mod.get_openrouter_key


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pat, src, f"Credential: {pat!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(p, src)

    def test_api_key_uses_keychain(self):
        src = _SCRIPT.read_text()
        self.assertIn("find-generic-password", src)
        self.assertIn("nova-openrouter-api-key", src)

    def test_keychain_raises_on_missing_key(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
            with self.assertRaises(RuntimeError):
                get_openrouter_key()


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_fetch_news_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)

    def test_load_state_fast(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            state_data = {"recent_stories": list(range(30)), "opinion_count": 10}
            Path(f.name).write_text(json.dumps(state_data))
            tmp = Path(f.name)
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

    def test_fetch_news_returns_empty_on_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("no network")):
            result = fetch_news()
        self.assertIsInstance(result, list)

    def test_generate_opinion_falls_back_to_ollama(self):
        ollama_calls = []
        def fake_ollama(*args, **kw):
            ollama_calls.append(1)
            return "Opinion text " * 60

        story = {"title": "Test Story", "description": "Details", "link": "http://x.com"}
        with patch.object(_mod, "get_openrouter_key", side_effect=RuntimeError("no key")):
            with patch.object(_mod, "_generate_via_ollama", side_effect=fake_ollama):
                result = _mod.generate_opinion(story, [])
        self.assertGreater(len(ollama_calls), 0)

    def test_main_aborts_gracefully_on_no_news(self):
        with patch.object(_mod, "fetch_news", return_value=[]):
            with patch.object(_mod, "load_state", return_value={"recent_stories": [], "opinion_count": 0}):
                _mod.main()  # should not raise


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_load_state_defaults_on_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(_mod, "STATE_FILE", Path(tmp) / "missing.json"):
                state = load_state()
        self.assertIn("recent_stories", state)
        self.assertIn("opinion_count", state)

    def test_save_load_roundtrip(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            state = {"recent_stories": ["story1"], "opinion_count": 5}
            with patch.object(_mod, "STATE_FILE", tmp):
                save_state(state)
                loaded = load_state()
            self.assertEqual(loaded["opinion_count"], 5)
        finally:
            tmp.unlink(missing_ok=True)

    def test_fetch_news_parses_rss(self):
        rss_xml = b"""<?xml version="1.0"?><rss version="2.0"><channel>
            <item><title>News Title</title><description>Desc</description><link>http://x.com</link></item>
        </channel></rss>"""
        fake_resp = MagicMock()
        fake_resp.read.return_value = rss_xml
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=fake_resp):
            stories = fetch_news()
        self.assertGreater(len(stories), 0)
        self.assertIn("title", stories[0])

    def test_pick_story_avoids_recent(self):
        stories = [
            {"title": "Old Story", "description": "desc", "link": "http://a.com"},
            {"title": "Fresh Story", "description": "desc", "link": "http://b.com"},
        ]
        state = {"recent_stories": ["Old Story"]}
        result = _mod.pick_story(stories, state)
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Fresh Story")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_updates_state_on_success(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
            tmp.write_text(json.dumps({"recent_stories": [], "opinion_count": 2}))
        story = {"title": "Test Story", "description": "Desc", "link": "http://x.com"}
        try:
            with patch.object(_mod, "STATE_FILE", tmp):
                with patch.object(_mod, "fetch_news", return_value=[story]):
                    with patch.object(_mod, "pick_story", return_value=story):
                        with patch.object(_mod, "recall_memories", return_value=[]):
                            with patch.object(_mod, "generate_opinion",
                                              return_value="Opinion text " * 60):
                                with patch.object(_mod, "generate_opinion_image", return_value=None):
                                    with patch.object(_mod, "send_to_herd"):
                                        with patch.object(_mod, "post_to_slack"):
                                            with patch.object(_mod, "publish_to_journal"):
                                                _mod.main()
            state = json.loads(tmp.read_text())
            self.assertEqual(state["opinion_count"], 3)
        finally:
            tmp.unlink(missing_ok=True)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_post_to_slack_includes_story_title(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        story = {"title": "Big Important Story", "link": "http://x.com"}
        _mod.post_to_slack("My opinion " * 20, story)
        combined = " ".join(posts)
        self.assertIn("Big Important Story", combined)
        _nova_cfg.post_both.side_effect = None

    def test_opinion_too_short_aborts(self):
        story = {"title": "Test", "description": "Desc", "link": "http://x.com"}
        with patch.object(_mod, "fetch_news", return_value=[story]):
            with patch.object(_mod, "pick_story", return_value=story):
                with patch.object(_mod, "recall_memories", return_value=[]):
                    with patch.object(_mod, "generate_opinion", return_value="short"):
                        with patch.object(_mod, "send_to_herd") as herd_mock:
                            with patch.object(_mod, "load_state",
                                              return_value={"recent_stories": [], "opinion_count": 0}):
                                with patch.object(_mod, "save_state"):
                                    _mod.main()
                        herd_mock.assert_not_called()


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
        for fn in ["main", "fetch_news", "pick_story", "recall_memories",
                   "generate_opinion", "post_to_slack", "load_state", "save_state"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.NEWS_RSS)
        self.assertIn("google", _mod.NEWS_RSS)
        self.assertGreater(_mod.MEMORY_COUNT, 0)

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
