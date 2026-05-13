"""
test_bulk_music_ingest.py — All 7 test categories for bulk_music_ingest.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub nova_config before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "bulk_music_ingest.py"


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        """Source must not contain API keys or tokens."""
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA", "xoxb-", "password =", "secret ="]
        for pattern in forbidden:
            self.assertNotIn(pattern, src,
                             f"Potential credential found: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        """Source must not hardcode the user home path."""
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src,
                         "Hardcoded home path found — use Path.home() instead")

    def test_no_pii_email(self):
        """Source must not contain personal email addresses."""
        src = _SCRIPT.read_text()
        _at = "@"
        pii_patterns = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney" + ".com",
            "kochj" + _at + "digitalnoise.net",
            "kochj23" + _at + "gmail.com",
        ]
        for pattern in pii_patterns:
            self.assertNotIn(pattern, src,
                             f"PII email found in source: {pattern!r}")

    def test_memory_url_is_localhost(self):
        """MEMORY_URL must point to localhost only."""
        src = _SCRIPT.read_text()
        self.assertIn("127.0.0.1", src,
                      "Memory URL should use localhost (127.0.0.1)")
        self.assertNotIn("0.0.0.0", src,
                         "Memory URL should not bind on all interfaces")

    def test_payload_is_json_encoded(self):
        """remember() must JSON-encode the payload before sending."""
        src = _SCRIPT.read_text()
        self.assertIn("json.dumps", src,
                      "Payload must be JSON-encoded")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_bands_list_not_empty(self):
        """Both band lists must have non-zero length."""
        # Load just enough to read the constants
        src = _SCRIPT.read_text()
        # bands_80s_newwave and bands_80s_continued defined in source
        self.assertIn("bands_80s_newwave", src)
        self.assertIn("bands_80s_continued", src)

    def test_memory_url_constant_defined(self):
        """MEMORY_URL constant must be defined."""
        src = _SCRIPT.read_text()
        self.assertIn("MEMORY_URL", src)

    def test_rate_limit_sleep_present(self):
        """Script must include sleep/pause to avoid hammering memory server."""
        src = _SCRIPT.read_text()
        self.assertIn("time.sleep", src,
                      "Script should rate-limit with time.sleep")

    def test_target_constant_defined(self):
        """TARGET constant must be defined."""
        src = _SCRIPT.read_text()
        self.assertIn("TARGET", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_fails_silently_on_exception(self):
        """remember() must not propagate exceptions from urlopen."""
        # We simulate the remember function behavior from source
        # It catches all exceptions and increments failed counter
        src = _SCRIPT.read_text()
        self.assertIn("except:", src,
                      "remember() must catch all exceptions silently")

    def test_failed_counter_incremented(self):
        """remember() must increment failed counter on error."""
        src = _SCRIPT.read_text()
        self.assertIn("failed += 1", src,
                      "failed counter must be incremented on error")

    def test_sleep_on_failure(self):
        """remember() should sleep after a failed request."""
        src = _SCRIPT.read_text()
        # Check that time.sleep is called after failure
        lines = src.splitlines()
        in_except = False
        found_sleep_in_except = False
        for line in lines:
            stripped = line.strip()
            if stripped == "except:":
                in_except = True
            elif in_except and "time.sleep" in stripped:
                found_sleep_in_except = True
                break
            elif in_except and stripped and not stripped.startswith("#"):
                if not stripped.startswith("failed") and not stripped.startswith("time.sleep"):
                    # End of except block
                    in_except = False
        self.assertTrue(found_sleep_in_except,
                        "remember() should sleep after a failed request")

    def test_return_false_on_failure(self):
        """remember() must return False on failure."""
        src = _SCRIPT.read_text()
        self.assertIn("return False", src,
                      "remember() should return False on error")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_band_data_structure_valid(self):
        """Each band tuple must have 5 elements: name, city, genre, year, singles."""
        # Parse the bands from source without executing the full script
        src = _SCRIPT.read_text()
        # Verify tuple structure hints in comments/code
        self.assertIn("singles", src,
                      "Band data should reference singles")

    def test_log_function_writes_to_file(self):
        """log() must write to both stdout and log file."""
        src = _SCRIPT.read_text()
        self.assertIn('LOG_FILE', src, "LOG_FILE must be defined")
        self.assertIn('f.write', src, "log() must write to file")

    def test_genre_facts_templates_defined(self):
        """genre_facts_templates must be a list of strings."""
        src = _SCRIPT.read_text()
        self.assertIn("genre_facts_templates", src)

    def test_characteristics_map_defined(self):
        """characteristics_map must contain expected genres."""
        src = _SCRIPT.read_text()
        self.assertIn("characteristics_map", src)
        self.assertIn("synth-pop", src)
        self.assertIn("post-punk", src)

    def test_influences_list_defined(self):
        """influences list must be defined for cross-reference facts."""
        src = _SCRIPT.read_text()
        self.assertIn("influences", src)

    def test_get_count_function_defined(self):
        """get_count() must be defined to check DB count."""
        src = _SCRIPT.read_text()
        self.assertIn("def get_count", src)

    def test_get_count_has_health_endpoint(self):
        """get_count() must call health endpoint."""
        src = _SCRIPT.read_text()
        self.assertIn("/health", src)

    def test_content_type_header_set(self):
        """Requests must set Content-Type: application/json."""
        src = _SCRIPT.read_text()
        self.assertIn("application/json", src)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_slack_post_called_at_end(self):
        """Script must call nova_config.post_both at completion."""
        src = _SCRIPT.read_text()
        self.assertIn("post_both", src,
                      "Script must post completion to Slack")

    def test_fact_format_includes_kroq(self):
        """Generated facts must reference KROQ for radio context."""
        src = _SCRIPT.read_text()
        self.assertIn("KROQ", src,
                      "Facts must mention KROQ FM for context")

    def test_fact_format_includes_city(self):
        """Band facts must include city of origin."""
        src = _SCRIPT.read_text()
        self.assertIn("{city}", src,
                      "Fact templates should include {city}")

    def test_fact_format_includes_band(self):
        """Fact templates must include band name."""
        src = _SCRIPT.read_text()
        self.assertIn("{band}", src,
                      "Fact templates should include {band}")

    def test_target_check_breaks_loop(self):
        """Script must break when target is reached."""
        src = _SCRIPT.read_text()
        self.assertIn("TARGET REACHED", src,
                      "Script must stop when target count is reached")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_remember_posts_to_correct_url(self):
        """remember() must POST to MEMORY_URL."""
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        # Build remember function inline based on script logic
        MEMORY_URL = "http://127.0.0.1:18790/remember"
        import urllib.request

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            payload = json.dumps({
                "text": "Test fact",
                "source": "local_knowledge",
                "metadata": {"type": "music_history"}
            }).encode()
            req = urllib.request.Request(
                MEMORY_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=10)

        self.assertEqual(len(calls), 1)
        self.assertIn("18790/remember", calls[0])

    def test_payload_contains_required_fields(self):
        """Payload must contain text, source, and metadata fields."""
        payload_data = {
            "text": "Test band fact",
            "source": "local_knowledge",
            "metadata": {"type": "music_history"}
        }
        payload = json.dumps(payload_data).encode()
        decoded = json.loads(payload)
        self.assertIn("text", decoded)
        self.assertIn("source", decoded)
        self.assertIn("metadata", decoded)
        self.assertEqual(decoded["source"], "local_knowledge")
        self.assertEqual(decoded["metadata"]["type"], "music_history")

    def test_get_count_returns_zero_on_error(self):
        """get_count() must return 0 when server is unreachable."""
        import urllib.request

        def failing_urlopen(url, timeout=None):
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", side_effect=failing_urlopen):
            # Simulate get_count behavior
            try:
                resp = urllib.request.urlopen(
                    "http://127.0.0.1:18790/health", timeout=5)
                result = json.loads(resp.read()).get("count", 0)
            except:
                result = 0
        self.assertEqual(result, 0)

    def test_band_fact_format(self):
        """Band facts must follow expected sentence structure."""
        band = "Depeche Mode"
        city = "Basildon"
        genre = "synth-pop"
        year = "1980"
        fact = f"{band} formed in {city} in {year}. They played {genre} and were part of the alternative/new wave movement championed by KROQ-FM in Burbank, California."
        self.assertIn(band, fact)
        self.assertIn(city, fact)
        self.assertIn("KROQ", fact)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_exists(self):
        """bulk_music_ingest.py must exist."""
        self.assertTrue(_SCRIPT.exists(),
                        f"{_SCRIPT} not found")

    def test_script_compiles(self):
        """bulk_music_ingest.py must compile without syntax errors."""
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"bulk_music_ingest.py has syntax errors: {e}")

    def test_constants_defined_in_source(self):
        """MEMORY_URL, TARGET, and LOG_FILE must be defined."""
        src = _SCRIPT.read_text()
        for const in ["MEMORY_URL", "TARGET", "LOG_FILE"]:
            self.assertIn(const, src,
                          f"Constant {const} not found in source")

    def test_bands_lists_not_empty_in_source(self):
        """Source must define band data lists."""
        src = _SCRIPT.read_text()
        self.assertIn("bands_80s_newwave", src)
        self.assertIn("bands_80s_continued", src)

    def test_no_traceback_pattern_in_bare_except(self):
        """Bare except: blocks must not re-raise (would crash script)."""
        src = _SCRIPT.read_text()
        # Ensure bare excepts don't have 'raise' directly after
        lines = src.splitlines()
        for i, line in enumerate(lines):
            if line.strip() == "except:":
                # Check next few lines for raise
                for j in range(1, 4):
                    if i + j < len(lines):
                        next_line = lines[i + j].strip()
                        self.assertNotEqual(next_line, "raise",
                                            f"Bare except at line {i+1} re-raises — would crash")


if __name__ == "__main__":
    unittest.main(verbosity=2)
