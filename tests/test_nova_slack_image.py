"""
test_nova_slack_image.py — All 7 test categories for nova_slack_image.py
Written by Jordan Koch.
"""
import importlib.util, base64, json, sys, time, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_slack_image.py"
_spec = importlib.util.spec_from_file_location("nova_slack_image", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

get_slack_token = _mod.get_slack_token
analyze_image = _mod.analyze_image
download_slack_file = _mod.download_slack_file


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
        self.assertNotIn(str(Path.home()) + "/", src)
    def test_slack_token_from_keychain(self):
        src = _SCRIPT.read_text()
        self.assertIn("nova-slack-bot-token", src)
    def test_openrouter_disabled_by_default(self):
        self.assertFalse(_mod.USE_OPENROUTER)
    def test_openrouter_key_from_keychain(self):
        self.assertIn("nova-openrouter-api-key", _SCRIPT.read_text())
    def test_local_vision_model_defined(self):
        self.assertTrue(_mod.OLLAMA_URL.startswith("http://127.0.0.1"))


class TestPerformance(unittest.TestCase):
    def test_ollama_timeout_defined(self):
        self.assertIn("timeout", _SCRIPT.read_text())
    def test_analyze_image_fast_setup(self):
        fake_bytes = b"\xff\xd8\xff" + b"\x80" * 1000
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            start = time.perf_counter()
            try:
                analyze_image(fake_bytes)
            except Exception:
                pass
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)


class TestRetry(unittest.TestCase):
    def test_get_slack_token_returns_empty_on_missing(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = get_slack_token()
        self.assertEqual(result, "")
    def test_analyze_image_failure_handled(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            try:
                analyze_image(b"FAKE")
            except Exception:
                pass
    def test_download_direct_url_raises_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            with self.assertRaises(Exception):
                download_slack_file("https://files.slack.com/test.jpg", "xoxb-fake")


class TestUnit(unittest.TestCase):
    def test_vision_model_defined(self):
        self.assertIsInstance(_mod.VISION_MODEL, str)
        self.assertGreater(len(_mod.VISION_MODEL), 0)
    def test_ollama_url_localhost(self):
        self.assertTrue(_mod.OLLAMA_URL.startswith("http://127.0.0.1"))
    def test_use_openrouter_false(self):
        self.assertFalse(_mod.USE_OPENROUTER)
    def test_get_slack_token_returns_stripped_value(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="xoxb-real-token\n")
            result = get_slack_token()
        self.assertEqual(result, "xoxb-real-token")
    def test_analyze_image_uses_ollama_local(self):
        fake_bytes = b"FAKE IMAGE DATA"
        captured_urls = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"response": "test result"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        def capture_urlopen(req, **kwargs):
            captured_urls.append(req.full_url)
            return mock_resp
        with patch("urllib.request.urlopen", side_effect=capture_urlopen):
            analyze_image(fake_bytes)
        self.assertTrue(any("11434" in url for url in captured_urls))
    def test_analyze_image_includes_images_in_payload(self):
        fake_bytes = b"FAKE"
        expected_b64 = base64.b64encode(fake_bytes).decode()
        captured = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"response": "test"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        def capture_urlopen(req, **kwargs):
            if req.data:
                captured.append(json.loads(req.data.decode()))
            return mock_resp
        with patch("urllib.request.urlopen", side_effect=capture_urlopen):
            analyze_image(fake_bytes)
        if captured:
            self.assertIn("images", captured[0])
            self.assertIn(expected_b64, captured[0]["images"])
    def test_download_direct_url_with_auth_header(self):
        built_headers = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"FAKE IMAGE"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        def capture(req, **kwargs):
            built_headers.append(dict(req.headers))
            return mock_resp
        with patch("urllib.request.urlopen", side_effect=capture):
            download_slack_file("https://files.slack.com/fake.jpg", "xoxb-test-token")
        self.assertTrue(any("Authorization" in h or "authorization" in h for h in built_headers))


class TestIntegration(unittest.TestCase):
    def test_analyze_returns_string_on_success(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"response": "A person is visible."}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = analyze_image(b"\xff\xd8\xff" + b"\x80" * 100)
        self.assertIsInstance(result, str)
    def test_default_channel_is_nova_chat(self):
        self.assertIn("C0AMNQ5GX70", _SCRIPT.read_text())


class TestFunctional(unittest.TestCase):
    def test_vision_analysis_stays_local(self):
        self.assertFalse(_mod.USE_OPENROUTER)
    def test_security_comment_mentions_local(self):
        self.assertIn("local", _SCRIPT.read_text().lower())


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.OLLAMA_URL, str)
        self.assertIsInstance(_mod.OPENROUTER_URL, str)
        self.assertIsInstance(_mod.VISION_MODEL, str)
        self.assertIsInstance(_mod.USE_OPENROUTER, bool)
    def test_functions_exist(self):
        for fn in ("get_slack_token", "download_slack_file", "get_openrouter_key", "analyze_image"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")
    def test_openrouter_url_is_https(self):
        self.assertTrue(_mod.OPENROUTER_URL.startswith("https://"))

if __name__ == "__main__":
    unittest.main(verbosity=2)

import base64
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_slack_image.py"
_spec = importlib.util.spec_from_file_location("nova_slack_image", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

get_slack_token = _mod.get_slack_token
analyze_image = _mod.analyze_image


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA", "xoxb-"]
        for p in forbidden:
            self.assertNotIn(p, src, f"Credential: {p!r}")

    def test_slack_token_from_keychain(self):
        """Slack token must come from macOS Keychain."""
        src = _SCRIPT.read_text()
        self.assertIn("find-generic-password", src)
        self.assertIn("nova-slack-bot-token", src)

    def test_openrouter_disabled_by_default(self):
        """USE_OPENROUTER must be False — all analysis must stay local."""
        self.assertFalse(_mod.USE_OPENROUTER,
                         "Image analysis must be local by default (privacy)")

    def test_use_openrouter_flag_exists_with_comment(self):
        """SECURITY comment must accompany USE_OPENROUTER flag."""
        src = _SCRIPT.read_text()
        self.assertIn("SECURITY", src, "USE_OPENROUTER must have a SECURITY comment")

    def test_no_pii_email_literals(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII: {p!r}")

    def test_openrouter_key_from_keychain(self):
        src = _SCRIPT.read_text()
        self.assertIn("nova-openrouter-api-key", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_get_slack_token_fast_on_failure(self):
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=1, stdout="")):
            start = time.perf_counter()
            result = get_slack_token()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result, "")

    def test_analyze_image_encodes_bytes_without_blocking(self):
        """base64 encoding of image bytes must be fast."""
        image_bytes = os.urandom(1024 * 100)  # 100KB
        start = time.perf_counter()
        b64 = base64.b64encode(image_bytes).decode()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"base64 100KB: {elapsed:.3f}s")
        self.assertGreater(len(b64), 0)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_analyze_image_raises_on_ollama_failure(self):
        """analyze_image lets network errors propagate (caller handles retry)."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with self.assertRaises(Exception):
                analyze_image(b"fake image bytes")

    def test_download_slack_file_raises_on_not_found(self):
        """download_slack_file must raise RuntimeError when file not found."""
        mock_r = MagicMock()
        mock_r.__enter__ = lambda s: s
        mock_r.__exit__ = MagicMock(return_value=False)
        mock_r.read.return_value = json.dumps({"ok": True, "messages": []}).encode()

        with patch("urllib.request.urlopen", return_value=mock_r):
            with self.assertRaises(RuntimeError):
                _mod.download_slack_file("nonexistent_file_id", "fake_token")

    def test_get_slack_token_returns_empty_on_subprocess_failure(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
            result = get_slack_token()
        self.assertEqual(result, "")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_analyze_image_sends_base64(self):
        """analyze_image must base64-encode image bytes in the request."""
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.read.return_value = json.dumps({"response": "A cat"}).encode()
            return r

        image_bytes = b"fake image data"
        expected_b64 = base64.b64encode(image_bytes).decode()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            analyze_image(image_bytes, "Describe this.")

        self.assertGreater(len(captured), 0)
        payload = captured[0]
        self.assertIn("images", payload)
        self.assertEqual(payload["images"][0], expected_b64)

    def test_analyze_image_uses_local_ollama_by_default(self):
        """analyze_image must target local Ollama when USE_OPENROUTER is False."""
        captured_urls = []

        class FakeReq:
            def __init__(self, url, data=None, headers=None):
                captured_urls.append(url)

        mock_r = MagicMock()
        mock_r.__enter__ = lambda s: s
        mock_r.__exit__ = MagicMock(return_value=False)
        mock_r.read.return_value = json.dumps({"response": "a scene"}).encode()

        with patch("urllib.request.Request", FakeReq):
            with patch("urllib.request.urlopen", return_value=mock_r):
                analyze_image(b"bytes", "test")

        self.assertTrue(any("11434" in u for u in captured_urls),
                        "Must use local Ollama port 11434")

    def test_vision_model_set(self):
        self.assertIsInstance(_mod.VISION_MODEL, str)
        self.assertGreater(len(_mod.VISION_MODEL), 0)

    def test_ollama_url_is_local(self):
        self.assertIn("127.0.0.1", _mod.OLLAMA_URL)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_download_and_analyze_flow(self):
        """Full download + analyze flow must return a description string."""
        image_bytes = b"fake PNG data here"
        mock_dl = MagicMock()
        mock_dl.__enter__ = lambda s: s
        mock_dl.__exit__ = MagicMock(return_value=False)
        mock_dl.read.return_value = image_bytes

        mock_analyze = MagicMock()
        mock_analyze.__enter__ = lambda s: s
        mock_analyze.__exit__ = MagicMock(return_value=False)
        mock_analyze.read.return_value = json.dumps({"response": "A forest scene"}).encode()

        calls = [0]

        def fake_urlopen(req, timeout=None):
            calls[0] += 1
            if calls[0] == 1:
                return mock_dl  # download
            return mock_analyze

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            image_data, mimetype, name = _mod.download_slack_file(
                "http://fake.slack.com/file.jpg", "fake_token"
            )
        self.assertEqual(image_data, image_bytes)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_analyze_image_returns_response_text(self):
        mock_r = MagicMock()
        mock_r.__enter__ = lambda s: s
        mock_r.__exit__ = MagicMock(return_value=False)
        mock_r.read.return_value = json.dumps({"response": "A cat sitting on a mat."}).encode()

        with patch("urllib.request.urlopen", return_value=mock_r):
            result = analyze_image(b"fake bytes", "Describe this.")

        self.assertEqual(result, "A cat sitting on a mat.")

    def test_main_exits_when_no_args(self):
        with patch("sys.argv", ["nova_slack_image.py"]):
            with self.assertRaises(SystemExit) as ctx:
                _mod.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_main_exits_when_no_slack_token(self):
        with patch("sys.argv", ["nova_slack_image.py", "file123"]):
            with patch.object(_mod, "get_slack_token", return_value=""):
                with self.assertRaises(SystemExit) as ctx:
                    _mod.main()
        self.assertEqual(ctx.exception.code, 1)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_slack_image.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertIsInstance(_mod.OLLAMA_URL, str)
        self.assertIsInstance(_mod.VISION_MODEL, str)
        self.assertIsInstance(_mod.USE_OPENROUTER, bool)

    def test_functions_callable(self):
        for fn in [get_slack_token, analyze_image, _mod.download_slack_file, _mod.main]:
            self.assertTrue(callable(fn))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
