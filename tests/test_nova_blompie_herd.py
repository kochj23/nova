"""
test_nova_blompie_herd.py — All 7 test categories for nova_blompie_herd.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_EMAIL = "#nova-email"
_nova_cfg.NOVA_EMAIL = "nova@digitalnoise.net"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg
sys.modules["herd_config"] = MagicMock(HERD=[
    {"name": "Sam", "email": "sam@example.com"},
    {"name": "Gaston", "email": "gaston@example.com"},
])

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_blompie_herd.py"
_spec = importlib.util.spec_from_file_location("nova_blompie_herd", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_state = _mod.load_state
save_state = _mod.save_state
format_scene_email = _mod.format_scene_email
send_mail = _mod.send_mail
cmd_status = _mod.cmd_status


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

    def test_herd_config_imported_with_fallback(self):
        src = _SCRIPT.read_text()
        self.assertIn("ImportError", src)

    def test_state_file_is_local(self):
        self.assertTrue(str(_mod.STATE_FILE).startswith(str(Path.home())))

    def test_send_mail_uses_herd_mail_sh(self):
        """send_mail must delegate to nova_herd_mail.sh, not SMTP directly."""
        src = _SCRIPT.read_text()
        self.assertIn("nova_herd_mail.sh", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_load_save_state_fast(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "game.json"
            with patch.object(_mod, "STATE_FILE", state_file):
                start = time.perf_counter()
                for _ in range(100):
                    save_state({"session_id": "abc", "turn": 1})
                    load_state()
                elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"100x state load/save: {elapsed:.3f}s")

    def test_format_scene_email_fast(self):
        players = [{"name": "Nova", "email": "nova@digitalnoise.net", "style": "curious"},
                   {"name": "Sam", "email": "sam@example.com", "style": "warm"}]
        player = players[0]
        start = time.perf_counter()
        for _ in range(100):
            format_scene_email("You are in a forest.", player, 1, ["torch"], players)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_send_mail_returns_false_on_subprocess_error(self):
        with patch("subprocess.run", side_effect=Exception("process failed")):
            result = send_mail("to@example.com", "Subject", "Body")
        self.assertFalse(result)

    def test_blompie_action_raises_on_failure(self):
        """blompie_action error propagates (cmd_turn handles it)."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with self.assertRaises(Exception):
                _mod.blompie_action("session123", "look around")

    def test_nova_auto_play_returns_fallback_on_llm_failure(self):
        """nova_auto_play must return 'look around' when LLM fails."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = _mod.nova_auto_play("You are in a forest.", [], 1, [])
        self.assertEqual(result, "look around")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_load_state_returns_none_when_no_file(self):
        with patch.object(_mod, "STATE_FILE", Path("/nonexistent/game.json")):
            result = load_state()
        self.assertIsNone(result)

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "game.json"
            with patch.object(_mod, "STATE_FILE", state_file):
                data = {"session_id": "abc123", "turn": 5, "player_index": 1}
                save_state(data)
                loaded = load_state()
        self.assertEqual(loaded["session_id"], "abc123")
        self.assertEqual(loaded["turn"], 5)

    def test_format_scene_email_contains_player_name(self):
        players = [{"name": "Sam", "email": "sam@example.com", "style": "warm"},
                   {"name": "Nova", "email": "nova@digitalnoise.net", "style": "curious"}]
        result = format_scene_email("Forest scene.", players[0], 1, [], players)
        self.assertIn("Sam", result)

    def test_format_scene_email_contains_scene(self):
        players = [{"name": "Sam", "email": "sam@example.com", "style": "warm"}]
        result = format_scene_email("The dungeon is dark.", players[0], 1, ["key"], players)
        self.assertIn("The dungeon is dark", result)

    def test_format_scene_email_first_includes_intro(self):
        players = [{"name": "Sam", "email": "sam@example.com", "style": "warm"}]
        result = format_scene_email("Scene.", players[0], 1, [], players, is_first=True)
        self.assertIn("Blompie", result)

    def test_format_scene_email_shows_inventory(self):
        players = [{"name": "Sam", "email": "sam@example.com", "style": "warm"}]
        result = format_scene_email("Scene.", players[0], 1, ["sword", "key"], players)
        self.assertIn("sword", result)

    def test_cmd_status_no_game(self):
        """cmd_status must not raise when no active game."""
        with patch.object(_mod, "STATE_FILE", Path("/nonexistent/game.json")):
            try:
                cmd_status()
            except SystemExit:
                pass
            except Exception as e:
                self.fail(f"cmd_status raised: {e}")

    def test_players_list_not_empty(self):
        self.assertGreater(len(_mod.PLAYERS), 0)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_cmd_start_checks_blompie_running(self):
        """cmd_start must exit if Blompie API is not running."""
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            with self.assertRaises(SystemExit) as ctx:
                _mod.cmd_start()
        self.assertEqual(ctx.exception.code, 1)

    def test_cmd_nudge_no_game_exits(self):
        with patch.object(_mod, "STATE_FILE", Path("/nonexistent/game.json")):
            with self.assertRaises(SystemExit):
                _mod.cmd_nudge()

    def test_check_inbox_for_moves_no_game(self):
        """check_inbox_for_moves must return gracefully when no active game."""
        with patch.object(_mod, "STATE_FILE", Path("/nonexistent/game.json")):
            try:
                _mod.check_inbox_for_moves()
            except Exception as e:
                self.fail(f"check_inbox_for_moves raised: {e}")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_nova_auto_play_returns_string(self):
        response_json = json.dumps({"response": "examine the glowing door"}).encode()
        mock_r = MagicMock()
        mock_r.__enter__ = lambda s: s
        mock_r.__exit__ = MagicMock(return_value=False)
        mock_r.read.return_value = response_json
        with patch("urllib.request.urlopen", return_value=mock_r):
            result = _mod.nova_auto_play("You see a glowing door.", [], 1, ["open door", "look"])
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_nova_auto_play_strips_backticks(self):
        response_json = json.dumps({"response": "`look around`"}).encode()
        mock_r = MagicMock()
        mock_r.__enter__ = lambda s: s
        mock_r.__exit__ = MagicMock(return_value=False)
        mock_r.read.return_value = response_json
        with patch("urllib.request.urlopen", return_value=mock_r):
            result = _mod.nova_auto_play("Scene.", [], 1, [])
        self.assertNotIn("`", result)

    def test_send_mail_returns_true_on_success(self):
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="", stderr="")):
            result = send_mail("to@example.com", "Subject", "Body")
        self.assertTrue(result)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_blompie_herd.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertIsInstance(_mod.BLOMPIE_API, str)
        self.assertIsInstance(_mod.STATE_FILE, Path)
        self.assertIsInstance(_mod.PLAYERS, list)

    def test_blompie_api_local(self):
        """Blompie API must be local (127.0.0.1), not cloud."""
        self.assertIn("127.0.0.1", _mod.BLOMPIE_API)

    def test_all_functions_callable(self):
        for fn in [load_state, save_state, format_scene_email,
                    send_mail, cmd_status, _mod.cmd_nudge]:
            self.assertTrue(callable(fn))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
