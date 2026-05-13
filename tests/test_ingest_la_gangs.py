"""
test_ingest_la_gangs.py — All 7 test categories for ingest_la_gangs.py
Written by Jordan Koch.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

import importlib.util
_SCRIPT = Path(__file__).parent.parent / "scripts" / "ingest_la_gangs.py"
_spec = importlib.util.spec_from_file_location("ingest_la_gangs", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(pattern, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_memory_url_localhost(self):
        self.assertIn("127.0.0.1", _mod.MEMORY_URL)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pattern in ["kochjpar" + _at + "gmail.com"]:
            self.assertNotIn(pattern, src)

    def test_metadata_type_specified(self):
        """Metadata must classify data type for privacy separation."""
        src = _SCRIPT.read_text()
        self.assertIn("gang_intelligence", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_facts_count_reasonable(self):
        self.assertGreater(len(_mod.FACTS), 10)
        self.assertLessEqual(len(_mod.FACTS), 2000)

    def test_remember_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=10", src)

    def test_progress_pause_present(self):
        """Script must pause periodically to avoid hammering server."""
        src = _SCRIPT.read_text()
        self.assertIn("time.sleep", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_silent_on_exception(self):
        def fail(*args, **kwargs):
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", side_effect=fail):
            result = _mod.remember("Test gang fact.")
        self.assertFalse(result)

    def test_exception_logged_to_stderr(self):
        src = _SCRIPT.read_text()
        self.assertIn("stderr", src,
                      "Errors should be logged to stderr")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_facts_are_strings(self):
        for fact in _mod.FACTS:
            self.assertIsInstance(fact, str)
            self.assertGreater(len(fact), 20)

    def test_remember_posts_json(self):
        posted = []

        def capture(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=capture):
            _mod.remember("Test gang fact here.")

        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["source"], "local_knowledge")
        self.assertEqual(posted[0]["metadata"]["type"], "gang_intelligence")
        self.assertEqual(posted[0]["metadata"]["region"], "los_angeles")

    def test_main_function_exists(self):
        self.assertTrue(callable(_mod.main))

    def test_main_guard_present(self):
        src = _SCRIPT.read_text()
        self.assertIn('if __name__ == "__main__"', src)

    def test_la_references_in_facts(self):
        """Facts should reference LA-area locations."""
        fact_text = " ".join(_mod.FACTS[:50])
        self.assertTrue(
            any(loc in fact_text for loc in ["Los Angeles", "LAPD", "Compton", "Watts", "Valley"]),
            "Facts must reference LA area"
        )


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_ingests_all_facts(self):
        posted = [0]

        def mock_remember(text):
            posted[0] += 1
            return True

        with patch.object(_mod, "remember", side_effect=mock_remember):
            with patch("time.sleep"):
                _mod.main()

        self.assertEqual(posted[0], len(_mod.FACTS),
                         f"main() must attempt to ingest all {len(_mod.FACTS)} facts")

    def test_progress_reported_every_100(self):
        src = _SCRIPT.read_text()
        self.assertIn("% 100 == 0", src,
                      "Progress must be reported every 100 facts")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_output_includes_completion_summary(self):
        """main() must print completion summary."""
        src = _SCRIPT.read_text()
        self.assertIn("Complete", src)

    def test_facts_cover_multiple_regions(self):
        """FACTS must cover multiple LA-area regions."""
        fact_text = " ".join(_mod.FACTS)
        regions = ["Valley", "Compton", "East LA", "Inglewood", "Long Beach", "South"]
        covered = [r for r in regions if r in fact_text]
        self.assertGreater(len(covered), 3,
                           f"Facts should cover multiple LA regions, found: {covered}")

    def test_factual_content_not_empty(self):
        for fact in _mod.FACTS:
            self.assertGreater(len(fact.strip()), 0)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"ingest_la_gangs.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_facts_list_defined(self):
        self.assertTrue(hasattr(_mod, "FACTS"))
        self.assertIsInstance(_mod.FACTS, list)

    def test_remember_function_defined(self):
        self.assertTrue(callable(_mod.remember))


if __name__ == "__main__":
    unittest.main(verbosity=2)
