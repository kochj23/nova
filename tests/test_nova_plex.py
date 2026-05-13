"""
test_nova_plex.py — All 7 test categories for nova_plex.py
Written by Jordan Koch.
"""
import importlib.util, json, sys, time, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

_nova_cfg = MagicMock()
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.SLACK_CHAN = "#nova-chat"
_nova_cfg.JORDAN_DM = "D0AMPB3F4T0"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_plex.py"
_spec = importlib.util.spec_from_file_location("nova_plex", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_skip_library = _mod._skip_library
format_duration = _mod.format_duration
load_json = _mod.load_json
save_json = _mod.save_json
ts_to_dt = _mod.ts_to_dt


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
    def test_plex_token_from_keychain(self):
        src = _SCRIPT.read_text()
        self.assertIn("security", src)
        self.assertIn("nova-plex-token", src)
    def test_plex_password_from_keychain(self):
        src = _SCRIPT.read_text()
        self.assertIn("nova-plex-password", src)
    def test_other_library_skipped(self):
        """Library 23 ('Other') must always be skipped."""
        self.assertTrue(_skip_library(23))
    def test_skip_libraries_set_defined(self):
        self.assertIn(23, _mod.SKIP_LIBRARIES)


class TestPerformance(unittest.TestCase):
    def test_format_duration_fast(self):
        start = time.perf_counter()
        for _ in range(10000):
            format_duration(7200)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)
    def test_format_duration_hours(self):
        self.assertEqual(format_duration(7200), "2h 0m")
    def test_format_duration_minutes(self):
        self.assertEqual(format_duration(2700), "45m")
    def test_load_json_fast_on_missing(self):
        start = time.perf_counter()
        result = load_json(Path("/nonexistent/file.json"), default={})
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.01)
        self.assertEqual(result, {})


class TestRetry(unittest.TestCase):
    def test_store_vector_does_not_raise(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            _mod.store_vector("test text", "plex", {})
    def test_plex_get_raises_on_network_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            with patch.object(_mod, "token", return_value="fake_token"):
                with self.assertRaises(Exception):
                    _mod.plex_get("/library/sections")


class TestUnit(unittest.TestCase):
    def test_skip_library_true_for_23(self):
        self.assertTrue(_skip_library(23))
    def test_skip_library_false_for_7(self):
        self.assertFalse(_skip_library(7))
    def test_skip_library_false_for_6(self):
        self.assertFalse(_skip_library(6))
    def test_skip_library_handles_non_int(self):
        self.assertFalse(_skip_library("not_a_number"))
    def test_ts_to_dt_returns_datetime(self):
        from datetime import datetime
        result = ts_to_dt(1700000000)
        self.assertIsInstance(result, datetime)
    def test_save_and_load_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.json"
            data = {"key": "value", "count": 42}
            save_json(path, data)
            loaded = load_json(path)
        self.assertEqual(loaded["key"], "value")
        self.assertEqual(loaded["count"], 42)
    def test_load_json_returns_default_when_missing(self):
        result = load_json(Path("/nonexistent/x.json"), default={"default": True})
        self.assertTrue(result["default"])
    def test_load_json_returns_empty_dict_by_default(self):
        result = load_json(Path("/nonexistent/y.json"))
        self.assertEqual(result, {})
    def test_library_names_has_major_libraries(self):
        self.assertIn(7, _mod.LIBRARY_NAMES)   # Movies
        self.assertIn(6, _mod.LIBRARY_NAMES)   # TV Shows
    def test_commands_dict_populated(self):
        self.assertIn("history", _mod.COMMANDS)
        self.assertIn("playing", _mod.COMMANDS)
        self.assertIn("stats", _mod.COMMANDS)


class TestIntegration(unittest.TestCase):
    def test_cmd_playing_handles_no_sessions(self):
        xml_str = '<?xml version="1.0"?><MediaContainer size="0"></MediaContainer>'
        mock_resp = MagicMock()
        mock_resp.read.return_value = xml_str.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(_mod, "token", return_value="fake_token"):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    _mod.cmd_playing(MagicMock())
        self.assertIn("Nothing playing", buf.getvalue())

    def test_cmd_history_handles_no_items(self):
        xml_str = '<?xml version="1.0"?><MediaContainer size="0"></MediaContainer>'
        mock_resp = MagicMock()
        mock_resp.read.return_value = xml_str.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(_mod, "token", return_value="fake_token"):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    _mod.cmd_history(MagicMock())


class TestFunctional(unittest.TestCase):
    def test_main_help_or_no_command(self):
        with patch("sys.argv", ["nova_plex.py", "--help"]):
            with self.assertRaises(SystemExit):
                _mod.main()

    def test_main_quiet_mode_suppresses_slack(self):
        xml_str = '<?xml version="1.0"?><MediaContainer size="0"></MediaContainer>'
        mock_resp = MagicMock()
        mock_resp.read.return_value = xml_str.encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("sys.argv", ["nova_plex.py", "playing", "--quiet"]):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                with patch.object(_mod, "token", return_value="fake_token"):
                    _mod.main()
        # post_both should NOT be called in quiet mode
        _nova_cfg.post_both.assert_not_called()

    def test_shame_roasts_are_defined(self):
        src = _SCRIPT.read_text()
        self.assertIn("loyal dog", src)


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.PLEX_URL, str)
        self.assertIsInstance(_mod.SKIP_LIBRARIES, set)
        self.assertIsInstance(_mod.LIBRARY_NAMES, dict)
        self.assertIsInstance(_mod.COMMANDS, dict)
    def test_functions_exist(self):
        for fn in ("_keychain_get", "_plex_token", "token", "plex_get", "plex_get_json",
                   "_skip_library", "format_duration", "load_json", "save_json",
                   "ts_to_dt", "get_all_libraries", "store_vector", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
