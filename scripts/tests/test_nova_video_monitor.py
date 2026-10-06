#!/usr/bin/env python3
"""Tests for nova_video_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_video_monitor.py"
SRC = SCRIPT.read_text()


def _stub_config():
    cfg = types.ModuleType("nova_config")
    cfg.SLACK_BB = "C_TEST_BB"
    cfg.post_both = MagicMock()
    return cfg


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_config": _stub_config()}):   # Slack stubbed at import; restored after
        spec.loader.exec_module(mod)
    return mod


vm = _load("vm_mod", SCRIPT)
_TMP = tempfile.TemporaryDirectory()
vm.LOG_FILE = Path(_TMP.name) / "batch.log"          # never read /Volumes/Data
vm.INTERVAL = 0                                       # no real sleeping in tests


def _pgrep(rc):
    return types.SimpleNamespace(returncode=rc, stdout="")


LOG = ("Found 3 video(s)\n"
       "Processing: ep1.mp4\n"
       "Processed video ep1.mp4 -> 1200 char transcript\n"
       "Processing: ep2.mp4\n"
       "Error: whisper crashed\n"
       "Processed video ep2.mp4 -> 800 char transcript\n"
       "Processing: " + "x" * 100 + "\n")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_pgrep_is_argv_list_no_shell_and_slack_goes_through_nova_config(self):
        self.assertIn('["pgrep", "-f", "nova_video_ingest.*yt"]', SRC)
        self.assertNotIn("shell=True", SRC)
        self.assertNotIn("slack.com", SRC)
        self.assertIn("nova_config.post_both", SRC)

    def test_log_content_is_data_not_code(self):
        vm.LOG_FILE.write_text("Processed video $(rm -rf /) -> abc char transcript\n")
        with patch.object(vm.subprocess, "run", return_value=_pgrep(1)):
            p = vm.get_progress()
        self.assertEqual((p["processed"], p["total_chars"]), (1, 0))     # bad int swallowed, nothing executed


class TestPerformance(unittest.TestCase):
    def test_10k_line_log_parses_under_bound(self):
        vm.LOG_FILE.write_text("".join(f"Processed video v{i}.mp4 -> {i} char transcript\n" for i in range(10_000)))
        with patch.object(vm.subprocess, "run", return_value=_pgrep(0)):
            t0 = time.perf_counter()
            p = vm.get_progress()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(p["processed"], 10_000)
        self.assertEqual(p["total_chars"], sum(range(10_000)))


class TestRetry(unittest.TestCase):
    def test_slack_failure_escapes_loop(self):
        # RETRY GAP: slack_post()/nova_config.post_both — one attempt per tick, no try/except: a Slack
        # outage ends the monitor (nohup exits), it does not retry.
        vm.LOG_FILE.write_text(LOG)
        vm.nova_config.post_both = MagicMock(side_effect=RuntimeError("slack 500"))
        with patch.object(vm.subprocess, "run", return_value=_pgrep(1)), patch.object(vm.time, "sleep"), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                vm.main()
        self.assertEqual(vm.nova_config.post_both.call_count, 1)
        vm.nova_config.post_both = MagicMock()

    def test_missing_log_file_is_skipped_not_fatal(self):
        if vm.LOG_FILE.exists():
            vm.LOG_FILE.unlink()
        with patch.object(vm.subprocess, "run") as sp:
            self.assertIsNone(vm.get_progress())
        sp.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_get_progress_counts(self):
        vm.LOG_FILE.write_text(LOG)
        with patch.object(vm.subprocess, "run", return_value=_pgrep(0)) as sp:
            p = vm.get_progress()
        self.assertEqual(sp.call_args[0][0], ["pgrep", "-f", "nova_video_ingest.*yt"])
        self.assertEqual(p["processed"], 2)
        self.assertEqual(p["total_chars"], 2000)
        self.assertEqual(p["errors"], 1)
        self.assertTrue(p["running"])
        self.assertEqual(len(p["last_file"]), 60)

    def test_empty_log(self):
        vm.LOG_FILE.write_text("")
        with patch.object(vm.subprocess, "run", return_value=_pgrep(1)):
            p = vm.get_progress()
        self.assertEqual(p, {"processed": 0, "total_chars": 0, "last_file": "", "errors": 0, "running": False})

    def test_slack_post_targets_bb_channel(self):
        vm.nova_config.post_both = MagicMock()
        vm.slack_post("hi")
        vm.nova_config.post_both.assert_called_once_with("hi", slack_channel="C_TEST_BB")


class TestIntegration(unittest.TestCase):
    def test_uses_shared_nova_config_channel_constant(self):
        self.assertIn("import nova_config", SRC)
        self.assertIn("nova_config.SLACK_BB", SRC)
        real = (SCRIPTS / "nova_config.py").read_text()
        self.assertRegex(real, r"(?m)^SLACK_BB\s*=")

    def test_progress_feeds_the_message_shape(self):
        vm.LOG_FILE.write_text(LOG)
        vm.nova_config.post_both = MagicMock()
        with patch.object(vm.subprocess, "run", return_value=_pgrep(1)), patch.object(vm.time, "sleep"), \
             redirect_stdout(io.StringIO()):
            vm.main()
        first = vm.nova_config.post_both.call_args_list[0][0][0]
        self.assertIn("Processed: *2* videos", first)
        self.assertIn("Transcript data: 2,000 characters", first)
        self.assertIn("Errors: 1", first)
        self.assertIn("Status: FINISHED", first)


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_progress_then_completion_and_exits(self):
        vm.LOG_FILE.write_text(LOG)
        vm.nova_config.post_both = MagicMock()
        with patch.object(vm.subprocess, "run", return_value=_pgrep(1)), patch.object(vm.time, "sleep") as sl, \
             redirect_stdout(io.StringIO()) as out:
            vm.main()
        self.assertEqual(vm.nova_config.post_both.call_count, 2)
        self.assertIn("Video Batch Transcription Complete", vm.nova_config.post_both.call_args_list[1][0][0])
        self.assertIn("Batch finished", out.getvalue())
        sl.assert_called_once_with(0)

    def test_keeps_polling_while_running_then_stops(self):
        vm.LOG_FILE.write_text(LOG)
        vm.nova_config.post_both = MagicMock()
        with patch.object(vm.subprocess, "run", side_effect=[_pgrep(0), _pgrep(0), _pgrep(1)]), \
             patch.object(vm.time, "sleep"), redirect_stdout(io.StringIO()):
            vm.main()
        self.assertEqual(vm.nova_config.post_both.call_count, 4)     # 3 progress + 1 completion


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        snippet = ("import sys, types; from unittest.mock import MagicMock\n"
                   "cfg = types.ModuleType('nova_config'); cfg.SLACK_BB='C'; cfg.post_both=MagicMock()\n"
                   "sys.modules['nova_config'] = cfg\n"
                   "import nova_video_monitor as m; assert m.INTERVAL == 600")
        r = subprocess.run([sys.executable, "-c", snippet], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
