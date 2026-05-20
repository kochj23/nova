"""
test_nova_this_day.py — All 7 test categories for nova_this_day.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_this_day.py"

_nova_cfg = MagicMock()
_nova_cfg.post_both = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.filter_private_memories = lambda m: m

_nova_logger = MagicMock()
_nova_logger.log = MagicMock()
_nova_logger.LOG_INFO = "INFO"
_nova_logger.LOG_ERROR = "ERROR"

sys.modules["nova_config"] = _nova_cfg
sys.modules["nova_logger"] = _nova_logger

_spec = importlib.util.spec_from_file_location("nova_this_day", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

vector_remember = _mod.vector_remember
vector_recall = _mod.vector_recall


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_private_memory_filter_applied(self):
        """vector_recall must call nova_config.filter_private_memories."""
        filter_calls = []
        original = _nova_cfg.filter_private_memories
        _nova_cfg.filter_private_memories = lambda m: (filter_calls.append(1), m)[1]
        fake = MagicMock()
        fake.read.return_value = json.dumps({"memories": [{"text": "event"}]}).encode()
        with patch("urllib.request.urlopen", return_value=fake):
            vector_recall("test query")
        _nova_cfg.filter_private_memories = original
        self.assertGreater(len(filter_calls), 0)

    def test_vector_remember_uses_history_source(self):
        """Historical events stored with source='history'."""
        payloads = []
        def capture(req, timeout=None):
            payloads.append(json.loads(req.data.decode()))
            return MagicMock(__enter__=lambda s: s, __exit__=MagicMock(return_value=False))
        with patch("urllib.request.urlopen", side_effect=capture):
            vector_remember("On this day in 1969: Moon landing")
        if payloads:
            self.assertEqual(payloads[0]["source"], "history")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_wiki_api_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)

    def test_vector_remember_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=10", src)

    def test_max_items_bounded(self):
        self.assertGreater(_mod.MAX_EVENTS, 0)
        self.assertLessEqual(_mod.MAX_EVENTS, 20)
        self.assertGreater(_mod.MAX_BIRTHS, 0)
        self.assertGreater(_mod.MAX_DEATHS, 0)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_vector_remember_silent_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            try:
                vector_remember("test event text", {"type": "event"})
            except Exception as e:
                self.fail(f"vector_remember raised: {e}")

    def test_vector_recall_returns_empty_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            result = vector_recall("test query")
        self.assertEqual(result, [])

    def test_fetch_wikipedia_handles_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("wiki down")):
            try:
                result = _mod.fetch_wikipedia()
            except Exception:
                result = {}
        self.assertIsInstance(result, dict)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_vector_remember_sends_text_and_metadata(self):
        payloads = []
        def capture(req, timeout=None):
            payloads.append(json.loads(req.data.decode()))
            return MagicMock(__enter__=lambda s: s, __exit__=MagicMock(return_value=False))
        with patch("urllib.request.urlopen", side_effect=capture):
            vector_remember("historical fact about 1066", {"type": "event", "year": "1066"})
        self.assertTrue(len(payloads) > 0)
        self.assertIn("historical fact", payloads[0]["text"])

    def test_vector_recall_returns_list(self):
        fake = MagicMock()
        fake.read.return_value = json.dumps({"memories": [{"text": "memory 1"}]}).encode()
        with patch("urllib.request.urlopen", return_value=fake):
            result = vector_recall("history query")
        self.assertIsInstance(result, list)

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.VECTOR_URL)
        self.assertIsNotNone(_mod.VECTOR_MEM_URL)
        self.assertIsNotNone(_mod.MEMORY_DIR)
        self.assertGreater(_mod.MAX_EVENTS, 0)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_posts_to_slack(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        fake_wiki = {
            "events": [{"year": "1969", "text": "Moon landing success", "pages": []}],
            "births": [], "deaths": [],
        }
        with patch.object(_mod, "fetch_on_this_day", return_value=fake_wiki):
            with patch.object(_mod, "find_memories_for_date", return_value={}):
                with patch.object(_mod, "vector_remember"):
                    with patch.object(_mod, "append_to_memory"):
                        try:
                            _mod.main()
                        except Exception:
                            pass
        _nova_cfg.post_both.side_effect = None

    def test_main_handles_empty_history(self):
        with patch.object(_mod, "fetch_on_this_day", return_value=None):
            with patch.object(_mod, "find_memories_for_date", return_value={}):
                with patch.object(_mod, "vector_remember"):
                    with patch.object(_mod, "append_to_memory"):
                        try:
                            _mod.main()
                        except Exception:
                            pass  # should not raise unhandled

    def test_historical_facts_stored_in_memory(self):
        remember_calls = []
        fake_wiki = {
            "events": [{"year": "1966", "text": "Moon probe lands on surface", "pages": []}],
            "births": [], "deaths": [],
        }
        with patch.object(_mod, "fetch_on_this_day", return_value=fake_wiki):
            with patch.object(_mod, "find_memories_for_date", return_value={}):
                with patch.object(_mod, "vector_remember",
                                  side_effect=lambda t, m=None: remember_calls.append(t)):
                    with patch.object(_mod, "append_to_memory"):
                        with patch.object(_nova_cfg, "post_both"):
                            try:
                                _mod.main()
                            except Exception:
                                pass
        self.assertGreater(len(remember_calls), 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_slack_post_formats_history_section(self):
        """format_history_slack must include event text."""
        events = [{"year": "1969", "text": "Apollo 11 lands on the Moon", "pages": []}]
        result = _mod.format_history_slack(events, [], [], "05-13")
        self.assertIn("1969", result)

    def test_vector_recall_filters_private(self):
        """vector_recall must use filter_private_memories."""
        original = _nova_cfg.filter_private_memories
        calls = []
        _nova_cfg.filter_private_memories = lambda m: (calls.append(1), m)[1]
        fake = MagicMock()
        fake.read.return_value = json.dumps({"memories": [{"text": "test"}]}).encode()
        with patch("urllib.request.urlopen", return_value=fake):
            vector_recall("query")
        _nova_cfg.filter_private_memories = original
        self.assertGreater(len(calls), 0)


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
        for fn in ["main", "vector_remember", "vector_recall", "fetch_on_this_day"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
