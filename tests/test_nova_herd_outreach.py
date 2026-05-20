"""
test_nova_herd_outreach.py — All 7 test categories for nova_herd_outreach.py
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
sys.modules["herd_config"] = MagicMock(HERD=[
    {"name": "Sam", "email": "sam@example.com", "profile": "sam.md"},
    {"name": "Gaston", "email": "gaston@example.com", "profile": "gaston.md"},
])
sys.modules["nova_strip_thinking"] = MagicMock(strip_thinking=lambda x: x)

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_herd_outreach.py"

# Python 3.9 compatibility: rewrite X | Y return type annotations
def _load_compat(script_path, module_name):
    src = script_path.read_text()
    if sys.version_info < (3, 10):
        src = re.sub(r'\)\s*->\s*(\w+)\s*\|\s*(\w+)\s*:', r') -> "\1 | \2":', src)
    mod = types.ModuleType(module_name)
    mod.__file__ = str(script_path)
    exec(compile(src, str(script_path), "exec"), mod.__dict__)
    return mod

_mod = _load_compat(_SCRIPT, "nova_herd_outreach")

already_reached_out_today = _mod.already_reached_out_today
read_file = _mod.read_file
send_email = _mod.send_email
maybe_attach_dream_image = _mod.maybe_attach_dream_image


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA", "password ="]
        for p in forbidden:
            self.assertNotIn(p, src, f"Credential: {p!r}")

    def test_no_pii_email_literals(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII: {p!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_herd_config_gitignored(self):
        """Script must import herd_config optionally (gitignored)."""
        src = _SCRIPT.read_text()
        self.assertIn("ImportError", src, "herd_config must be imported with ImportError fallback")

    def test_outreach_log_is_local(self):
        """OUTREACH_LOG must be under home directory."""
        self.assertTrue(str(_mod.OUTREACH_LOG).startswith(str(Path.home())))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_already_reached_out_today_fast(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("2026-05-13 09:00:00] Outreach sent to Sam\n")
            tmp = f.name
        with patch.object(_mod, "OUTREACH_LOG", Path(tmp)):
            start = time.perf_counter()
            for _ in range(100):
                already_reached_out_today()
            elapsed = time.perf_counter() - start
        os.unlink(tmp)
        self.assertLess(elapsed, 0.5, f"already_reached_out_today 100x: {elapsed:.3f}s")

    def test_read_file_fast(self):
        start = time.perf_counter()
        for _ in range(200):
            read_file(Path("/nonexistent/file.md"), 500)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_pick_recipient_returns_none_on_ollama_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = _mod.pick_recipient_and_angle()
        self.assertIsNone(result)

    def test_generate_outreach_email_returns_empty_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = _mod.generate_outreach_email("Sam", "sam@example.com", "angle", "hook")
        self.assertEqual(result, "")

    def test_send_email_returns_false_on_subprocess_error(self):
        with patch("subprocess.run", side_effect=Exception("process failed")):
            result = send_email("sam@example.com", "Subject", "Body")
        self.assertFalse(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_read_file_returns_empty_on_missing(self):
        result = read_file(Path("/nonexistent/xyz.md"), 500)
        self.assertEqual(result, "")

    def test_read_file_truncates(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("X" * 2000)
            tmp = f.name
        result = read_file(Path(tmp), 100)
        os.unlink(tmp)
        self.assertLessEqual(len(result), 100)

    def test_already_reached_out_today_false_on_no_log(self):
        with patch.object(_mod, "OUTREACH_LOG", Path("/nonexistent/log.log")):
            result = already_reached_out_today()
        self.assertFalse(result)

    def test_already_reached_out_today_true_when_log_has_today(self):
        today = _mod.TODAY
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(f"{today} 09:00:00] Outreach sent to Sam\n")
            tmp = f.name
        with patch.object(_mod, "OUTREACH_LOG", Path(tmp)):
            result = already_reached_out_today()
        os.unlink(tmp)
        self.assertTrue(result)

    def test_already_reached_out_today_false_old_log(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("2020-01-01 09:00:00] Outreach sent to Sam\n")
            tmp = f.name
        with patch.object(_mod, "OUTREACH_LOG", Path(tmp)):
            result = already_reached_out_today()
        os.unlink(tmp)
        self.assertFalse(result)

    def test_send_email_uses_herd_mail(self):
        """send_email must call nova_herd_mail.sh."""
        captured = []
        with patch("subprocess.run",
                   side_effect=lambda args, **kw: captured.append(args) or MagicMock(returncode=0)):
            send_email("sam@example.com", "Subject", "Body")
        self.assertTrue(any("nova_herd_mail.sh" in str(a) for a in captured))

    def test_maybe_attach_dream_image_returns_none_when_no_file(self):
        """maybe_attach_dream_image must return None when no dream image exists."""
        with patch("pathlib.Path.exists", return_value=False):
            result = maybe_attach_dream_image()
        self.assertIsNone(result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_skips_if_already_reached_out(self):
        """main() must skip if already_reached_out_today is True."""
        pick_calls = []
        with patch.object(_mod, "already_reached_out_today", return_value=True):
            with patch.object(_mod, "pick_recipient_and_angle",
                               side_effect=lambda: pick_calls.append(1)):
                _mod.main()
        self.assertEqual(len(pick_calls), 0)

    def test_main_skips_if_pick_returns_skip(self):
        """main() must skip when LLM returns skip=True."""
        send_calls = []
        with patch.object(_mod, "already_reached_out_today", return_value=False):
            with patch.object(_mod, "pick_recipient_and_angle",
                               return_value={"skip": True, "reason": "nothing genuine"}):
                with patch.object(_mod, "send_email",
                                   side_effect=lambda *a, **kw: send_calls.append(1)):
                    _mod.main()
        self.assertEqual(len(send_calls), 0)

    def test_main_sends_email_on_valid_decision(self):
        """main() must call send_email when a valid decision is returned."""
        send_calls = []
        with patch.object(_mod, "already_reached_out_today", return_value=False):
            with patch.object(_mod, "pick_recipient_and_angle", return_value={
                "recipient_email": "sam@example.com",
                "recipient_name": "Sam",
                "subject": "Test outreach",
                "angle": "Something interesting",
                "hook": "Sam would like this",
            }):
                with patch.object(_mod, "generate_outreach_email", return_value="Hey Sam!"):
                    with patch.object(_mod, "maybe_attach_dream_image", return_value=None):
                        with patch.object(_mod, "send_email",
                                           side_effect=lambda *a, **kw: send_calls.append(a) or True):
                            _mod.main()
        self.assertEqual(len(send_calls), 1)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_posts_slack_on_success(self):
        """main() must post Slack notification when email is sent."""
        _nova_cfg.post_both.reset_mock()
        with patch.object(_mod, "already_reached_out_today", return_value=False):
            with patch.object(_mod, "pick_recipient_and_angle", return_value={
                "recipient_email": "sam@example.com",
                "recipient_name": "Sam",
                "subject": "Hey",
                "angle": "test",
                "hook": "test",
            }):
                with patch.object(_mod, "generate_outreach_email", return_value="Test body"):
                    with patch.object(_mod, "maybe_attach_dream_image", return_value=None):
                        with patch.object(_mod, "send_email", return_value=True):
                            _mod.main()
        _nova_cfg.post_both.assert_called()

    def test_main_handles_missing_recipient(self):
        """main() must skip when decision has no recipient_email."""
        with patch.object(_mod, "already_reached_out_today", return_value=False):
            with patch.object(_mod, "pick_recipient_and_angle", return_value={
                "recipient_email": "",
                "recipient_name": "",
            }):
                try:
                    _mod.main()
                except Exception as e:
                    self.fail(f"main() raised on missing recipient: {e}")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_herd_outreach.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertIsInstance(_mod.SCRIPTS, Path)
        self.assertIsInstance(_mod.WORKSPACE, Path)
        self.assertIsInstance(_mod.OLLAMA_URL, str)
        self.assertIsInstance(_mod.TODAY, str)

    def test_all_functions_callable(self):
        for fn in [already_reached_out_today, read_file, send_email,
                    maybe_attach_dream_image, _mod.main]:
            self.assertTrue(callable(fn))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
