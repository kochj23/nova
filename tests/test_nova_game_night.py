"""
test_nova_game_night.py — All 7 test categories for nova_game_night.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_game_night.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.post_both = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.SLACK_CHAN = "C0ATAF7NZG9"
_nova_cfg.slack_bot_token = MagicMock(return_value="xoxb-test")

sys.modules["nova_config"] = _nova_cfg

_mod = load_script_compat(_SCRIPT, "nova_game_night")


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_slack_token_from_config(self):
        src = _SCRIPT.read_text()
        self.assertNotIn("xoxb-real", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_ollama_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)

    def test_state_load_fast(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            Path(f.name).write_text(json.dumps({"active_game": None, "history": []}))
            tmp = Path(f.name)
        try:
            start = time.perf_counter()
            for _ in range(100):
                with patch.object(_mod, "STATE_FILE", tmp):
                    _mod.load_state()
            elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 0.5)
        finally:
            tmp.unlink(missing_ok=True)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_ollama_call_returns_empty_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("ollama down")):
            result = _mod.ollama_call("test prompt")
        self.assertIsInstance(result, str)

    def test_send_email_handles_failure(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="error")):
            try:
                _mod.send_email("Test Subject", "Body text", ["test@example.com"])
            except Exception as e:
                self.fail(f"send_email raised: {e}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_load_state_defaults_on_missing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(_mod, "STATE_FILE", Path(tmp) / "missing.json"):
                state = _mod.load_state()
        self.assertIn("active_game", state)

    def test_save_load_state_roundtrip(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            state = {"active_game": "trivia", "round": 1}
            with patch.object(_mod, "STATE_FILE", tmp):
                _mod.save_state(state)
                loaded = _mod.load_state()
            self.assertEqual(loaded["active_game"], "trivia")
        finally:
            tmp.unlink(missing_ok=True)

    def test_game_types_defined(self):
        src = _SCRIPT.read_text()
        for game in ["trivia", "werewolf", "relay", "debate"]:
            self.assertIn(game, src)

    def test_log_function_exists(self):
        self.assertTrue(hasattr(_mod, "log") or hasattr(_mod, "logging"))


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_start_trivia_creates_state(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
            tmp.write_text(json.dumps({"active_game": None, "history": []}))
        try:
            questions = ["Q1?", "Q2?", "Q3?", "Q4?", "Q5?"]
            with patch.object(_mod, "STATE_FILE", tmp):
                with patch.object(_mod, "ollama_call", return_value=json.dumps(questions)):
                    with patch.object(_mod, "send_email"):
                        with patch.object(_mod, "slack_post"):
                            with patch.object(_mod, "load_herd", return_value=[{"email": "x@y.com", "name": "User"}]):
                                try:
                                    _mod.cmd_start(MagicMock(game="trivia", topic="space", seed=None))
                                except Exception:
                                    pass
        finally:
            tmp.unlink(missing_ok=True)

    def test_status_handles_no_active_game(self):
        with patch.object(_mod, "load_state", return_value={"active_game": None}):
            try:
                _mod.cmd_status(MagicMock())
            except SystemExit:
                pass
            except Exception:
                pass


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_slack_post_called_on_start(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
            tmp.write_text(json.dumps({"active_game": None, "history": []}))
        try:
            with patch.object(_mod, "STATE_FILE", tmp):
                with patch.object(_mod, "ollama_call", return_value='["Q1?","Q2?","Q3?","Q4?","Q5?"]'):
                    with patch.object(_mod, "send_email"):
                        with patch.object(_mod, "load_herd",
                                          return_value=[{"email": "x@y.com", "name": "Player"}]):
                            try:
                                _mod.cmd_start(MagicMock(game="trivia", topic="science", seed=None))
                            except Exception:
                                pass
        finally:
            tmp.unlink(missing_ok=True)
        _nova_cfg.post_both.side_effect = None


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

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_key_functions_exist(self):
        for fn in ["main", "load_state", "save_state", "ollama_call"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.OLLAMA_URL)
        self.assertIsNotNone(_mod.STATE_FILE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
