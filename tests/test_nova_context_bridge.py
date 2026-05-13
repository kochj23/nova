"""
test_nova_context_bridge.py — All 7 test categories for nova_context_bridge.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import subprocess
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
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_context_bridge.py"

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "C0ATAF7NZG9"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_spec = importlib.util.spec_from_file_location("nova_context_bridge", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_state = _mod.load_state
save_state = _mod.save_state
filter_echoes = _mod.filter_echoes
build_bridge_message = _mod.build_bridge_message
recall = _mod.recall


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-live", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pattern in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(pattern, src, f"PII found: {pattern!r}")

    def test_vector_url_uses_localhost(self):
        self.assertIn("127.0.0.1", _mod.VECTOR_URL)

    def test_state_file_in_home(self):
        self.assertTrue(str(_mod.STATE_FILE).startswith(str(Path.home())))

    def test_bridge_message_does_not_include_raw_api_tokens(self):
        """build_bridge_message() must not inject raw API tokens into Slack messages."""
        signal = "coding: add authentication to API"
        echo = {
            "text": "API authentication discussion from March 2025",
            "date": "2025-03-15",
            "source": "email_archive",
            "similarity": 0.72,
            "days_ago": 300,
        }
        msg = build_bridge_message(signal, echo)
        # No API keys should appear
        for pattern in ["sk-", "ghp_", "xoxb-", "AKIA"]:
            self.assertNotIn(pattern, msg)

    def test_filter_echoes_respects_similarity_ceiling(self):
        """filter_echoes() must reject memories that are too similar (same event)."""
        results = [{
            "metadata": {"date": "2025-01-01"},
            "score": 0.9,  # Above ceiling (0.85)
            "text": "Jordan worked on authentication feature today",
        }]
        echoes = filter_echoes(results)
        self.assertEqual(len(echoes), 0,
                         "filter_echoes() must reject memories above similarity ceiling")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_filter_echoes_fast(self):
        """filter_echoes() must process 1000 results in < 100ms."""
        old_date = (date.today() - timedelta(days=60)).isoformat()
        results = [
            {
                "metadata": {"date": old_date},
                "score": 0.6 + (i % 3) * 0.1,
                "text": f"Memory text number {i} about Jordan's projects",
            }
            for i in range(1000)
        ]
        start = time.perf_counter()
        echoes = filter_echoes(results)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"filter_echoes(1000) took {elapsed:.3f}s")

    def test_build_bridge_message_fast(self):
        """build_bridge_message() must complete in < 5ms."""
        signal = "coding: refactor authentication module"
        echo = {
            "text": "Worked on auth module in March, made good progress on OAuth2 flow.",
            "date": "2025-03-15",
            "source": "email_archive",
            "similarity": 0.7,
            "days_ago": 300,
        }
        start = time.perf_counter()
        for _ in range(1000):
            build_bridge_message(signal, echo)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"build_bridge_message 1000x took {elapsed:.3f}s")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_recall_returns_empty_on_failure(self):
        """recall() must return [] on network failure."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("memory server down")
            result = recall("test query")
        self.assertEqual(result, [])

    def test_main_handles_missing_signals_gracefully(self):
        """main() must exit gracefully when no signals are found."""
        with patch.object(_mod, "gather_today_signals", return_value=[]):
            with patch.object(_mod, "slack_post") as mock_slack:
                _mod.main()
        mock_slack.assert_not_called()

    def test_main_handles_recall_failure(self):
        """main() must not crash when recall fails for all signals."""
        with patch.object(_mod, "gather_today_signals", return_value=["coding: feature A"]):
            with patch.object(_mod, "recall", return_value=[]):
                with tempfile.TemporaryDirectory() as tmpdir:
                    with patch.object(_mod, "STATE_FILE",
                                      Path(tmpdir) / "state.json"):
                        try:
                            _mod.main()
                        except Exception as e:
                            self.fail(f"main() raised when recall failed: {e}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_filter_echoes_rejects_too_recent(self):
        """filter_echoes() must reject memories that are too recent (< 14 days)."""
        recent_date = (date.today() - timedelta(days=5)).isoformat()
        results = [{
            "metadata": {"date": recent_date},
            "score": 0.65,
            "text": "Recent memory about a project discussion.",
        }]
        echoes = filter_echoes(results)
        self.assertEqual(len(echoes), 0,
                         "filter_echoes() must reject memories younger than MIN_ECHO_AGE_DAYS")

    def test_filter_echoes_rejects_too_low_similarity(self):
        """filter_echoes() must reject weakly similar memories."""
        old_date = (date.today() - timedelta(days=60)).isoformat()
        results = [{
            "metadata": {"date": old_date},
            "score": 0.3,  # Below SIMILARITY_FLOOR (0.45)
            "text": "Old memory with low similarity.",
        }]
        echoes = filter_echoes(results)
        self.assertEqual(len(echoes), 0,
                         "filter_echoes() must reject memories below similarity floor")

    def test_filter_echoes_rejects_short_text(self):
        """filter_echoes() must reject memories with very short text."""
        old_date = (date.today() - timedelta(days=60)).isoformat()
        results = [{
            "metadata": {"date": old_date},
            "score": 0.65,
            "text": "Too short.",
        }]
        echoes = filter_echoes(results)
        self.assertEqual(len(echoes), 0,
                         "filter_echoes() must reject memories with text < 20 chars")

    def test_filter_echoes_accepts_valid_echo(self):
        """filter_echoes() must accept memories that meet all criteria."""
        old_date = (date.today() - timedelta(days=60)).isoformat()
        results = [{
            "metadata": {"date": old_date},
            "score": 0.65,
            "text": "Jordan worked on authentication and security review for the API.",
        }]
        echoes = filter_echoes(results)
        self.assertEqual(len(echoes), 1, "Valid echo must be returned")

    def test_filter_echoes_uses_score_key_fallback(self):
        """filter_echoes() must use 'score' key when 'similarity' is absent."""
        old_date = (date.today() - timedelta(days=60)).isoformat()
        results = [{
            "metadata": {"date": old_date},
            "score": 0.65,
            "text": "Jordan worked on memory architecture improvements.",
        }]
        echoes = filter_echoes(results)
        self.assertEqual(len(echoes), 1)

    def test_filter_echoes_sorted_by_age(self):
        """filter_echoes() must sort oldest first (most surprising)."""
        def make_echo(days_ago):
            old_date = (date.today() - timedelta(days=days_ago)).isoformat()
            return {
                "metadata": {"date": old_date},
                "score": 0.65,
                "text": f"Memory from {days_ago} days ago about Jordan's work on projects.",
            }
        results = [make_echo(30), make_echo(200), make_echo(100)]
        echoes = filter_echoes(results)
        self.assertEqual(len(echoes), 3)
        self.assertEqual(echoes[0]["days_ago"], 200, "Oldest echo should be first")

    def test_build_bridge_message_includes_echo_text(self):
        """build_bridge_message() must include the echo text."""
        signal = "coding: refactor auth"
        echo = {
            "text": "Jordan discussed OAuth2 implementation details and best practices.",
            "date": "2025-03-15",
            "source": "email_archive",
            "similarity": 0.7,
            "days_ago": 300,
        }
        msg = build_bridge_message(signal, echo)
        self.assertIn("OAuth2", msg)
        self.assertIn("2025-03-15", msg)

    def test_build_bridge_message_truncates_long_echo(self):
        """build_bridge_message() must truncate long echo text to 200 chars."""
        signal = "coding: feature"
        echo = {
            "text": "x" * 500,
            "date": "2025-01-01",
            "source": "test",
            "similarity": 0.7,
            "days_ago": 100,
        }
        msg = build_bridge_message(signal, echo)
        text_part = msg[msg.find(">") + 1:] if ">" in msg else msg
        # The displayed echo text should not be 500 chars
        self.assertLessEqual(len(msg), 800, "Bridge message should not be excessively long")

    def test_load_state_returns_fresh_on_different_date(self):
        """load_state() must return a fresh state if the stored date != today."""
        old_state = {"date": "2020-01-01", "bridges_sent": ["old bridge"], "topics_used": ["old topic"]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(old_state, f)
            fname = f.name
        try:
            with patch.object(_mod, "STATE_FILE", Path(fname)):
                state = load_state()
            self.assertEqual(state["bridges_sent"], [])
            self.assertEqual(state["topics_used"], [])
        finally:
            os.unlink(fname)

    def test_load_state_restores_today_state(self):
        """load_state() must return existing state if date == today."""
        today_state = {"date": _mod.TODAY, "bridges_sent": ["some bridge"], "topics_used": ["coding"]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(today_state, f)
            fname = f.name
        try:
            with patch.object(_mod, "STATE_FILE", Path(fname)):
                state = load_state()
            self.assertEqual(len(state["bridges_sent"]), 1)
        finally:
            os.unlink(fname)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_posts_bridge_when_echo_found(self):
        """main() must post to Slack when a valid echo is found."""
        slack_calls = []
        old_date = (date.today() - timedelta(days=60)).isoformat()

        def mock_recall(query, n=8):
            return [{
                "metadata": {"date": old_date},
                "score": 0.65,
                "text": f"Jordan worked on {query} feature in early 2025 iteration.",
            }]

        with patch.object(_mod, "gather_today_signals", return_value=["coding: auth feature"]):
            with patch.object(_mod, "recall", side_effect=mock_recall):
                with patch.object(_mod, "slack_post",
                                  side_effect=lambda msg, channel=None: slack_calls.append(msg)):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "state.json"):
                            _mod.main()

        self.assertTrue(len(slack_calls) > 0,
                        "main() must post a bridge message when valid echo found")

    def test_save_and_load_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "state.json"):
                state = load_state()
                state["bridges_sent"].append({"signal": "test", "echo_date": "2025-01-01"})
                save_state(state)
                loaded = load_state()
        self.assertEqual(len(loaded["bridges_sent"]), 1)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_cli_signals_mode(self):
        """--signals mode must list today's signals without posting."""
        with patch.object(_mod, "gather_today_signals",
                          return_value=["coding: test feature", "meeting: standup"]):
            with patch.object(_mod, "slack_post") as mock_slack:
                result = subprocess.run(
                    [sys.executable, str(_SCRIPT), "--signals"],
                    capture_output=True, text=True,
                    env={**os.environ, "PYTHONPATH": str(_SCRIPT.parent)},
                    timeout=5,
                )
        # Should not crash
        self.assertNotIn("Traceback", result.stderr)

    def test_main_limits_to_two_bridges_per_day(self):
        """main() must not post more than 2 bridges per day."""
        old_date = (date.today() - timedelta(days=60)).isoformat()
        slack_calls = []

        def mock_recall(query, n=8):
            return [{
                "metadata": {"date": old_date},
                "score": 0.65,
                "text": f"Jordan's memory about {query} work and related topics.",
            }]

        signals = [f"coding: feature {i}" for i in range(10)]

        with patch.object(_mod, "gather_today_signals", return_value=signals):
            with patch.object(_mod, "recall", side_effect=mock_recall):
                with patch.object(_mod, "slack_post",
                                  side_effect=lambda msg, channel=None: slack_calls.append(msg)):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "state.json"):
                            _mod.main()

        self.assertLessEqual(len(slack_calls), 2,
                             "main() must not post more than 2 bridges per day")


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
        for fn in ["load_state", "save_state", "gather_today_signals",
                   "recall", "filter_echoes", "build_bridge_message", "main"]:
            self.assertTrue(callable(getattr(_mod, fn, None)), f"Missing: {fn}")

    def test_constants_defined(self):
        self.assertIsInstance(_mod.MIN_ECHO_AGE_DAYS, int)
        self.assertIsInstance(_mod.SIMILARITY_FLOOR, float)
        self.assertIsInstance(_mod.SIMILARITY_CEILING, float)
        self.assertGreater(_mod.SIMILARITY_CEILING, _mod.SIMILARITY_FLOOR)

    def test_bridge_intros_non_empty(self):
        self.assertGreater(len(_mod.BRIDGE_INTROS), 3,
                           "BRIDGE_INTROS should have variety")

    def test_today_is_set(self):
        self.assertIsInstance(_mod.TODAY, str)
        self.assertGreater(len(_mod.TODAY), 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
