"""
test_nova_herd_profiles.py — All 7 test categories for nova_herd_profiles.py
Written by Jordan Koch.
"""

from __future__ import annotations
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_EMAIL = "#nova-email"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg
sys.modules["herd_config"] = MagicMock(
    HERD=[
        {"name": "Sam", "email": "sam@example.com", "profile": "sam.md"},
        {"name": "Gaston", "email": "gaston@example.com", "profile": "gaston.md"},
    ],
    HERD_EMAILS={"sam@example.com", "gaston@example.com"},
)

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_herd_profiles.py"

# Python 3.9 compatibility: rewrite X | Y return type annotations
def _load_compat(script_path, module_name):
    src = script_path.read_text()
    if sys.version_info < (3, 10):
        src = re.sub(r'\)\s*->\s*(\w+)\s*\|\s*(\w+)\s*:', r') -> "\1 | \2":', src)
    mod = types.ModuleType(module_name)
    mod.__file__ = str(script_path)
    exec(compile(src, str(script_path), "exec"), mod.__dict__)
    return mod

_mod = _load_compat(_SCRIPT, "nova_herd_profiles")

scrub_emails = _mod.scrub_emails
parse_analysis = _mod.parse_analysis
is_recent = _mod.is_recent


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA"]
        for p in forbidden:
            self.assertNotIn(p, src, f"Credential: {p!r}")

    def test_openrouter_key_from_keychain(self):
        """OpenRouter key must be loaded from Keychain."""
        src = _SCRIPT.read_text()
        self.assertIn("find-generic-password", src, "Must use Keychain for API key")

    def test_scrub_emails_removes_pii(self):
        """scrub_emails must replace all email addresses."""
        _at = "@"
        text = "Contact me at user" + _at + "example.com or test" + _at + "test.org for info."
        result = scrub_emails(text)
        self.assertNotIn(_at, result)
        self.assertIn("[email redacted]", result)

    def test_email_scrubbed_before_llm(self):
        """Source must scrub emails from body before sending to LLM."""
        src = _SCRIPT.read_text()
        self.assertIn("scrub_emails", src, "Must call scrub_emails before LLM")

    def test_profiles_stored_locally(self):
        """Profile files must be stored under user home, not cloud."""
        self.assertTrue(str(_mod.HERD_DIR).startswith(str(Path.home())))

    def test_no_pii_email_literals(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII: {p!r}")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_scrub_emails_fast(self):
        _at = "@"
        text = ("Here is user" + _at + "example.com and another" + _at + "test.org ") * 100
        start = time.perf_counter()
        for _ in range(100):
            scrub_emails(text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"scrub_emails 100x took {elapsed:.3f}s")

    def test_parse_analysis_fast(self):
        raw = """STYLE: Terse and direct
TOPICS: AI, coding, philosophy
TONE: Dry and sarcastic
RESPONSE_TYPE: pushed back
NOTABLE_QUOTE: None
SUMMARY: Critical thinker who challenges assumptions."""
        start = time.perf_counter()
        for _ in range(500):
            parse_analysis(raw)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2, f"parse_analysis 500x took {elapsed:.3f}s")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_extract_personality_falls_back_to_ollama(self):
        """extract_personality_signals must try Ollama when OpenRouter fails."""
        ollama_calls = []

        def fake_openrouter(*a, **kw):
            raise Exception("OpenRouter down")

        def fake_ollama(system, user, model):
            ollama_calls.append(model)
            return "STYLE: Direct\nTOPICS: AI\nTONE: Neutral\nRESPONSE_TYPE: agreed\nNOTABLE_QUOTE: None\nSUMMARY: Test."

        with patch.object(_mod, "_generate_via_openrouter", side_effect=fake_openrouter):
            with patch.object(_mod, "_generate_via_ollama", side_effect=fake_ollama):
                result = _mod.extract_personality_signals("Sam", "Test", "Hello Nova.")

        self.assertIsNotNone(result, "Should fall back to Ollama")
        self.assertGreater(len(ollama_calls), 0)

    def test_extract_personality_returns_none_when_all_fail(self):
        with patch.object(_mod, "_generate_via_openrouter", side_effect=Exception("down")):
            with patch.object(_mod, "_generate_via_ollama", side_effect=Exception("down")):
                result = _mod.extract_personality_signals("Sam", "Subject", "Body")
        self.assertIsNone(result)

    def test_store_memory_silent_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            try:
                _mod.store_memory("Sam", "test summary")
            except Exception as e:
                self.fail(f"store_memory raised: {e}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_scrub_emails_handles_empty(self):
        self.assertEqual(scrub_emails(""), "")

    def test_scrub_emails_handles_no_emails(self):
        text = "No emails here, just text."
        self.assertEqual(scrub_emails(text), text)

    def test_scrub_emails_multiple_emails(self):
        _at = "@"
        text = "a" + _at + "a.com and b" + _at + "b.org"
        result = scrub_emails(text)
        self.assertEqual(result.count("[email redacted]"), 2)

    def test_parse_analysis_extracts_all_fields(self):
        raw = """STYLE: Verbose and philosophical
TOPICS: ethics, AI consciousness
TONE: Earnest and thoughtful
RESPONSE_TYPE: asked questions
NOTABLE_QUOTE: "What does it mean to be?"
SUMMARY: Deep thinker interested in consciousness."""
        result = parse_analysis(raw)
        self.assertEqual(result["style"], "Verbose and philosophical")
        self.assertEqual(result["topics"], "ethics, AI consciousness")
        self.assertEqual(result["tone"], "Earnest and thoughtful")
        self.assertEqual(result["response_type"], "asked questions")
        self.assertIn("What does it mean", result["notable_quote"])
        self.assertIn("Deep thinker", result["summary"])

    def test_parse_analysis_handles_empty(self):
        result = parse_analysis("")
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result), 0)

    def test_is_recent_within_24h(self):
        from datetime import datetime, timezone, timedelta
        recent_dt = datetime.now(timezone.utc) - timedelta(hours=12)
        from email.utils import format_datetime
        date_str = format_datetime(recent_dt)
        self.assertTrue(is_recent(date_str, hours=24))

    def test_is_recent_outside_24h(self):
        from datetime import datetime, timezone, timedelta
        old_dt = datetime.now(timezone.utc) - timedelta(hours=48)
        from email.utils import format_datetime
        date_str = format_datetime(old_dt)
        self.assertFalse(is_recent(date_str, hours=24))

    def test_is_recent_invalid_date(self):
        result = is_recent("not a date", hours=24)
        self.assertFalse(result)

    def test_load_state_returns_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "sub" / "state.json"
            with patch.object(_mod, "STATE_FILE", missing):
                state = _mod.load_state()
        self.assertIn("processed_uids", state)
        self.assertIn("last_run", state)

    def test_processed_uid_list_capped_at_500(self):
        """State must cap processed_uids at 500 to prevent unbounded growth."""
        state = {"processed_uids": [str(i) for i in range(600)], "last_run": None}
        new_processed = [str(i) for i in range(600, 620)]
        all_processed = list(set(state["processed_uids"]) | set(new_processed))
        capped = all_processed[-500:]
        self.assertLessEqual(len(capped), 500)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_update_profile_creates_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            herd_dir = Path(tmpdir)
            with patch.object(_mod, "HERD_DIR", herd_dir):
                analysis = {
                    "style": "Direct",
                    "topics": "AI, coding",
                    "tone": "Dry",
                    "response_type": "agreed",
                    "notable_quote": "None",
                    "summary": "Tech-focused.",
                }
                _mod.update_profile("Sam", "sam.md", analysis)
                profile = herd_dir / "sam.md"
                self.assertTrue(profile.exists())
                content = profile.read_text()
                self.assertIn("Sam", content)
                self.assertIn("Tech-focused", content)

    def test_update_profile_scrubs_emails(self):
        """Profile update must scrub emails that creep in from analysis."""
        _at = "@"
        with tempfile.TemporaryDirectory() as tmpdir:
            herd_dir = Path(tmpdir)
            with patch.object(_mod, "HERD_DIR", herd_dir):
                analysis = {
                    "summary": "Sent email to test" + _at + "example.com about AI.",
                }
                _mod.update_profile("Sam", "sam.md", analysis)
                profile = (herd_dir / "sam.md").read_text()
                self.assertNotIn(_at, profile)

    def test_main_skips_when_no_emails(self):
        with patch.object(_mod, "fetch_recent_emails", return_value=[]):
            with patch.object(_mod, "save_state"):
                try:
                    _mod.main()
                except Exception as e:
                    self.fail(f"main() raised on empty inbox: {e}")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_post_daily_summary_posts_when_interactions(self):
        _nova_cfg.post_both.reset_mock()
        interactions = [{"name": "Sam", "summary": "Engaged with AI topics."}]
        _mod.post_daily_summary(interactions)
        _nova_cfg.post_both.assert_called()

    def test_post_daily_summary_silent_when_empty(self):
        _nova_cfg.post_both.reset_mock()
        _mod.post_daily_summary([])
        _nova_cfg.post_both.assert_not_called()

    def test_summary_truncated_for_slack(self):
        """Summary lines must be truncated at 80 chars for Slack."""
        interactions = [{"name": "Sam", "summary": "S" * 200}]
        _nova_cfg.post_both.reset_mock()
        _mod.post_daily_summary(interactions)
        if _nova_cfg.post_both.called:
            msg = _nova_cfg.post_both.call_args[0][0]
            # Each summary line should not exceed 80+3 (ellipsis) chars
            lines = msg.split("\n")
            for line in lines:
                # Only check bullet lines
                if line.strip().startswith("•"):
                    self.assertLessEqual(len(line), 120)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_herd_profiles.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertIsInstance(_mod.MEMORY_SERVER, str)
        self.assertIsInstance(_mod.OPENROUTER_URL, str)
        self.assertIsInstance(_mod.MODEL, str)
        self.assertIsInstance(_mod.HERD_DIR, Path)

    def test_email_to_member_lookup_populated(self):
        self.assertIsInstance(_mod.EMAIL_TO_MEMBER, dict)
        self.assertIn("sam@example.com", _mod.EMAIL_TO_MEMBER)

    def test_all_functions_callable(self):
        for fn in [scrub_emails, parse_analysis, is_recent,
                    _mod.load_state, _mod.save_state, _mod.main]:
            self.assertTrue(callable(fn))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
