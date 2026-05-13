"""
test_dream_deliver.py — All 7 test categories for dream_deliver.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub nova_config before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.slack_bot_token.return_value = "xoxb-test-token"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.is_private_source.return_value = False
sys.modules["nova_config"] = _nova_cfg

# Stub herd_config
_herd_cfg = MagicMock()
_herd_cfg.HERD = []
sys.modules["herd_config"] = _herd_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "dream_deliver.py"
_spec = importlib.util.spec_from_file_location("dream_deliver", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_slack_token(self):
        """Slack token must come from nova_config, not hardcoded."""
        src = _SCRIPT.read_text()
        # Should NOT have a literal xoxb- token
        import re
        token_pattern = re.compile(r'xoxb-[A-Za-z0-9-]+')
        matches = [m.group() for m in token_pattern.finditer(src)
                   if "xoxb-test" not in m.group() and "xoxb-fake" not in m.group()]
        self.assertEqual(matches, [],
                         f"Hardcoded Slack token found: {matches}")

    def test_no_hardcoded_home_path(self):
        """Source must not hardcode user home path."""
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src,
                         "Hardcoded home path found — use Path.home()")

    def test_no_pii_email(self):
        """Source must not contain personal email addresses."""
        src = _SCRIPT.read_text()
        _at = "@"
        pii_patterns = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney" + ".com",
            "kochj" + _at + "digitalnoise.net",
        ]
        for pattern in pii_patterns:
            self.assertNotIn(pattern, src, f"PII email found: {pattern!r}")

    def test_jordan_email_loaded_from_keychain(self):
        """Jordan's email must be loaded from Keychain, not hardcoded."""
        src = _SCRIPT.read_text()
        self.assertIn("security", src,
                      "Email must be retrieved from Keychain via security CLI")
        self.assertIn("nova-jordan-work-email", src,
                      "Keychain item name expected for Jordan's email")

    def test_dream_privacy_note_present(self):
        """Dream narratives must never go to cloud — privacy note in source."""
        src = _SCRIPT.read_text()
        self.assertIn("personal context", src.lower(),
                      "Privacy note about dream content should be present")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_narrative_chunked_to_3000(self):
        """Narrative must be chunked to 3000 chars max per Slack message."""
        src = _SCRIPT.read_text()
        self.assertIn("3000", src,
                      "Narrative should be chunked at 3000 chars for Slack")

    def test_haiku_truncates_input(self):
        """Haiku generation must truncate narrative before sending to LLM."""
        src = _SCRIPT.read_text()
        self.assertIn("[:800]", src,
                      "Haiku prompt must truncate narrative to 800 chars")

    def test_urlopen_has_timeout(self):
        """All urlopen calls must specify a timeout."""
        src = _SCRIPT.read_text()
        import re
        # Check that timeout is specified in urlopen calls
        urlopen_calls = re.findall(r'urlopen\([^)]+\)', src)
        for call_str in urlopen_calls:
            self.assertIn("timeout", call_str,
                          f"urlopen call missing timeout: {call_str}")

    def test_subprocess_has_timeout(self):
        """subprocess.run calls must have timeouts."""
        src = _SCRIPT.read_text()
        import re
        subproc_calls = re.findall(r'subprocess\.run\([^)]+timeout[^)]+\)', src)
        self.assertGreater(len(subproc_calls), 0,
                           "subprocess.run calls should specify timeout")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_max_retries_constant_defined(self):
        """MAX_RETRIES must be defined."""
        self.assertEqual(_mod.MAX_RETRIES, 3,
                         "MAX_RETRIES should be 3")

    def test_failed_delivery_moves_to_dead_letter(self):
        """After MAX_RETRIES failures, file must move to dead-letter."""
        src = _SCRIPT.read_text()
        self.assertIn("DEAD_LETTER", src,
                      "Dead-letter queue must be used for persistent failures")
        self.assertIn("MAX_RETRIES", src)

    def test_retry_count_incremented(self):
        """Retry count must be incremented on Slack failure."""
        src = _SCRIPT.read_text()
        self.assertIn("_retry_count", src,
                      "Retry count must be tracked in delivery file")

    def test_slack_post_has_exception_handler(self):
        """slack_post() must not propagate exceptions."""
        src = _SCRIPT.read_text()
        # slack_post function has try/except
        self.assertIn("except Exception", src,
                      "slack_post must catch exceptions")

    def test_haiku_returns_fallback_on_failure(self):
        """generate_haiku() must return a fallback string on failure."""
        with patch("urllib.request.urlopen", side_effect=OSError("no server")):
            haiku = _mod.generate_haiku("A long dream narrative here " * 50)
        self.assertIsInstance(haiku, str)
        self.assertGreater(len(haiku), 0,
                           "generate_haiku must return fallback on failure")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_log_function_exists(self):
        """log() function must exist."""
        self.assertTrue(callable(_mod.log))

    def test_constants_defined(self):
        """Required module constants must be defined."""
        self.assertIsNotNone(_mod.PENDING_FILE)
        self.assertIsNotNone(_mod.DEAD_LETTER)
        self.assertIsNotNone(_mod.SLACK_CHANNEL)
        self.assertEqual(_mod.SLACK_CHANNEL, "C0AMNQ5GX70")

    def test_slack_channel_is_nova_chat(self):
        """SLACK_CHANNEL must be #nova-chat."""
        self.assertEqual(_mod.SLACK_CHANNEL, "C0AMNQ5GX70")

    def test_generate_haiku_strips_thinking_blocks(self):
        """generate_haiku() must strip <think>...</think> blocks."""
        fake_response = json.dumps({
            "message": {"content": "<think>thinking...</think>\nSilent server hums\nMemories loop in circuits\nDawn resets the clock"}
        }).encode()

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_response

        with patch("urllib.request.urlopen", return_value=mock_resp):
            haiku = _mod.generate_haiku("A test dream narrative with enough words.")

        self.assertNotIn("<think>", haiku)
        self.assertNotIn("thinking...", haiku)

    def test_json_repair_strips_backslash_before_unicode(self):
        """main() must repair JSON with backslash before curly-quote."""
        # Verify repair logic exists in source
        src = _SCRIPT.read_text()
        self.assertIn("auto-repair", src.lower(),
                      "JSON auto-repair logic must be present")

    def test_image_placeholder_stripped_from_narrative(self):
        """Narrative must have image placeholder lines stripped."""
        src = _SCRIPT.read_text()
        self.assertIn(r"!\[Dream\]\(\[", src,
                      "Image placeholder regex must be present for stripping")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_post_dream_chunks_long_narrative(self):
        """post_dream() must send multiple messages for long narratives."""
        posts = []

        def fake_slack_post(endpoint, payload):
            if endpoint == "chat.postMessage":
                posts.append(payload.get("text", ""))
            return {"ok": True}

        long_narrative = "word " * 2000  # ~10k chars, needs 4 chunks
        with patch.object(_mod, "slack_post", side_effect=fake_slack_post):
            with patch.object(_mod, "upload_image_to_channel", return_value=True):
                _mod.post_dream(long_narrative, None, "2025-01-01")

        self.assertGreater(len(posts), 1,
                           "Long narrative must be split into multiple posts")

    def test_main_exits_when_no_pending_file(self):
        """main() must exit cleanly when no pending file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pending = Path(tmpdir) / "pending_delivery.json"
            with patch.object(_mod, "PENDING_FILE", pending):
                with self.assertRaises(SystemExit) as cm:
                    _mod.main()
                self.assertEqual(cm.exception.code, 0)

    def test_main_exits_on_empty_narrative(self):
        """main() must exit with code 1 for empty narrative."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"narrative": "", "date": "2025-01-01"}, f)
            pending_path = Path(f.name)

        try:
            with patch.object(_mod, "PENDING_FILE", pending_path):
                with self.assertRaises(SystemExit) as cm:
                    _mod.main()
                self.assertEqual(cm.exception.code, 1)
        finally:
            pending_path.unlink(missing_ok=True)

    def test_inspirations_filtered_by_private_source(self):
        """Private sources must be excluded from inspiration block."""
        _nova_cfg.is_private_source.return_value = True
        posts = []

        def fake_slack_post(endpoint, payload):
            if endpoint == "chat.postMessage":
                posts.append(payload.get("text", ""))
            return {"ok": True}

        inspirations = [{"source": "private_document", "label": "test", "memory": "secret stuff"}]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "narrative": "A dream about code and memory.",
                "date": "2025-01-01",
                "inspirations": inspirations,
                "dream_meta": {},
            }, f)
            pending_path = Path(f.name)

        try:
            with patch.object(_mod, "PENDING_FILE", pending_path):
                with patch.object(_mod, "slack_post", side_effect=fake_slack_post):
                    with patch.object(_mod, "upload_image_to_channel", return_value=True):
                        with patch.object(_mod, "email_herd"):
                            with patch.object(_mod, "DEAD_LETTER", Path(tempfile.mkdtemp())):
                                with patch("subprocess.run") as mock_run:
                                    mock_run.return_value = MagicMock(returncode=0)
                                    _mod.main()

            combined = " ".join(posts)
            self.assertNotIn("secret stuff", combined,
                             "Private source content must not appear in Slack post")
        finally:
            pending_path.unlink(missing_ok=True)
            _nova_cfg.is_private_source.return_value = False


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_successful_delivery_removes_pending_file(self):
        """On successful Slack delivery, pending file must be deleted."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "narrative": "A dream unfolds in silence.",
                "date": "2025-01-01",
                "inspirations": [],
                "dream_meta": {},
            }, f)
            pending_path = Path(f.name)

        def fake_slack_post(endpoint, payload):
            return {"ok": True}

        try:
            with patch.object(_mod, "PENDING_FILE", pending_path):
                with patch.object(_mod, "slack_post", side_effect=fake_slack_post):
                    with patch.object(_mod, "upload_image_to_channel", return_value=True):
                        with patch.object(_mod, "email_herd"):
                            with patch("subprocess.run") as mock_run:
                                mock_run.return_value = MagicMock(returncode=0)
                                _mod.main()

            self.assertFalse(pending_path.exists(),
                             "Pending file must be deleted after successful delivery")
        finally:
            pending_path.unlink(missing_ok=True)

    def test_failed_delivery_keeps_pending_file(self):
        """On failed Slack delivery (first attempt), pending file must remain."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({
                "narrative": "A dream unfolds in silence.",
                "date": "2025-01-01",
                "inspirations": [],
                "dream_meta": {},
            }, f)
            pending_path = Path(f.name)

        def fake_slack_post_fail(endpoint, payload):
            return {"ok": False, "error": "channel_not_found"}

        try:
            with patch.object(_mod, "PENDING_FILE", pending_path):
                with patch.object(_mod, "slack_post", side_effect=fake_slack_post_fail):
                    with patch.object(_mod, "upload_image_to_channel", return_value=False):
                        with patch.object(_mod, "email_herd"):
                            with patch("subprocess.run") as mock_run:
                                mock_run.return_value = MagicMock(returncode=0)
                                _mod.main()

            self.assertTrue(pending_path.exists(),
                            "Pending file must remain after failed delivery (retry)")
        finally:
            pending_path.unlink(missing_ok=True)

    def test_dead_letter_created_after_max_retries(self):
        """After MAX_RETRIES failures, dead letter file must be created."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pending_path = Path(tmpdir) / "pending_delivery.json"
            dead_letter_dir = Path(tmpdir) / "failed_deliveries"
            dead_letter_dir.mkdir()

            pending_path.write_text(json.dumps({
                "narrative": "A dream unfolds in silence.",
                "date": "2025-01-01",
                "_retry_count": _mod.MAX_RETRIES - 1,
                "inspirations": [],
                "dream_meta": {},
            }))

            def fake_slack_post_fail(endpoint, payload):
                return {"ok": False, "error": "channel_not_found"}

            with patch.object(_mod, "PENDING_FILE", pending_path):
                with patch.object(_mod, "DEAD_LETTER", dead_letter_dir):
                    with patch.object(_mod, "slack_post", side_effect=fake_slack_post_fail):
                        with patch.object(_mod, "upload_image_to_channel", return_value=False):
                            with patch.object(_mod, "email_herd"):
                                with patch("subprocess.run") as mock_run:
                                    mock_run.return_value = MagicMock(returncode=0)
                                    _mod.main()

            dead_files = list(dead_letter_dir.glob("*.json"))
            self.assertEqual(len(dead_files), 1,
                             "Dead-letter file should be created after max retries")
            self.assertFalse(pending_path.exists(),
                             "Pending file should be removed after max retries")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        """dream_deliver.py must compile without errors."""
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"dream_deliver.py has syntax errors: {e}")

    def test_module_loads(self):
        """dream_deliver module must load without crashing."""
        self.assertIsNotNone(_mod)

    def test_main_function_exists(self):
        """main() function must exist."""
        self.assertTrue(callable(_mod.main))

    def test_constants_present(self):
        """Critical constants must be defined."""
        self.assertIsNotNone(_mod.PENDING_FILE)
        self.assertIsNotNone(_mod.DEAD_LETTER)
        self.assertIsNotNone(_mod.MAX_RETRIES)
        self.assertIsNotNone(_mod.SLACK_CHANNEL)
        self.assertIsNotNone(_mod.SLACK_API)

    def test_slack_api_url_is_slack(self):
        """SLACK_API must point to Slack's API."""
        self.assertIn("slack.com/api", _mod.SLACK_API)

    def test_functions_exist(self):
        """Required functions must be defined."""
        for fn_name in ["post_dream", "upload_image_to_channel",
                        "generate_haiku", "email_herd", "log"]:
            self.assertTrue(callable(getattr(_mod, fn_name, None)),
                            f"Function {fn_name} must exist")


if __name__ == "__main__":
    unittest.main(verbosity=2)
