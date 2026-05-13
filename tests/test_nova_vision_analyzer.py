"""
test_nova_vision_analyzer.py — All 7 test categories for nova_vision_analyzer.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_vision_analyzer.py"
_spec = importlib.util.spec_from_file_location("nova_vision_analyzer", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_", "Bearer "]:
            self.assertNotIn(pat, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_all_llm_calls_are_local(self):
        """All LLM calls must go to local Ollama, not cloud."""
        self.assertTrue(_mod.OLLAMA_URL.startswith("http://127.0.0.1"))

    def test_vision_model_is_local(self):
        self.assertTrue(_mod.OLLAMA_URL.startswith("http://127.0.0.1"))

    def test_memory_url_is_localhost(self):
        self.assertTrue(_mod.MEMORY_URL.startswith("http://127.0.0.1"))

    def test_think_tags_stripped_from_response(self):
        """<think> tags from qwen3 models must be stripped from output."""
        src = _SCRIPT.read_text()
        self.assertIn("<think>", src, "Think tag stripping code missing")
        self.assertIn("</think>", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_ollama_timeout_defined(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=120", src)

    def test_recall_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=5", src)

    def test_query_local_returns_none_on_timeout(self):
        """query_local must return None on failure, not raise."""
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = _mod.query_local("test prompt")
        self.assertIsNone(result)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_returns_none_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = _mod.remember("test vision event", "vision")
        self.assertIsNone(result)

    def test_recall_returns_empty_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = _mod.recall("camera events")
        self.assertEqual(result, [])

    def test_describe_image_returns_none_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                f.write(b"FAKE")
                tmp = f.name
            try:
                result = _mod.describe_image(tmp)
            finally:
                os.unlink(tmp)
        self.assertIsNone(result)

    def test_anomaly_alert_does_not_raise_on_llm_failure(self):
        """anomaly_alert must not raise even if LLM is unavailable."""
        with patch.object(_mod, "query_local", return_value=None):
            result = _mod.anomaly_alert("suspicious activity", "medium")
        self.assertIsNone(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_query_local_strips_think_tags(self):
        """query_local must strip <think>...</think> from response."""
        mock_response = json.dumps({"response": "<think>Internal reasoning</think>Real answer here."})
        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_response.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _mod.query_local("test")

        self.assertNotIn("<think>", result)
        self.assertNotIn("</think>", result)
        self.assertIn("Real answer", result)

    def test_anomaly_alert_severity_levels(self):
        """anomaly_alert must accept low/medium/high severity."""
        for severity in ("low", "medium", "high"):
            with patch.object(_mod, "query_local", return_value="No threat"):
                with patch.object(_mod, "remember"):
                    with patch.object(_mod, "slack_post"):
                        result = _mod.anomaly_alert("test anomaly", severity)

    def test_slack_post_calls_nova_config(self):
        """slack_post must delegate to nova_config.post_both."""
        with patch.object(_nova_cfg, "post_both") as mock_post:
            _mod.slack_post("Test message")
        mock_post.assert_called_once()

    def test_recall_parses_results_key(self):
        """recall() must handle both 'results' and 'memories' keys."""
        mock_data = json.dumps({"results": [{"text": "Event 1"}, {"text": "Event 2"}]})
        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_data.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _mod.recall("camera events")
        self.assertEqual(len(result), 2)

    def test_anomaly_high_severity_posts_to_slack(self):
        """High severity anomalies must post to Slack."""
        slack_calls = []
        with patch.object(_mod, "query_local", return_value="Yes, concerning. Call 911."):
            with patch.object(_mod, "remember"):
                with patch.object(_mod, "slack_post", side_effect=lambda t: slack_calls.append(t)):
                    _mod.anomaly_alert("armed person at door", "high")
        self.assertGreater(len(slack_calls), 0)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_analyze_daily_events_calls_recall_and_query(self):
        """analyze_daily_events must recall events then query LLM."""
        recall_calls = []
        query_calls = []

        with patch.object(_mod, "recall", side_effect=lambda q, **kw: recall_calls.append(q) or [{"text": "Person at door"}]):
            with patch.object(_mod, "query_local", side_effect=lambda p, **kw: query_calls.append(p) or "Low threat level"):
                with patch.object(_mod, "remember"):
                    with patch.object(_mod, "slack_post"):
                        _mod.analyze_daily_events()

        self.assertGreater(len(recall_calls), 0)
        self.assertGreater(len(query_calls), 0)

    def test_analyze_daily_events_no_events_returns_none(self):
        """If no events, analyze_daily_events returns None without calling LLM."""
        with patch.object(_mod, "recall", return_value=[]):
            with patch.object(_mod, "query_local") as mock_llm:
                result = _mod.analyze_daily_events()
        self.assertIsNone(result)
        mock_llm.assert_not_called()

    def test_analyze_threat_profile_includes_clip_count(self):
        """Threat analysis prompt must include motion clip count."""
        query_prompts = []
        with patch.object(_mod, "recall", return_value=[{"text": "anomaly test"}]):
            with patch.object(_mod, "query_local", side_effect=lambda p, **kw: query_prompts.append(p) or "Low"):
                with patch.object(_mod, "remember"):
                    with patch.object(_mod, "slack_post"):
                        with patch.object(_mod, "CLIPS_DIR", Path("/nonexistent")):
                            _mod.analyze_threat_profile()
        self.assertGreater(len(query_prompts), 0)
        self.assertIn("clip", query_prompts[0].lower())


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_defaults_to_daily_analysis(self):
        """main() with no args should call analyze_daily_events."""
        with patch("sys.argv", ["nova_vision_analyzer.py"]):
            with patch.object(_mod, "analyze_daily_events") as mock_daily:
                _mod.main()
        mock_daily.assert_called_once()

    def test_main_threat_subcommand(self):
        with patch("sys.argv", ["nova_vision_analyzer.py", "threat"]):
            with patch.object(_mod, "analyze_threat_profile") as mock_threat:
                _mod.main()
        mock_threat.assert_called_once()

    def test_main_anomaly_subcommand(self):
        with patch("sys.argv", ["nova_vision_analyzer.py", "anomaly", "car outside"]):
            with patch.object(_mod, "anomaly_alert") as mock_anom:
                _mod.main()
        mock_anom.assert_called_once()


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_constants_defined(self):
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIsInstance(_mod.OLLAMA_URL, str)
        self.assertIsInstance(_mod.MODEL, str)
        self.assertIsInstance(_mod.VISION_MODEL, str)
        self.assertIsInstance(_mod.CLIPS_DIR, Path)

    def test_functions_exist(self):
        for fn in ("log", "remember", "recall", "query_local", "describe_image",
                   "slack_post", "analyze_daily_events", "analyze_threat_profile",
                   "anomaly_alert", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
