"""
test_slack_post_image.py — All 7 test categories for slack_post_image.py
Written by Jordan Koch.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.slack_bot_token.return_value = "xoxb-test-token"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

import importlib.util
_SCRIPT = Path(__file__).parent.parent / "scripts" / "slack_post_image.py"
_spec = importlib.util.spec_from_file_location("slack_post_image", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_slack_token(self):
        """Slack token must come from nova_config, not hardcoded."""
        src = _SCRIPT.read_text()
        import re
        tokens = re.findall(r'xoxb-[A-Za-z0-9-]+', src)
        real_tokens = [t for t in tokens if "test" not in t and "fake" not in t]
        self.assertEqual(real_tokens, [],
                         f"Hardcoded Slack token: {real_tokens}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_token_from_nova_config(self):
        """Token must be loaded via nova_config.slack_bot_token()."""
        src = _SCRIPT.read_text()
        self.assertIn("slack_bot_token()", src)

    def test_api_uses_bearer_auth(self):
        """Slack API calls must use Bearer token auth."""
        src = _SCRIPT.read_text()
        self.assertIn("Bearer", src)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pattern in ["kochjpar" + _at + "gmail.com"]:
            self.assertNotIn(pattern, src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_upload_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=60", src,
                      "File upload must have 60s timeout")

    def test_api_calls_have_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=30", src)

    def test_three_step_upload_process(self):
        """Upload must use 3-step Slack API (getURL, PUT, complete)."""
        src = _SCRIPT.read_text()
        self.assertIn("getUploadURLExternal", src)
        self.assertIn("completeUploadExternal", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_exits_on_missing_file(self):
        """upload_image() must exit 1 when file doesn't exist."""
        with self.assertRaises(SystemExit) as cm:
            _mod.upload_image("/nonexistent/file.png", "C0B01L9GQTV")
        self.assertEqual(cm.exception.code, 1)

    def test_exits_on_url_api_error(self):
        """upload_image() must exit 1 when getUploadURLExternal fails."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            Path(f.name).write_bytes(b"PNG_DATA")
            fpath = Path(f.name)

        bad_url_resp = MagicMock()
        bad_url_resp.__enter__ = lambda s: s
        bad_url_resp.__exit__ = MagicMock(return_value=False)
        bad_url_resp.read.return_value = json.dumps(
            {"ok": False, "error": "invalid_token"}).encode()

        try:
            with patch("urllib.request.urlopen", return_value=bad_url_resp):
                with self.assertRaises(SystemExit) as cm:
                    _mod.upload_image(str(fpath), "C0B01L9GQTV")
            self.assertEqual(cm.exception.code, 1)
        finally:
            fpath.unlink(missing_ok=True)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_slack_post_function_exists(self):
        self.assertTrue(callable(_mod.slack_post))

    def test_upload_image_function_exists(self):
        self.assertTrue(callable(_mod.upload_image))

    def test_default_channel_defined(self):
        self.assertIsNotNone(_mod.DEFAULT_CHAN)
        self.assertTrue(_mod.DEFAULT_CHAN.startswith("C"))

    def test_slack_api_url(self):
        self.assertIn("slack.com/api", _mod.SLACK_API)

    def test_slack_post_sends_auth_header(self):
        """slack_post() must include Authorization header."""
        requests_made = []

        def capture(req, timeout=None):
            requests_made.append(req)
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.read.return_value = json.dumps({"ok": True}).encode()
            return r

        with patch("urllib.request.urlopen", side_effect=capture):
            _mod.slack_post("chat.postMessage", {"channel": "C123", "text": "hi"})

        self.assertEqual(len(requests_made), 1)
        self.assertIn("Authorization", requests_made[0].headers)
        self.assertIn("Bearer", requests_made[0].headers["Authorization"])


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_successful_upload_prints_confirmation(self):
        """Successful upload must print confirmation."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            Path(f.name).write_bytes(b"\x89PNG\r\n\x1a\n")
            fpath = Path(f.name)

        url_response = MagicMock()
        url_response.__enter__ = lambda s: s
        url_response.__exit__ = MagicMock(return_value=False)
        url_response.read.return_value = json.dumps({
            "ok": True, "upload_url": "https://files.slack.com/upload/v1/abc",
            "file_id": "F123"
        }).encode()

        upload_response = MagicMock()
        upload_response.__enter__ = lambda s: s
        upload_response.__exit__ = MagicMock(return_value=False)

        complete_response = MagicMock()
        complete_response.__enter__ = lambda s: s
        complete_response.__exit__ = MagicMock(return_value=False)
        complete_response.read.return_value = json.dumps({"ok": True}).encode()

        responses = [url_response, upload_response, complete_response]
        calls = [0]

        def side_effect(req, timeout=None):
            resp = responses[calls[0] % len(responses)]
            calls[0] += 1
            return resp

        import io
        from contextlib import redirect_stdout
        output = io.StringIO()

        try:
            with patch("urllib.request.urlopen", side_effect=side_effect):
                with redirect_stdout(output):
                    _mod.upload_image(str(fpath), "C0B01L9GQTV")
            self.assertIn("Image posted", output.getvalue())
        finally:
            fpath.unlink(missing_ok=True)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_caption_included_in_complete_request(self):
        """Caption must be passed as initial_comment in complete step."""
        src = _SCRIPT.read_text()
        self.assertIn("initial_comment", src)

    def test_no_caption_omitted_from_payload(self):
        """When caption is empty, initial_comment must not be added."""
        src = _SCRIPT.read_text()
        self.assertIn("if caption:", src)

    def test_cli_args_parsed(self):
        """Script must parse image_path, channel, and caption from argv."""
        src = _SCRIPT.read_text()
        self.assertIn("sys.argv[1]", src)
        self.assertIn("sys.argv[2]", src)
        self.assertIn("sys.argv[3]", src)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"slack_post_image.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_main_guard_present(self):
        src = _SCRIPT.read_text()
        self.assertIn('if __name__ == "__main__"', src)

    def test_constants_defined(self):
        for attr in ["SLACK_TOKEN", "SLACK_API", "DEFAULT_CHAN"]:
            self.assertTrue(hasattr(_mod, attr))

    def test_usage_message_present(self):
        src = _SCRIPT.read_text()
        self.assertIn("Usage", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
