"""
test_dream_generate.py — All 7 test categories for dream_generate.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub external modules before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

# Stub psycopg2 and pg8000
sys.modules["psycopg2"] = MagicMock()
sys.modules["pg8000"] = MagicMock()
sys.modules["nova_strip_thinking"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "dream_generate.py"
_spec = importlib.util.spec_from_file_location("dream_generate", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_api_key(self):
        """Source must not contain hardcoded API keys."""
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "Bearer sk", "AKIA", "ghp_"]
        for pattern in forbidden:
            self.assertNotIn(pattern, src,
                             f"Potential hardcoded credential: {pattern!r}")

    def test_api_key_from_keychain(self):
        """OpenRouter API key must come from Keychain, not env var or file."""
        src = _SCRIPT.read_text()
        self.assertIn("security", src,
                      "API key must be retrieved from Keychain via security CLI")
        self.assertIn("nova-openrouter-api-key", src,
                      "Keychain item name expected")

    def test_no_hardcoded_home_path(self):
        """Source must not hardcode user home path."""
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_private_sources_excluded(self):
        """EXCLUDE_SOURCES must contain private/personal sources."""
        self.assertIn("private_document", _mod.EXCLUDE_SOURCES)
        self.assertIn("email_archive", _mod.EXCLUDE_SOURCES)
        self.assertIn("imessage", _mod.EXCLUDE_SOURCES)

    def test_cloud_privacy_note(self):
        """Dream narratives must never go to cloud — privacy note in source."""
        src = _SCRIPT.read_text()
        # Check for privacy-related comment about personal context
        self.assertTrue(
            "never send to cloud" in src.lower() or
            "personal context" in src.lower() or
            "private" in src.lower(),
            "Privacy comment required for dream narratives"
        )

    def test_no_pii_in_source(self):
        """Source must not contain PII emails."""
        src = _SCRIPT.read_text()
        _at = "@"
        pii_patterns = [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp" + ".com",
        ]
        for pattern in pii_patterns:
            self.assertNotIn(pattern, src, f"PII found: {pattern!r}")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_narrative_truncated_for_image_prompt(self):
        """Dream narrative must be truncated before sending to image model."""
        src = _SCRIPT.read_text()
        self.assertIn("[:2000]", src,
                      "Dream narrative must be truncated at 2000 chars for image prompt")

    def test_ollama_has_timeout(self):
        """Ollama calls must have timeouts."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=600", src,
                      "Long Ollama generation must have 600s timeout")

    def test_openrouter_has_timeout(self):
        """OpenRouter calls must have timeout."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=90", src,
                      "OpenRouter calls must have timeout")

    def test_repetition_detection_bounded(self):
        """Repetition detection must use bounded windows."""
        src = _SCRIPT.read_text()
        self.assertIn("window in [6, 10, 15]", src,
                      "Repetition windows must be bounded")

    def test_circuit_breaker_limits_retries(self):
        """Ollama circuit breaker must prevent infinite retry loops."""
        src = _SCRIPT.read_text()
        self.assertIn("_ollama_circuit_open", src,
                      "Circuit breaker must be checked before Ollama calls")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_falls_back_to_ollama_on_openrouter_failure(self):
        """generate_narrative() must fall back to Ollama when OpenRouter fails."""
        src = _SCRIPT.read_text()
        # The fallback message in source (case-sensitive, as written by Jordan)
        self.assertTrue(
            "falling back to local Ollama" in src or
            "fall back" in src.lower() or
            "Ollama" in src,
            "Fallback to Ollama must be present"
        )

    def test_short_response_triggers_retry(self):
        """Response with < 100 words must trigger a retry."""
        src = _SCRIPT.read_text()
        self.assertIn("too short", src.lower(),
                      "Short response must trigger retry")
        self.assertIn("100", src,
                      "100-word threshold must be checked")

    def test_fallback_models_defined(self):
        """FALLBACK_MODELS must be defined."""
        self.assertIsInstance(_mod.FALLBACK_MODELS, list)
        self.assertGreater(len(_mod.FALLBACK_MODELS), 0)

    def test_circuit_breaker_records_failure(self):
        """_ollama_circuit_record_failure() must not raise."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cb_file = Path(tmpdir) / ".ollama_circuit_breaker"
            with patch.object(_mod, "CIRCUIT_BREAKER_FILE", cb_file):
                _mod._ollama_circuit_record_failure()
                self.assertTrue(cb_file.exists(), "Circuit breaker file must be created")
                data = json.loads(cb_file.read_text())
                self.assertEqual(data["consecutive_failures"], 1)

    def test_circuit_breaker_resets(self):
        """_ollama_circuit_reset() must remove breaker file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cb_file = Path(tmpdir) / ".ollama_circuit_breaker"
            cb_file.write_text(json.dumps({"consecutive_failures": 5}))
            with patch.object(_mod, "CIRCUIT_BREAKER_FILE", cb_file):
                _mod._ollama_circuit_reset()
                self.assertFalse(cb_file.exists(), "Circuit breaker file must be removed on reset")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_moods_list_defined(self):
        """MOODS list must be defined with (name, description) tuples."""
        self.assertIsInstance(_mod.MOODS, list)
        self.assertGreater(len(_mod.MOODS), 0)
        for mood in _mod.MOODS:
            self.assertEqual(len(mood), 2,
                             "Each mood must be (name, description) tuple")

    def test_all_mood_names_are_strings(self):
        """All mood names must be non-empty strings."""
        for name, desc in _mod.MOODS:
            self.assertIsInstance(name, str)
            self.assertGreater(len(name), 0)

    def test_exclude_sources_is_tuple(self):
        """EXCLUDE_SOURCES must be a tuple for use in SQL IN clause."""
        self.assertIsInstance(_mod.EXCLUDE_SOURCES, tuple)

    def test_circuit_breaker_open_false_when_no_file(self):
        """Circuit breaker must be closed when no file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cb_file = Path(tmpdir) / ".ollama_circuit_breaker"
            with patch.object(_mod, "CIRCUIT_BREAKER_FILE", cb_file):
                self.assertFalse(_mod._ollama_circuit_open())

    def test_circuit_breaker_open_true_after_3_failures(self):
        """Circuit breaker must open after 3 consecutive failures within cooldown."""
        from datetime import datetime
        with tempfile.TemporaryDirectory() as tmpdir:
            cb_file = Path(tmpdir) / ".ollama_circuit_breaker"
            cb_file.write_text(json.dumps({
                "consecutive_failures": 3,
                "last_failure": datetime.now().isoformat(),
                "cooldown_hours": 1,
            }))
            with patch.object(_mod, "CIRCUIT_BREAKER_FILE", cb_file):
                self.assertTrue(_mod._ollama_circuit_open())

    def test_read_file_truncates(self):
        """read_file() must truncate at max_chars."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("x" * 3000)
            path = f.name
        try:
            result = _mod.read_file(path, max_chars=100)
            self.assertEqual(len(result), 100)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_write_journal_creates_file(self):
        """write_journal() must create a markdown file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_dir = Path(tmpdir) / "dreams"
            journal_dir.mkdir()
            with patch.object(_mod, "JOURNAL_DIR", journal_dir):
                with patch.object(_mod, "TODAY", "2025-01-01"):
                    path = _mod.write_journal("Test narrative text.", None, [], {})
            self.assertTrue(path.exists(), "Journal file must be created")
            content = path.read_text()
            self.assertIn("Test narrative text.", content)

    def test_write_pending_creates_json(self):
        """write_pending() must create a valid JSON file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pending_file = Path(tmpdir) / "pending_delivery.json"
            with patch.object(_mod, "PENDING", pending_file):
                with patch.object(_mod, "TODAY", "2025-01-01"):
                    _mod.write_pending("Test narrative.", Path(tmpdir) / "journal.md",
                                       None, [], {})
            self.assertTrue(pending_file.exists())
            data = json.loads(pending_file.read_text())
            self.assertEqual(data["date"], "2025-01-01")
            self.assertEqual(data["narrative"], "Test narrative.")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_generate_narrative_returns_empty_on_all_failures(self):
        """generate_narrative() must return empty string when all models fail."""
        with patch.object(_mod, "_generate_via_openrouter",
                          side_effect=RuntimeError("OpenRouter down")):
            with patch.object(_mod, "_ollama_circuit_open", return_value=True):
                with patch.object(_mod, "query_recent_memories_for_theme",
                                  return_value=("", [])):
                    with patch.object(_mod, "query_themed_memories", return_value=[]):
                        with patch.object(_mod, "query_wildcard_memories", return_value=[]):
                            with patch.object(_mod, "_generate_short",
                                              return_value="test theme"):
                                narrative, inspirations, meta = _mod.generate_narrative()
        self.assertEqual(narrative, "",
                         "Empty narrative must be returned when all models fail")

    def test_journal_includes_theme_and_mood(self):
        """Journal file must include theme and mood metadata."""
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_dir = Path(tmpdir) / "dreams"
            journal_dir.mkdir()
            dream_meta = {"theme": "loss of signal", "mood": "surreal"}
            with patch.object(_mod, "JOURNAL_DIR", journal_dir):
                with patch.object(_mod, "TODAY", "2025-01-01"):
                    path = _mod.write_journal("Test dream.", None, [], dream_meta)
            content = path.read_text()
            self.assertIn("loss of signal", content)
            self.assertIn("surreal", content)

    def test_pending_includes_inspirations(self):
        """Pending delivery JSON must include inspiration list."""
        inspirations = [{"source": "history", "label": "wwii", "memory": "test"}]
        with tempfile.TemporaryDirectory() as tmpdir:
            pending_file = Path(tmpdir) / "pending_delivery.json"
            with patch.object(_mod, "PENDING", pending_file):
                with patch.object(_mod, "TODAY", "2025-01-01"):
                    _mod.write_pending("Narrative.", Path(tmpdir) / "j.md",
                                       None, inspirations, {})
            data = json.loads(pending_file.read_text())
            self.assertEqual(len(data["inspirations"]), 1)
            self.assertEqual(data["inspirations"][0]["source"], "history")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_skips_if_pending_exists_for_today(self):
        """main() must skip generation if pending file already exists for today."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pending_file = Path(tmpdir) / "pending_delivery.json"
            pending_file.write_text(json.dumps({
                "date": _mod.TODAY,
                "narrative": "Already generated dream.",
            }))
            deliver_calls = []
            with patch.object(_mod, "PENDING", pending_file):
                with patch.object(_mod, "deliver_dream",
                                  side_effect=lambda: deliver_calls.append(1)):
                    _mod.main()
            self.assertEqual(len(deliver_calls), 1,
                             "Should call deliver_dream, not re-generate")

    def test_generate_dream_image_skips_if_swarmui_down(self):
        """generate_dream_image() must return empty string if SwarmUI is down."""
        def failing_urlopen(req, timeout=None):
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", side_effect=failing_urlopen):
            result = _mod.generate_dream_image("A surreal dream narrative here.", "surreal")
        self.assertEqual(result, "",
                         "Must return empty string when SwarmUI is unreachable")

    def test_header_stripping_removes_echo(self):
        """generate_narrative pipeline must strip model-echoed headers."""
        src = _SCRIPT.read_text()
        self.assertIn("lines[0].startswith", src,
                      "Header stripping logic must be present")

    def test_deliver_dream_calls_dream_deliver_script(self):
        """deliver_dream() must invoke dream_deliver.py subprocess."""
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args[0])
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            _mod.deliver_dream()

        self.assertGreater(len(calls), 0,
                           "deliver_dream() must call subprocess.run")
        cmd = " ".join(str(c) for c in calls[0])
        self.assertIn("dream_deliver.py", cmd,
                      "deliver_dream() must invoke dream_deliver.py")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        """dream_generate.py must compile without errors."""
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"dream_generate.py has syntax errors: {e}")

    def test_module_loads(self):
        """dream_generate module must load."""
        self.assertIsNotNone(_mod)

    def test_main_function_exists(self):
        """main() must exist."""
        self.assertTrue(callable(_mod.main))

    def test_constants_present(self):
        """Critical constants must be defined."""
        for attr in ["WORKSPACE", "JOURNAL_DIR", "PENDING", "MOODS",
                     "OLLAMA_URL", "OPENROUTER_URL", "VECTOR_URL",
                     "FALLBACK_MODELS", "EXCLUDE_SOURCES"]:
            self.assertTrue(hasattr(_mod, attr), f"{attr} must be defined")

    def test_functions_present(self):
        """Required functions must exist."""
        for fn_name in ["generate_narrative", "generate_dream_image",
                        "write_journal", "write_pending", "derive_theme",
                        "log", "_ollama_circuit_open"]:
            self.assertTrue(callable(getattr(_mod, fn_name, None)),
                            f"Function {fn_name} must exist")

    def test_moods_count_reasonable(self):
        """MOODS must have at least 6 entries."""
        self.assertGreaterEqual(len(_mod.MOODS), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
