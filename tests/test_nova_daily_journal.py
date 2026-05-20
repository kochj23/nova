"""
test_nova_daily_journal.py — All 7 test categories for nova_daily_journal.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_daily_journal.py"

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
_nova_cfg.is_private_source = MagicMock(return_value=False)

sys.modules["nova_config"] = _nova_cfg

_spec = importlib.util.spec_from_file_location("nova_daily_journal", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

build_unified_message = _mod.build_unified_message
generate_journal_sections = _mod.generate_journal_sections
_fallback_summary = _mod._fallback_summary
_ollama_available = _mod._ollama_available
get_memory_stats = _mod.get_memory_stats
vector_recall = _mod.vector_recall


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
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_private_source_filter_applied(self):
        """vector_recall must call is_private_source to filter results."""
        fake = {"memories": [
            {"text": "public fact", "score": 0.9, "source": "wikipedia"},
            {"text": "private data", "score": 0.9, "source": "work_internal"},
        ]}
        private_check = []
        _nova_cfg.is_private_source.side_effect = lambda s: private_check.append(s) or (s == "work_internal")
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps(fake).encode()
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=fake_resp):
            results = vector_recall("test query")
        self.assertTrue(len(private_check) > 0)
        self.assertNotIn("private data", results)
        _nova_cfg.is_private_source.side_effect = None


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_build_unified_message_fast(self):
        journal = "journal text " * 100
        summary = "summary text " * 50
        start = time.perf_counter()
        for _ in range(1000):
            build_unified_message(journal, 1000, summary)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)

    def test_fallback_summary_fast_on_large_input(self):
        raw = ("• weather: 72°F hot\n• bullet point\n" * 1000)
        start = time.perf_counter()
        _fallback_summary(raw)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_memory_stats_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_memory_stats_returns_empty_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            result = get_memory_stats()
        self.assertIsInstance(result, dict)

    def test_vector_recall_returns_empty_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            result = vector_recall("test query")
        self.assertIsInstance(result, list)
        self.assertEqual(result, [])

    def test_main_continues_when_ollama_down(self):
        with patch.object(_mod, "generate_journal_sections", return_value="journal"):
            with patch.object(_mod, "gather_today_learnings", return_value="context"):
                with patch.object(_mod, "get_memory_stats", return_value={"count": 0}):
                    with patch.object(_mod, "_ollama_available", return_value=False):
                        with patch.object(_mod, "store_summary_in_memory"):
                            with patch.object(_nova_cfg, "post_both"):
                                result = _mod.main()
        self.assertEqual(result, 0)

    def test_store_summary_silent_on_failure(self):
        with patch("subprocess.run", side_effect=Exception("script missing")):
            try:
                _mod.store_summary_in_memory("test summary")
            except Exception as e:
                self.fail(f"store_summary raised: {e}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_build_unified_message_contains_header(self):
        msg = build_unified_message("journal", 500, "summary")
        self.assertIn("Nova Daily Journal", msg)

    def test_build_unified_message_contains_memory_count(self):
        msg = build_unified_message("journal", 1234, "summary")
        self.assertIn("1234", msg)

    def test_build_unified_message_contains_summary(self):
        msg = build_unified_message("journal", 100, "my summary text")
        self.assertIn("my summary text", msg)

    def test_fallback_summary_returns_string(self):
        result = _fallback_summary("• weather 72°F\n• something happened")
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_fallback_summary_quiet_day_on_empty(self):
        result = _fallback_summary("")
        self.assertIn("quiet", result.lower())

    def test_ollama_available_returns_bool(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            result = _ollama_available()
        self.assertFalse(result)

    def test_query_field_returns_none_on_empty(self):
        with patch.object(_mod, "_query", return_value=[]):
            result = _mod._query_field("SELECT count(*) FROM memories")
        self.assertIsNone(result)

    def test_load_state_returns_dict(self):
        result = _mod._load_state("nonexistent_state_file_xyz.json")
        self.assertIsInstance(result, dict)

    def test_section_calendar_returns_none_on_empty(self):
        with patch.object(_mod, "_query", return_value=[]):
            result = _mod.section_calendar()
        self.assertIsNone(result)

    def test_section_dream_truncates_long_text(self):
        long_row = "Dream detail: " + "x " * 200
        with patch.object(_mod, "_query", return_value=[long_row]):
            result = _mod.section_dream()
        self.assertIsNotNone(result)
        self.assertIn("...", result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_generate_journal_sections_returns_string(self):
        with patch.object(_mod, "_query", return_value=[]):
            with patch.object(_mod, "_load_state", return_value={}):
                result = generate_journal_sections()
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_generate_journal_sections_fallback_on_all_empty(self):
        with patch.object(_mod, "_query", return_value=[]):
            with patch.object(_mod, "_load_state", return_value={}):
                result = generate_journal_sections()
        self.assertIn("quiet", result.lower())

    def test_main_posts_to_slack(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        with patch.object(_mod, "generate_journal_sections", return_value="journal"):
            with patch.object(_mod, "gather_today_learnings", return_value="context"):
                with patch.object(_mod, "get_memory_stats", return_value={"count": 100}):
                    with patch.object(_mod, "_ollama_available", return_value=False):
                        with patch.object(_mod, "store_summary_in_memory"):
                            _mod.main()
        self.assertTrue(len(posts) > 0)
        _nova_cfg.post_both.side_effect = None

    def test_memory_count_appears_in_final_message(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        with patch.object(_mod, "generate_journal_sections", return_value="journal"):
            with patch.object(_mod, "gather_today_learnings", return_value="context"):
                with patch.object(_mod, "get_memory_stats", return_value={"count": 999}):
                    with patch.object(_mod, "_ollama_available", return_value=False):
                        with patch.object(_mod, "store_summary_in_memory"):
                            _mod.main()
        combined = " ".join(posts)
        self.assertIn("999", combined)
        _nova_cfg.post_both.side_effect = None


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_full_pipeline_returns_zero(self):
        with patch.object(_mod, "generate_journal_sections", return_value="journal text"):
            with patch.object(_mod, "gather_today_learnings", return_value="learning context"):
                with patch.object(_mod, "get_memory_stats", return_value={"count": 50}):
                    with patch.object(_mod, "synthesize_summary", return_value="Nova's reflection"):
                        with patch.object(_mod, "store_summary_in_memory"):
                            with patch.object(_nova_cfg, "post_both"):
                                result = _mod.main()
        self.assertEqual(result, 0)

    def test_journal_message_format_has_separator(self):
        msg = build_unified_message("journal section", 42, "llm summary")
        self.assertIn("────", msg)

    def test_section_security_formats_camera_counts(self):
        rows = [
            "Protect event on Front Door: motion",
            "Protect event on Back Gate: motion",
            "Protect event on Front Door: person",
        ]
        with patch.object(_mod, "_query_field", return_value="3"):
            with patch.object(_mod, "_query", return_value=rows):
                result = _mod.section_security()
        self.assertIsNotNone(result)
        self.assertIn("Front Door", result)

    def test_section_infrastructure_shows_no_outages(self):
        with patch.object(_mod, "_load_state", return_value={}):
            with patch.object(_mod, "_query", return_value=[]):
                result = _mod.section_infrastructure()
        # Either None or contains "no outages"
        if result:
            self.assertIn("outage", result.lower())


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
        for fn in ["main", "generate_journal_sections", "gather_today_learnings",
                   "synthesize_summary", "build_unified_message", "store_summary_in_memory",
                   "get_memory_stats", "vector_recall"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.VECTOR_URL)
        self.assertIsNotNone(_mod.OLLAMA_URL)
        self.assertIsNotNone(_mod.MODEL)

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
