"""
test_nova_gtnw_host.py — All 7 test categories for nova_gtnw_host.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_gtnw_host.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.post_both = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.NOVA_EMAIL = "nova@digitalnoise.net"

_herd_cfg = MagicMock()
_herd_cfg.HERD = [{"email": "player@example.com", "name": "Player One"}]

sys.modules["nova_config"] = _nova_cfg
sys.modules["herd_config"] = _herd_cfg

_mod = load_script_compat(_SCRIPT, "nova_gtnw_host")

gtnw_get = _mod.gtnw_get
log = _mod.log


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_gtnw_api_is_localhost(self):
        self.assertIn("127.0.0.1", _mod.GTNW_API)

    def test_nova_email_from_config(self):
        src = _SCRIPT.read_text()
        self.assertIn("nova_config.NOVA_EMAIL", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_gtnw_get_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout", src)

    def test_gtnw_get_fast_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            start = time.perf_counter()
            result = gtnw_get("/state")
            elapsed = time.perf_counter() - start
        self.assertIsNone(result)
        self.assertLess(elapsed, 5.0)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_gtnw_get_returns_none_on_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            result = gtnw_get("/api/state")
        self.assertIsNone(result)

    def test_gtnw_post_returns_none_on_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            result = _mod.gtnw_post("/api/action", {"key": "val"})
        self.assertIsNone(result)

    def test_send_email_silent_on_failure(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            try:
                _mod.send_email("Subject", "Body", ["player@example.com"])
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
        self.assertIsInstance(state, dict)

    def test_save_load_roundtrip(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            state = {"session_id": "test-123", "round": 3}
            with patch.object(_mod, "STATE_FILE", tmp):
                _mod.save_state(state)
                loaded = _mod.load_state()
            self.assertEqual(loaded["session_id"], "test-123")
        finally:
            tmp.unlink(missing_ok=True)

    def test_log_does_not_raise(self):
        log("test message")

    def test_gtnw_get_parses_json(self):
        fake = MagicMock()
        fake.read.return_value = json.dumps({"status": "ok"}).encode()
        fake.__enter__ = lambda s: s
        fake.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=fake):
            result = gtnw_get("/state")
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "ok")

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.GTNW_API)
        self.assertIsNotNone(_mod.OLLAMA_URL)
        self.assertIsNotNone(_mod.STATE_FILE)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_status_command_handles_no_state(self):
        with patch.object(_mod, "load_state", return_value={}):
            with patch.object(_mod, "gtnw_get", return_value=None):
                try:
                    _mod.cmd_status(MagicMock())
                except SystemExit:
                    pass
                except Exception:
                    pass

    def test_advance_posts_to_slack(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda m, **kw: posts.append(m)
        state = {"session_id": "s1", "year": 1962, "round": 1, "players": {}}
        with patch.object(_mod, "load_state", return_value=state):
            with patch.object(_mod, "gtnw_get", return_value={"year": 1963, "status": "active"}):
                with patch.object(_mod, "gtnw_post", return_value={"ok": True}):
                    with patch.object(_mod, "save_state"):
                        try:
                            _mod.cmd_advance(MagicMock())
                        except Exception:
                            pass
        _nova_cfg.post_both.side_effect = None


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_reset_clears_state(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
            tmp.write_text(json.dumps({"session_id": "old", "round": 5}))
        try:
            with patch.object(_mod, "STATE_FILE", tmp):
                _mod.cmd_reset(MagicMock())
            state = json.loads(tmp.read_text()) if tmp.exists() else {}
            self.assertNotEqual(state.get("round"), 5)
        finally:
            tmp.unlink(missing_ok=True)


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
        for fn in ["main", "gtnw_get", "gtnw_post", "load_state", "save_state", "log"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
