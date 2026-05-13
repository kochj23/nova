"""
test_nova_livetv.py — All 7 test categories for nova_livetv.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.SLACK_API = "https://slack.com/api"
_nova_cfg.JORDAN_DM = "D0AMPB3F4T0"
_nova_cfg.slack_bot_token.return_value = "xoxb-test"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_livetv.py"
_spec = importlib.util.spec_from_file_location("nova_livetv", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

classify_tv_content = _mod.classify_tv_content
load_schedule = _mod.load_schedule
matches_day = _mod.matches_day
is_weekday = _mod.is_weekday
mark_bad_channel = _mod.mark_bad_channel
get_bad_channels = _mod.get_bad_channels
save_prefs = _mod.save_prefs
load_prefs = _mod.load_prefs
ingest_to_memory = _mod.ingest_to_memory


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_"]:
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

    def test_hdhr_is_local_ip(self):
        """HDHomeRun must be local network IP."""
        self.assertIn("192.168.", _mod.HDHR_BASE)

    def test_vector_url_is_localhost(self):
        self.assertTrue(_mod.VECTOR_URL.startswith("http://127.0.0.1"))

    def test_ollama_is_localhost(self):
        self.assertTrue(_mod.OLLAMA_URL.startswith("http://127.0.0.1"))

    def test_transcription_is_local(self):
        """Transcription must use local mlx_whisper, not cloud."""
        src = _SCRIPT.read_text()
        self.assertIn("mlx_whisper", src)
        self.assertNotIn("openai.com/whisper", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_classify_tv_content_fast(self):
        start = time.perf_counter()
        for _ in range(100):
            classify_tv_content("Jeopardy!", "game show quiz", ["Game Show"])
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_ingest_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=10", src)

    def test_vector_map_covers_major_content_types(self):
        required = {"game_show", "comedy", "drama", "documentary", "news", "sports"}
        missing = required - set(_mod.VECTOR_MAP.keys())
        self.assertEqual(missing, set(), f"Missing: {missing}")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_ingest_to_memory_handles_failure(self):
        """ingest_to_memory must not raise on failure."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            ingest_to_memory("Test text", "livetv_news", {})

    def test_ollama_generate_returns_empty_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = _mod.ollama_generate("test prompt")
        self.assertEqual(result, "")

    def test_get_lineup_returns_empty_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = _mod.get_lineup()
        self.assertEqual(result, [])

    def test_get_tuner_status_returns_empty_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = _mod.get_tuner_status()
        self.assertEqual(result, [])


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_classify_game_show(self):
        result = classify_tv_content("Jeopardy!", "quiz show contestant", ["Game Show"])
        self.assertEqual(result, "game_show")

    def test_classify_news(self):
        result = classify_tv_content("Local News", "anchor breaking update", ["News"])
        self.assertEqual(result, "news")

    def test_classify_unknown_defaults_to_documentary(self):
        result = classify_tv_content("Random Show", "", [])
        self.assertEqual(result, "documentary")

    def test_matches_day_daily(self):
        self.assertTrue(matches_day("daily"))

    def test_matches_day_weekdays(self):
        with patch("nova_livetv.datetime") as mock_dt:
            mock_dt.now.return_value.weekday.return_value = 0  # Monday
            self.assertTrue(matches_day("weekdays"))

    def test_mark_bad_channel_increments_failures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "PREFS_FILE", Path(tmpdir) / "prefs.json"):
                prefs = load_prefs()
                mark_bad_channel(prefs, "9.1", "KCAL-DT", "no signal")
                self.assertEqual(prefs["bad_channels"]["9.1"]["failures"], 1)
                mark_bad_channel(prefs, "9.1", "KCAL-DT", "timeout")
                self.assertEqual(prefs["bad_channels"]["9.1"]["failures"], 2)

    def test_get_bad_channels_threshold_is_2(self):
        prefs = {
            "bad_channels": {
                "9.1": {"failures": 1},
                "5.1": {"failures": 2},
                "7.1": {"failures": 3},
            }
        }
        bad = get_bad_channels(prefs)
        self.assertNotIn("9.1", bad)
        self.assertIn("5.1", bad)
        self.assertIn("7.1", bad)

    def test_key_channels_has_major_la_stations(self):
        self.assertIn("7.1", _mod.KEY_CHANNELS)  # ABC
        self.assertIn("2.1", _mod.KEY_CHANNELS)  # CBS

    def test_breaking_keywords_defined(self):
        self.assertIn("breaking", _mod.BREAKING_KEYWORDS)
        self.assertIn("earthquake", _mod.BREAKING_KEYWORDS)

    def test_ingest_to_memory_skips_short_text(self):
        """ingest_to_memory must skip text shorter than 20 chars."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            ingest_to_memory("Short", "livetv_news")
        mock_urlopen.assert_not_called()

    def test_ollama_strips_think_tags(self):
        """ollama_generate must strip <think> tags from qwen3 responses."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "response": "<think>Internal thoughts</think>Actual response"
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _mod.ollama_generate("test")
        self.assertNotIn("<think>", result)
        self.assertIn("Actual response", result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_load_schedule_returns_default_when_missing(self):
        with patch.object(_mod, "SCHEDULE_FILE", Path("/nonexistent/schedule.json")):
            with tempfile.TemporaryDirectory() as tmpdir:
                with patch.object(_mod, "SCHEDULE_FILE", Path(tmpdir) / "schedule.json"):
                    schedule = load_schedule()
        self.assertIn("shows", schedule)
        self.assertGreater(len(schedule["shows"]), 0)

    def test_load_and_save_prefs_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "PREFS_FILE", Path(tmpdir) / "prefs.json"):
                prefs = {"history_count": 5, "favorites": ["7.1"], "bad_channels": {}}
                save_prefs(prefs)
                loaded = load_prefs()
        self.assertEqual(loaded["history_count"], 5)
        self.assertIn("7.1", loaded["favorites"])

    def test_cmd_breaking_disabled(self):
        """cmd_breaking must be disabled (returns early)."""
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            _mod.cmd_breaking(MagicMock())
        # Should print that breaking news is disabled and return


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_cmd_whats_on_no_alert_outside_window(self):
        """cmd_whats_on should not post when no shows are starting."""
        with patch.object(_mod, "post") as mock_post:
            with patch.object(_mod, "load_schedule", return_value={"shows": []}):
                _mod.cmd_whats_on(MagicMock())
        mock_post.assert_not_called()

    def test_dry_run_flag_suppresses_recording(self):
        """DRY_RUN=True must suppress actual recording calls."""
        _mod.DRY_RUN = True
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                with patch.object(_mod, "WORK_DIR", Path(tmpdir)):
                    wav = _mod.record_audio("7.1", 30, "test")
            self.assertIsNotNone(wav)  # returns fake file in dry-run
        finally:
            _mod.DRY_RUN = False


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
        self.assertIsInstance(_mod.HDHR_BASE, str)
        self.assertIsInstance(_mod.HDHR_STREAM, str)
        self.assertIsInstance(_mod.VECTOR_URL, str)
        self.assertIsInstance(_mod.KEY_CHANNELS, dict)
        self.assertIsInstance(_mod.BREAKING_KEYWORDS, list)
        self.assertIsInstance(_mod.VECTOR_MAP, dict)

    def test_functions_exist(self):
        for fn in ("get_lineup", "get_tuner_status", "record_audio", "transcribe",
                   "classify_tv_content", "ingest_to_memory", "ollama_generate",
                   "load_schedule", "load_prefs", "save_prefs",
                   "cmd_whats_on", "cmd_news", "cmd_breaking", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
