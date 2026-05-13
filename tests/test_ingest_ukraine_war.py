"""
test_ingest_ukraine_war.py — All 7 test categories for ingest_ukraine_war.py
Written by Jordan Koch.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

import importlib.util
_SCRIPT = (Path(__file__).parent.parent / "scripts" / "ingest_ukraine_war.py"
           if (Path(__file__).parent.parent / "scripts" / "ingest_ukraine_war.py").exists()
           else Path(__file__).parent.parent / "scripts" / "_archive" / "ingest_ukraine_war.py")
_spec = importlib.util.spec_from_file_location("ingest_ukraine_war", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_memory_url_localhost(self):
        self.assertIn("127.0.0.1", _mod.MEMORY_URL)

    def test_metadata_type_specified(self):
        src = _SCRIPT.read_text()
        self.assertIn("geopolitics", src)
        self.assertIn("russia_ukraine_war", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_facts_count_reasonable(self):
        self.assertGreater(len(_mod.FACTS), 100)
        self.assertLessEqual(len(_mod.FACTS), 5000)

    def test_remember_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=10", src)

    def test_progress_pause_present(self):
        src = _SCRIPT.read_text()
        self.assertIn("time.sleep(2)", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_silent_on_failure(self):
        def fail(*args, **kwargs):
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", side_effect=fail):
            result = _mod.remember("Test Ukraine war fact.")
        self.assertFalse(result)

    def test_failed_counter_tracked(self):
        src = _SCRIPT.read_text()
        self.assertIn("failed += 1", src)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_facts_are_strings(self):
        for fact in _mod.FACTS:
            self.assertIsInstance(fact, str)
            self.assertGreater(len(fact), 20)

    def test_remember_posts_correct_metadata(self):
        posted = []

        def capture(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=capture):
            _mod.remember("Russia invaded Ukraine on Feb 24, 2022.")

        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["source"], "local_knowledge")
        self.assertEqual(posted[0]["metadata"]["type"], "geopolitics")
        self.assertEqual(posted[0]["metadata"]["topic"], "russia_ukraine_war")

    def test_log_function_exists(self):
        self.assertTrue(callable(_mod.log))

    def test_slack_post_function_exists(self):
        self.assertTrue(callable(_mod.slack_post))

    def test_global_counters_initialized(self):
        self.assertTrue(hasattr(_mod, "count"))
        self.assertTrue(hasattr(_mod, "failed"))


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_facts_cover_key_events(self):
        """FACTS must cover key events of the war."""
        fact_text = " ".join(_mod.FACTS)
        key_events = ["Bucha", "Mariupol", "Kherson", "HIMARS", "Zelensky"]
        found = [e for e in key_events if e in fact_text]
        self.assertGreater(len(found), 3,
                           f"Expected key events in facts, found: {found}")

    def test_slack_update_every_250_facts(self):
        """Script must post Slack update every 250 facts."""
        src = _SCRIPT.read_text()
        self.assertIn("% 250 == 0", src)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_final_slack_post_present(self):
        src = _SCRIPT.read_text()
        self.assertIn("Ingestion Complete", src)

    def test_facts_include_date_references(self):
        """Facts must include dates for verifiability."""
        fact_text = " ".join(_mod.FACTS[:50])
        self.assertTrue(
            any(year in fact_text for year in ["2022", "2023", "2024"]),
            "Facts must reference years"
        )

    def test_sources_noted_in_docstring(self):
        """Script must note its sources in docstring."""
        src = _SCRIPT.read_text()
        self.assertIn("AP", src)
        self.assertIn("Reuters", src)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"ingest_ukraine_war.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_facts_list_defined(self):
        self.assertTrue(hasattr(_mod, "FACTS"))
        self.assertIsInstance(_mod.FACTS, list)

    def test_functions_defined(self):
        for fn in ["log", "slack_post", "remember"]:
            self.assertTrue(callable(getattr(_mod, fn, None)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
