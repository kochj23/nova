"""
test_ingest_liver_disease.py — All 7 test categories for ingest_liver_disease.py
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
_SCRIPT = Path(__file__).parent.parent / "scripts" / "ingest_liver_disease.py"
_spec = importlib.util.spec_from_file_location("ingest_liver_disease", _SCRIPT)
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

    def test_metadata_type_is_medical(self):
        """Medical metadata type must be specified for privacy/filtering."""
        src = _SCRIPT.read_text()
        self.assertIn("medical_liver_disease", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_all_facts_count_reasonable(self):
        total = len(_mod.ALL_FACTS)
        self.assertGreater(total, 100)
        self.assertLessEqual(total, 2000)

    def test_rate_limit_present(self):
        src = _SCRIPT.read_text()
        self.assertIn("time.sleep(0.05)", src,
                      "Rate limiting sleep must be present")

    def test_remember_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=10", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_silent_on_failure(self):
        def fail(*args, **kwargs):
            raise OSError("server down")

        with patch("urllib.request.urlopen", side_effect=fail):
            result = _mod.remember("Test liver fact.")
        self.assertFalse(result)

    def test_failure_logs_limited(self):
        """Script should not log every failure (limit to first 5)."""
        src = _SCRIPT.read_text()
        self.assertIn("failed <= 5", src,
                      "Failure logging should be limited")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_all_facts_is_concatenation(self):
        """ALL_FACTS must be sum of all category lists."""
        expected = (len(_mod.CIRRHOSIS_GENERAL) + len(_mod.LIVER_FAILURE) +
                    len(_mod.PARACENTESIS) + len(_mod.ORTHOSTATIC_HYPOTENSION))
        self.assertEqual(len(_mod.ALL_FACTS), expected)

    def test_category_lists_non_empty(self):
        for attr in ["CIRRHOSIS_GENERAL", "LIVER_FAILURE",
                     "PARACENTESIS", "ORTHOSTATIC_HYPOTENSION"]:
            lst = getattr(_mod, attr)
            self.assertGreater(len(lst), 0, f"{attr} must not be empty")

    def test_remember_posts_correct_metadata(self):
        posted = []

        def capture(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=capture):
            _mod.remember("Cirrhosis causes portal hypertension.")

        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["source"], "local_knowledge")
        self.assertIn("liver_failure", posted[0]["metadata"]["topics"])
        self.assertIn("cirrhosis", posted[0]["metadata"]["topics"])

    def test_log_function_exists(self):
        self.assertTrue(callable(_mod.log))

    def test_global_counters_defined(self):
        self.assertTrue(hasattr(_mod, "count"))
        self.assertTrue(hasattr(_mod, "failed"))


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_progress_reported(self):
        """Script must print progress every 50 facts."""
        src = _SCRIPT.read_text()
        self.assertIn("i % 50 == 0", src)

    def test_slack_notification_on_completion(self):
        """Script must notify via Slack when done."""
        src = _SCRIPT.read_text()
        self.assertIn("slack_post", src)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_cirrhosis_facts_have_medical_content(self):
        """Cirrhosis facts must contain medical terminology."""
        sample = " ".join(_mod.CIRRHOSIS_GENERAL[:10])
        self.assertTrue(
            any(term in sample for term in
                ["liver", "fibrosis", "hepatic", "portal", "albumin"]),
            "Cirrhosis facts must contain medical terms"
        )

    def test_paracentesis_facts_have_procedure_content(self):
        sample = " ".join(_mod.PARACENTESIS[:10])
        self.assertTrue(
            any(term in sample for term in
                ["paracentesis", "fluid", "ascites", "needle", "albumin"]),
            "Paracentesis facts must contain procedure terms"
        )

    def test_orthostatic_facts_defined(self):
        self.assertGreater(len(_mod.ORTHOSTATIC_HYPOTENSION), 0)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"ingest_liver_disease.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_main_guard_present(self):
        src = _SCRIPT.read_text()
        self.assertIn('if __name__ == "__main__"', src)

    def test_constants_present(self):
        for attr in ["MEMORY_URL", "ALL_FACTS", "CIRRHOSIS_GENERAL",
                     "LIVER_FAILURE", "PARACENTESIS", "ORTHOSTATIC_HYPOTENSION"]:
            self.assertTrue(hasattr(_mod, attr))


if __name__ == "__main__":
    unittest.main(verbosity=2)
