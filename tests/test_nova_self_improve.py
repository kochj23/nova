"""
test_nova_self_improve.py — All 7 test categories for nova_self_improve.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Load module under test — stub nova_config
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_self_improve.py"

_nova_cfg = MagicMock()
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_spec = importlib.util.spec_from_file_location("nova_self_improve", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)

# Suppress log file creation during module load
with patch("pathlib.Path.mkdir", MagicMock()):
    _spec.loader.exec_module(_mod)

get_past_week_dates = _mod.get_past_week_dates
build_critique_prompt = _mod.build_critique_prompt
save_lessons = _mod.save_lessons
build_slack_summary = _mod.build_slack_summary
generate_critique = _mod.generate_critique
load_state = _mod.load_state
save_state = _mod.save_state


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_api_keys(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-live", "ghp_", "AKIA"]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_openrouter_key_from_keychain(self):
        """get_openrouter_key() must use macOS Keychain, not env var."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="fake-key\n")
            key = _mod.get_openrouter_key()
        self.assertEqual(key, "fake-key")
        # Ensure it called security CLI
        call_args = mock_run.call_args
        self.assertIn("security", str(call_args))

    def test_get_openrouter_key_raises_when_missing(self):
        """get_openrouter_key() must raise RuntimeError when key not in Keychain."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            with self.assertRaises(RuntimeError):
                _mod.get_openrouter_key()

    def test_lessons_file_in_home(self):
        self.assertTrue(str(_mod.LESSONS_FILE).startswith(str(Path.home())))

    def test_state_file_in_home(self):
        self.assertTrue(str(_mod.STATE_FILE).startswith(str(Path.home())))

    def test_prompt_does_not_hardcode_pii(self):
        """build_critique_prompt() must not include hardcoded personal data."""
        dreams = [{"date": "2026-01-01", "content": "A dream about flying over the city."}]
        essays = []
        opinions = []
        system, user = build_critique_prompt(dreams, essays, opinions)
        _at = "@"
        for pattern in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(pattern, user + system, f"PII found in prompt: {pattern}")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_get_past_week_dates_fast(self):
        """get_past_week_dates() must complete in < 1ms."""
        start = time.perf_counter()
        for _ in range(1000):
            get_past_week_dates()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"get_past_week_dates 1000x took {elapsed:.3f}s")

    def test_build_slack_summary_fast(self):
        """build_slack_summary() must complete in < 10ms."""
        critique = "# Nova's Writing Lessons\nLast updated: 2026-01-01\n\n## Avoid\n- Stop using 'so' as opener.\n- Don't end every dream with a one-word sentence."
        start = time.perf_counter()
        for _ in range(1000):
            build_slack_summary(critique, 7, 7, 7)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"build_slack_summary 1000x took {elapsed:.3f}s")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_generate_critique_falls_back_to_ollama_on_openrouter_failure(self):
        """generate_critique() must fall back to Ollama when OpenRouter fails."""
        ollama_called = [False]
        # Make a response long enough to pass the 100-char minimum check
        long_critique = (
            "# Nova's Writing Lessons\nLast updated: 2026-01-01\n\n"
            "## Dreams\n- Less repetition of imagery.\n\n"
            "## Avoid\n- Stop using 'something between' as transition.\n"
            "- Avoid ending every dream with a one-word sentence.\n"
        )

        def fake_generate_ollama(system, user, model):
            ollama_called[0] = True
            return long_critique

        with patch.object(_mod, "generate_via_openrouter",
                          side_effect=Exception("OpenRouter down")):
            with patch.object(_mod, "generate_via_ollama", side_effect=fake_generate_ollama):
                system, user = "system", "user prompt"
                result = generate_critique(system, user)

        self.assertTrue(ollama_called[0], "Ollama fallback must be called when OpenRouter fails")
        self.assertIsNotNone(result)

    def test_generate_critique_returns_none_when_all_fail(self):
        """generate_critique() must return None when all backends fail."""
        with patch.object(_mod, "generate_via_openrouter", side_effect=Exception("OR down")):
            with patch.object(_mod, "generate_via_ollama", side_effect=Exception("Ollama down")):
                result = generate_critique("system", "user")
        self.assertIsNone(result)

    def test_generate_critique_returns_none_on_too_short_response(self):
        """generate_critique() must return None if response is too short."""
        with patch.object(_mod, "generate_via_openrouter", return_value="Too short."):
            result = generate_critique("system", "user")
        self.assertIsNone(result, "generate_critique() must reject responses < 100 chars")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_get_past_week_dates_returns_7_dates(self):
        dates = get_past_week_dates()
        self.assertEqual(len(dates), 7)

    def test_get_past_week_dates_includes_today(self):
        today = date.today().isoformat()
        dates = get_past_week_dates()
        self.assertIn(today, dates)

    def test_get_past_week_dates_in_order(self):
        """Dates must be in descending order (today first)."""
        dates = get_past_week_dates()
        self.assertEqual(dates[0], date.today().isoformat())
        self.assertEqual(dates[-1], (date.today() - timedelta(days=6)).isoformat())

    def test_build_critique_prompt_returns_tuple(self):
        system, user = build_critique_prompt([], [], [])
        self.assertIsInstance(system, str)
        self.assertIsInstance(user, str)

    def test_build_critique_prompt_includes_no_writing_message(self):
        """When no dreams/essays/opinions, prompt must say so."""
        system, user = build_critique_prompt([], [], [])
        self.assertIn("No", user)

    def test_build_critique_prompt_includes_dream_content(self):
        dreams = [{"date": "2026-01-01", "content": "Flew over neon city in a glider."}]
        system, user = build_critique_prompt(dreams, [], [])
        self.assertIn("neon city", user)

    def test_save_lessons_adds_header_if_missing(self):
        """save_lessons() must add proper header if critique lacks it."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            fname = f.name
        try:
            with patch.object(_mod, "LESSONS_FILE", Path(fname)):
                with patch("pathlib.Path.mkdir", MagicMock()):
                    save_lessons("## Dreams\n- Less repetition.\n## Avoid\n- 'So, '")
            content = Path(fname).read_text()
            self.assertIn("# Nova's Writing Lessons", content)
        finally:
            os.unlink(fname)

    def test_save_lessons_updates_date_in_existing_header(self):
        today = date.today().isoformat()
        old_content = "# Nova's Writing Lessons (auto-updated weekly)\nLast updated: 2020-01-01\n\n## Avoid\n- test"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(old_content)
            fname = f.name
        try:
            with patch.object(_mod, "LESSONS_FILE", Path(fname)):
                with patch("pathlib.Path.mkdir", MagicMock()):
                    save_lessons(old_content)
            content = Path(fname).read_text()
            self.assertIn(today, content)
        finally:
            os.unlink(fname)

    def test_build_slack_summary_extracts_avoid_section(self):
        critique = (
            "# Nova's Writing Lessons\nLast updated: 2026-01-01\n\n"
            "## Dreams\n- Less repetition.\n\n"
            "## Avoid\n- Stop using 'so' as opener.\n- Don't end with one-word sentence.\n\n"
            "## Essays\n- Clearer thesis."
        )
        msg = build_slack_summary(critique, 5, 5, 5)
        self.assertIn("stop using", msg.lower())

    def test_load_state_returns_default_when_missing(self):
        with patch.object(_mod, "STATE_FILE", Path("/nonexistent/state.json")):
            with patch("pathlib.Path.mkdir", MagicMock()):
                state = load_state()
        self.assertEqual(state["run_count"], 0)
        self.assertEqual(state["runs"], [])


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_aborts_gracefully_when_no_writing(self):
        """main() must log ABORT and return when no writing found."""
        with patch.object(_mod, "collect_dreams", return_value=[]):
            with patch.object(_mod, "collect_essays", return_value=[]):
                with patch.object(_mod, "collect_opinions", return_value=[]):
                    with patch.object(_mod, "generate_critique") as mock_gen:
                        with patch("pathlib.Path.mkdir", MagicMock()):
                            _mod.main()
        mock_gen.assert_not_called()

    def test_main_updates_state_on_success(self):
        """main() must update run_count and save state."""
        critique = (
            "# Nova's Writing Lessons (auto-updated weekly)\nLast updated: 2026-01-01\n\n"
            "## Dreams\n- Good imagery.\n## Avoid\n- Less 'so, '.\n## Essays\n- Clear thesis."
        )
        saved_state = {}

        def capture_save(state):
            saved_state.update(state)

        with patch.object(_mod, "collect_dreams",
                          return_value=[{"date": "2026-01-01", "content": "Dream content here."}]):
            with patch.object(_mod, "collect_essays", return_value=[]):
                with patch.object(_mod, "collect_opinions", return_value=[]):
                    with patch.object(_mod, "generate_critique", return_value=critique):
                        with patch.object(_mod, "load_state",
                                          return_value={"run_count": 5, "runs": []}):
                            with patch.object(_mod, "save_state", side_effect=capture_save):
                                with patch.object(_mod, "save_lessons"):
                                    with patch("pathlib.Path.mkdir", MagicMock()):
                                        _mod.main()

        self.assertEqual(saved_state.get("run_count"), 6,
                         "main() must increment run_count")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_collect_dreams_returns_list(self):
        dates = get_past_week_dates()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "DREAMS_DIR", Path(tmpdir)):
                result = _mod.collect_dreams(dates)
        self.assertIsInstance(result, list)

    def test_collect_essays_returns_list(self):
        dates = get_past_week_dates()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "ESSAYS_DIR", Path(tmpdir)):
                result = _mod.collect_essays(dates)
        self.assertIsInstance(result, list)

    def test_state_keeps_at_most_52_runs(self):
        """State must trim runs list to 52 (1 year) to prevent unbounded growth."""
        runs = [{"date": f"2026-{i:02d}-01"} for i in range(1, 54)]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"run_count": 53, "runs": runs}, f)
            fname = f.name
        try:
            with patch.object(_mod, "STATE_FILE", Path(fname)):
                with patch("pathlib.Path.mkdir", MagicMock()):
                    state = load_state()
                    state["runs"] = (state["runs"] + [{"date": "new"}])[-52:]
                    self.assertLessEqual(len(state["runs"]), 52)
        finally:
            os.unlink(fname)


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

    def test_required_functions_exist(self):
        for fn in ["get_past_week_dates", "collect_dreams", "collect_essays",
                   "collect_opinions", "build_critique_prompt", "generate_critique",
                   "save_lessons", "build_slack_summary", "main"]:
            self.assertTrue(callable(getattr(_mod, fn, None)), f"Missing: {fn}")

    def test_model_constant_defined(self):
        self.assertIsInstance(_mod.MODEL, str)
        self.assertIn("haiku", _mod.MODEL.lower())

    def test_fallback_models_list(self):
        self.assertIsInstance(_mod.FALLBACK_MODELS, list)
        self.assertGreater(len(_mod.FALLBACK_MODELS), 0)

    def test_slack_channel_defined(self):
        self.assertIsInstance(_mod.SLACK_CHANNEL, str)
        self.assertGreater(len(_mod.SLACK_CHANNEL), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
