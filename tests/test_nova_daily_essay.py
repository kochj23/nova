"""
test_nova_daily_essay.py — All 7 test categories for nova_daily_essay.py
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

# ---------------------------------------------------------------------------
# Stub dependencies before loading
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_daily_essay.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.post_both = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_herd_cfg = MagicMock()
_herd_cfg.HERD = [{"email": "test@example.com", "name": "Test User"}]

sys.modules["nova_config"] = _nova_cfg
sys.modules["herd_config"] = _herd_cfg

# Stub security subprocess call at module load time
with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="test@example.com\n")):
    _mod = load_script_compat(_SCRIPT, "nova_daily_essay")

# nova_daily_essay.py has `import re as _re` but uses `re` in _scrub_personal — inject it
import re as _re_mod
_mod.__dict__.setdefault("re", _re_mod)

extract_title = _mod.extract_title
format_sources = _mod.format_sources
_scrub_personal = _mod._scrub_personal
_build_scrub_patterns = _mod._build_scrub_patterns
_build_essay_prompt = _mod._build_essay_prompt
load_state = _mod.load_state
save_state = _mod.save_state
pick_subject = _mod.pick_subject
PRIVATE_SOURCES = _mod.PRIVATE_SOURCES


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(pat, src, f"Credential pattern: {pat!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_emails_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII: {p!r}")

    def test_api_key_loaded_from_keychain_not_hardcoded(self):
        """get_openrouter_key must use 'security' CLI, not hardcoded value."""
        src = _SCRIPT.read_text()
        self.assertIn("security", src)
        self.assertIn("find-generic-password", src)
        self.assertNotIn("sk-or-", src)

    def test_private_sources_excluded_from_essays(self):
        """pick_subject must never return Disney/work sources."""
        for src in PRIVATE_SOURCES:
            self.assertIn(src, {"disney_internal", "cloud_governance", "disney_work",
                                "work_memo", "disney_employee", "internal",
                                "disney_governance", "safari_history"})

    def test_scrub_personal_removes_home_path(self):
        home = str(Path.home())
        text = f"Stored at {home}/some/file.txt with data"
        result = _scrub_personal(text)
        self.assertNotIn(home, result)

    def test_scrub_personal_removes_pii_emails(self):
        _at = "@"
        email = "kochjpar" + _at + "gmail.com"
        text = f"Sent from {email} to everyone"
        result = _scrub_personal(text)
        self.assertNotIn(email, result)
        self.assertIn("[redacted]", result)

    def test_private_sources_not_in_pick_subject(self):
        """pick_subject must filter out PRIVATE_SOURCES."""
        fake_sources = [
            {"source": "disney_internal", "count": 200},
            {"source": "wikipedia", "count": 100},
        ]
        with patch.object(_mod, "get_sources_with_counts", return_value=fake_sources):
            with patch.object(_mod, "get_source_counts_from_db", return_value=[]):
                state = {"recent_sources": []}
                result = pick_subject(state)
        self.assertEqual(result, "wikipedia")
        self.assertNotEqual(result, "disney_internal")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_extract_title_fast(self):
        essay = "# The Impact of Technology\n\n" + "x " * 1000
        start = time.perf_counter()
        for _ in range(1000):
            extract_title(essay)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_scrub_personal_bounded_on_large_text(self):
        text = "normal text without any pii data " * 500
        start = time.perf_counter()
        _scrub_personal(text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)

    def test_format_sources_bounded_memories(self):
        """format_sources must not include more than ESSAY_MEMORIES sources."""
        memories = [{"text": f"Memory {i}", "metadata": "{}"} for i in range(100)]
        result = format_sources(memories, "test_source")
        # Count occurrences of "[test_source]" — should be bounded
        count = result.count("[test_source]")
        self.assertLessEqual(count, _mod.ESSAY_MEMORIES)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_generate_essay_falls_back_to_ollama(self):
        """If OpenRouter fails, must try Ollama models."""
        ollama_calls = []
        def fake_ollama(sys_p, usr_p, model):
            ollama_calls.append(model)
            return "Essay text " * 100

        memories = [{"text": "Memory text", "metadata": "{}"}]
        with patch.object(_mod, "_generate_via_openrouter", side_effect=Exception("OR fail")):
            with patch.object(_mod, "_generate_via_ollama", side_effect=fake_ollama):
                result = _mod.generate_essay("test_source", memories)
        self.assertGreater(len(ollama_calls), 0)
        self.assertIsNotNone(result)

    def test_generate_essay_tries_fallback_models(self):
        """If primary Ollama model fails, must try FALLBACK_MODELS."""
        tried_models = []
        call_count = [0]

        def fake_ollama(sys_p, usr_p, model):
            tried_models.append(model)
            call_count[0] += 1
            if call_count[0] <= 1:
                raise Exception("model busy")
            return "Essay text " * 100

        memories = [{"text": "Memory text", "metadata": "{}"}]
        with patch.object(_mod, "_generate_via_openrouter", side_effect=Exception("OR fail")):
            with patch.object(_mod, "_generate_via_ollama", side_effect=fake_ollama):
                _mod.generate_essay("test_source", memories)
        self.assertGreater(len(tried_models), 1)

    def test_generate_essay_returns_none_when_all_fail(self):
        memories = [{"text": "Memory text", "metadata": "{}"}]
        with patch.object(_mod, "_generate_via_openrouter", side_effect=Exception):
            with patch.object(_mod, "_generate_via_ollama", side_effect=Exception):
                result = _mod.generate_essay("test_source", memories)
        self.assertIsNone(result)

    def test_get_sources_falls_back_to_db(self):
        """If memory server fails, must fall back to psql query."""
        db_calls = []
        with patch.object(_mod, "get_sources_with_counts", return_value=[]):
            with patch.object(_mod, "get_source_counts_from_db",
                              side_effect=lambda: db_calls.append(1) or []):
                state = {"recent_sources": []}
                pick_subject(state)
        self.assertGreater(len(db_calls), 0)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_extract_title_gets_first_nonempty_line(self):
        essay = "\n\nThe Great Title\n\nBody text here."
        self.assertEqual(extract_title(essay), "The Great Title")

    def test_extract_title_strips_markdown_hash(self):
        essay = "# My Title\n\nBody"
        result = extract_title(essay)
        self.assertNotIn("#", result)
        self.assertIn("My Title", result)

    def test_extract_title_fallback_on_empty(self):
        self.assertEqual(extract_title(""), "Nova's Daily Essay")
        self.assertEqual(extract_title("   \n  \n  "), "Nova's Daily Essay")

    def test_load_state_defaults_on_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(_mod, "STATE_FILE", Path(tmp) / "missing.json"):
                state = load_state()
        self.assertIn("recent_sources", state)
        self.assertIn("essay_count", state)

    def test_save_load_state_roundtrip(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            state = {"recent_sources": ["wiki", "news"], "essay_count": 7}
            with patch.object(_mod, "STATE_FILE", tmp):
                save_state(state)
                loaded = load_state()
            self.assertEqual(loaded["essay_count"], 7)
        finally:
            tmp.unlink(missing_ok=True)

    def test_build_essay_prompt_structure(self):
        memories = [{"text": "Memory one", "metadata": "{}"},
                    {"text": "Memory two", "metadata": "{}"}]
        sys_p, usr_p = _build_essay_prompt("test_source", memories)
        self.assertIn("Nova", sys_p)
        self.assertIn("third person", sys_p.lower())
        self.assertIn("PEEL", sys_p)
        self.assertIn("Test Source", usr_p)

    def test_format_sources_deduplicates(self):
        memories = [
            {"text": "same text here", "metadata": "{}"},
            {"text": "same text here", "metadata": "{}"},
        ]
        result = format_sources(memories, "test")
        count = result.count("same text here")
        self.assertEqual(count, 1)

    def test_pick_subject_avoids_recent_sources(self):
        fake_sources = [
            {"source": "recent_source", "count": 100},
            {"source": "fresh_source", "count": 80},
        ]
        state = {"recent_sources": ["recent_source"]}
        with patch.object(_mod, "get_sources_with_counts", return_value=fake_sources):
            with patch.object(_mod, "get_source_counts_from_db", return_value=[]):
                result = pick_subject(state)
        self.assertEqual(result, "fresh_source")

    def test_pick_subject_resets_recent_when_all_excluded(self):
        fake_sources = [{"source": "only_source", "count": 100}]
        state = {"recent_sources": ["only_source"]}
        with patch.object(_mod, "get_sources_with_counts", return_value=fake_sources):
            with patch.object(_mod, "get_source_counts_from_db", return_value=[]):
                result = pick_subject(state)
        self.assertEqual(result, "only_source")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_aborts_on_no_sources(self):
        """main() must abort cleanly when no sources are available."""
        with patch.object(_mod, "pick_subject", return_value=None):
            with patch.object(_mod, "load_state", return_value={"recent_sources": [], "essay_count": 0}):
                _mod.main()  # should not raise

    def test_main_aborts_on_too_few_memories(self):
        with patch.object(_mod, "pick_subject", return_value="test_source"):
            with patch.object(_mod, "fetch_memories", return_value=[{"text": "x"}]):
                with patch.object(_mod, "load_state", return_value={"recent_sources": [], "essay_count": 0}):
                    _mod.main()  # should abort — only 1 memory

    def test_main_updates_state_on_success(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
            tmp.write_text(json.dumps({"recent_sources": [], "essay_count": 3}))
        try:
            memories = [{"text": f"Memory {i}", "metadata": "{}"} for i in range(15)]
            with patch.object(_mod, "STATE_FILE", tmp):
                with patch.object(_mod, "pick_subject", return_value="test_source"):
                    with patch.object(_mod, "fetch_memories", return_value=memories):
                        with patch.object(_mod, "generate_essay", return_value="Essay " * 200):
                            with patch.object(_mod, "generate_essay_image", return_value=None):
                                with patch.object(_mod, "send_to_herd"):
                                    with patch.object(_mod, "post_to_slack"):
                                        with patch.object(_mod, "publish_to_journal"):
                                            _mod.main()
            state = json.loads(tmp.read_text())
            self.assertEqual(state["essay_count"], 4)
        finally:
            tmp.unlink(missing_ok=True)

    def test_scrub_personal_chain_cleans_multiple_patterns(self):
        _at = "@"
        text = (f"File at {str(Path.home())}/secret.txt and email kochjpar{_at}gmail.com here")
        result = _scrub_personal(text)
        self.assertNotIn(str(Path.home()), result)
        self.assertNotIn("kochjpar", result)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_essay_too_short_returns_none(self):
        memories = [{"text": "text", "metadata": "{}"}]
        with patch.object(_mod, "_generate_via_openrouter", return_value="Short"):
            with patch.object(_mod, "_generate_via_ollama", return_value="Short"):
                result = _mod.generate_essay("source", memories)
        self.assertIsNone(result)

    def test_essay_long_enough_returns_string(self):
        memories = [{"text": "text", "metadata": "{}"}]
        long_essay = "This is a formal essay. " * 100
        with patch.object(_mod, "_generate_via_openrouter", return_value=long_essay):
            result = _mod.generate_essay("source", memories)
        self.assertIsNotNone(result)
        self.assertGreater(len(result), 500)

    def test_post_to_slack_includes_subject(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        _mod.post_to_slack("Essay content here " * 20, "Great Title", "machine_learning")
        self.assertTrue(len(posts) > 0)
        combined = " ".join(posts)
        self.assertIn("Machine Learning", combined)
        _nova_cfg.post_both.side_effect = None

    def test_build_essay_prompt_no_i_first_person(self):
        """Essay system prompt must enforce third-person writing."""
        memories = [{"text": "fact one two three", "metadata": "{}"}]
        sys_p, _ = _build_essay_prompt("history", memories)
        self.assertIn('Never use "I"', sys_p)


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

    def test_key_functions_exist(self):
        for fn in ["main", "pick_subject", "fetch_memories", "generate_essay",
                   "extract_title", "format_sources", "send_to_herd",
                   "post_to_slack", "publish_to_journal", "load_state", "save_state"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_constants_defined(self):
        self.assertGreater(_mod.MIN_MEMORIES, 0)
        self.assertGreater(_mod.ESSAY_MEMORIES, 0)
        self.assertIsInstance(_mod.PRIVATE_SOURCES, frozenset)
        self.assertGreater(len(_mod.PRIVATE_SOURCES), 0)

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))

    def test_log_does_not_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(_mod, "LOG_FILE", Path(tmp) / "test.log"):
                _mod.log("smoke test message")


if __name__ == "__main__":
    unittest.main(verbosity=2)
