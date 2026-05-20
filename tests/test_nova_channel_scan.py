"""
test_nova_channel_scan.py — All 7 test categories for nova_channel_scan.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_channel_scan.py"
_spec = importlib.util.spec_from_file_location("nova_channel_scan", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_prefs = _mod.load_prefs
save_prefs = _mod.save_prefs
sort_key = _mod.sort_key
get_lineup = _mod.get_lineup
test_channel = _mod.test_channel


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(pat, src, f"Credential: {pat!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_hdhr_ip_is_local(self):
        """HDHomeRun IP must be local network — never a cloud endpoint."""
        self.assertTrue(_mod.HDHR_LINEUP.startswith("http://192.168."),
                        "HDHR must be on local LAN")

    def test_work_dir_not_on_main_ssd(self):
        """WORK_DIR should be on /Volumes — not the main SSD."""
        self.assertTrue(str(_mod.WORK_DIR).startswith("/Volumes"),
                        "WORK_DIR should be on /Volumes per install policy")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_sort_key_fast(self):
        import time
        channels = [f"{i}.{j}" for i in range(50) for j in range(10)]
        start = time.perf_counter()
        sorted(channels, key=sort_key)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"sort_key on 500 channels took {elapsed:.3f}s")

    def test_record_secs_bounded(self):
        """RECORD_SECS should be short enough for a full scan to complete in reasonable time."""
        self.assertLessEqual(_mod.RECORD_SECS, 60, "RECORD_SECS too long")
        self.assertGreater(_mod.RECORD_SECS, 0)

    def test_min_bytes_reasonable(self):
        """MIN_BYTES threshold must be positive and not too high."""
        self.assertGreater(_mod.MIN_BYTES, 0)
        self.assertLess(_mod.MIN_BYTES, 10_000_000, "MIN_BYTES too large")

    def test_pause_secs_bounded(self):
        """PAUSE_SECS between channels should be short."""
        self.assertLessEqual(_mod.PAUSE_SECS, 10)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_lineup_returns_empty_on_connection_error(self):
        """get_lineup must return [] if HDHomeRun is unreachable."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = get_lineup()
        self.assertEqual(result, [])

    def test_test_channel_returns_false_on_timeout(self):
        """test_channel must return (False, 0) on ffmpeg timeout."""
        with patch("subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("ffmpeg", 30)):
            with patch.object(_mod.WORK_DIR, "mkdir"):
                ok, size = test_channel("7.1")
        self.assertFalse(ok)
        self.assertEqual(size, 0)

    def test_test_channel_returns_false_on_exception(self):
        with patch("subprocess.run", side_effect=Exception("ffmpeg not found")):
            with patch.object(_mod.WORK_DIR, "mkdir"):
                ok, size = test_channel("7.1")
        self.assertFalse(ok)

    def test_load_prefs_returns_defaults_on_missing_file(self):
        with patch.object(_mod.PREFS_FILE, "exists", return_value=False):
            prefs = load_prefs()
        self.assertIn("viewed", prefs)
        self.assertIn("favorites", prefs)
        self.assertIn("bad_channels", prefs)

    def test_load_prefs_returns_defaults_on_corrupt_json(self):
        with patch.object(_mod.PREFS_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value="{INVALID"):
                prefs = load_prefs()
        self.assertIn("viewed", prefs)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_sort_key_basic(self):
        self.assertEqual(sort_key("7.1"), (7, 1))
        self.assertEqual(sort_key("11.2"), (11, 2))

    def test_sort_key_no_sub(self):
        self.assertEqual(sort_key("5"), (5, 0))

    def test_sort_key_invalid(self):
        # Non-numeric falls back to (999, 0)
        self.assertEqual(sort_key("abc"), (999, 0))

    def test_load_save_prefs_roundtrip(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            with patch.object(_mod, "PREFS_FILE", tmp):
                prefs = {"viewed": ["7.1"], "favorites": [], "history_count": 5, "bad_channels": {}}
                save_prefs(prefs)
                loaded = load_prefs()
            self.assertEqual(loaded["history_count"], 5)
            self.assertIn("7.1", loaded["viewed"])
        finally:
            tmp.unlink(missing_ok=True)

    def test_test_channel_success_needs_enough_bytes(self):
        """test_channel returns False if output file is too small."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with patch.object(_mod.WORK_DIR, "mkdir"):
                with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
                    f.write(b"\x00" * 100)  # too small
                    tmp_path = Path(f.name)
                # The function builds its own outfile path, so patch outfile.stat
                with patch("pathlib.Path.exists", return_value=True):
                    with patch("pathlib.Path.stat") as mock_stat:
                        mock_stat.return_value = MagicMock(st_size=100)
                        with patch("pathlib.Path.unlink"):
                            ok, size = test_channel("7.1")
        self.assertFalse(ok, "Should fail when file is too small")

    def test_sort_order(self):
        channels = ["11.2", "2.1", "7.3", "4.1"]
        sorted_ch = sorted(channels, key=sort_key)
        self.assertEqual(sorted_ch[0], "2.1")
        self.assertEqual(sorted_ch[-1], "11.2")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_exits_on_empty_lineup(self):
        """main() must exit(1) if HDHomeRun returns no channels."""
        with patch.object(_mod, "get_lineup", return_value=[]):
            with self.assertRaises(SystemExit) as ctx:
                _mod.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_main_saves_prefs_after_scan(self):
        """After scanning, main() should save updated prefs."""
        lineup = [{"GuideNumber": "7.1", "GuideName": "ABC"}]
        saved = []

        def fake_save(prefs):
            saved.append(prefs)

        def fake_test(ch_num):
            return True, 100_000

        with patch.object(_mod, "get_lineup", return_value=lineup):
            with patch.object(_mod, "test_channel", side_effect=fake_test):
                with patch.object(_mod, "save_prefs", side_effect=fake_save):
                    with patch.object(_mod, "load_prefs", return_value={"viewed": [], "favorites": [], "history_count": 0, "bad_channels": {}}):
                        with patch.object(_mod, "slack"):
                            with patch("time.sleep"):
                                _mod.main()

        self.assertGreater(len(saved), 0, "save_prefs should be called at least once")

    def test_main_marks_bad_channels(self):
        """Channels that fail the signal test should be in bad_channels."""
        lineup = [{"GuideNumber": "99.1", "GuideName": "NoSignal"}]
        saved = []

        def fake_save(prefs):
            saved.append(prefs)

        def fake_test(ch_num):
            return False, 0

        with patch.object(_mod, "get_lineup", return_value=lineup):
            with patch.object(_mod, "test_channel", side_effect=fake_test):
                with patch.object(_mod, "save_prefs", side_effect=fake_save):
                    with patch.object(_mod, "load_prefs", return_value={"viewed": [], "favorites": [], "history_count": 0, "bad_channels": {}}):
                        with patch.object(_mod, "slack"):
                            with patch("time.sleep"):
                                _mod.main()

        final_prefs = saved[-1]
        self.assertIn("99.1", final_prefs.get("bad_channels", {}),
                      "Failed channel should be in bad_channels")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_good_channels_in_whitelist(self):
        """Passing channels must be saved to whitelist."""
        lineup = [{"GuideNumber": "7.1", "GuideName": "ABC"}]
        saved = []

        def fake_save(prefs):
            saved.append(prefs)

        with patch.object(_mod, "get_lineup", return_value=lineup):
            with patch.object(_mod, "test_channel", return_value=(True, 200_000)):
                with patch.object(_mod, "save_prefs", side_effect=fake_save):
                    with patch.object(_mod, "load_prefs",
                                      return_value={"viewed": [], "favorites": [], "history_count": 0, "bad_channels": {}}):
                        with patch.object(_mod, "slack"):
                            with patch("time.sleep"):
                                _mod.main()

        final = saved[-1]
        wl = [c["ch"] for c in final.get("whitelist", [])]
        self.assertIn("7.1", wl)

    def test_slack_message_split_on_long_list(self):
        """If message exceeds 3000 chars, a summary message must be sent instead."""
        # Build a large lineup
        lineup = [{"GuideNumber": f"{i}.1", "GuideName": f"Channel{i}"}
                  for i in range(50)]
        slack_calls = []

        def fake_test(ch):
            return True, 200_000

        with patch.object(_mod, "get_lineup", return_value=lineup):
            with patch.object(_mod, "test_channel", side_effect=fake_test):
                with patch.object(_mod, "save_prefs"):
                    with patch.object(_mod, "load_prefs",
                                      return_value={"viewed": [], "favorites": [], "history_count": 0, "bad_channels": {}}):
                        with patch.object(_mod, "slack", side_effect=lambda m: slack_calls.append(m)):
                            with patch("time.sleep"):
                                _mod.main()

        self.assertGreater(len(slack_calls), 0)
        # Last slack call should be the result — check it doesn't exceed 3000 chars
        for call in slack_calls:
            self.assertLessEqual(len(call), 3001,
                                 "Single Slack message exceeds 3000 char limit")


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

    def test_script_is_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.HDHR_LINEUP)
        self.assertIsNotNone(_mod.FFMPEG)
        self.assertIsNotNone(_mod.RECORD_SECS)
        self.assertIsNotNone(_mod.MIN_BYTES)
        self.assertIsNotNone(_mod.PREFS_FILE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
