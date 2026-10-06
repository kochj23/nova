#!/usr/bin/env python3
"""Tests for nova_proactive_peace.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). osascript/curl/urlopen and the Slack bus are mocked; STATE_FILE and
HOLD_QUEUE are redirected to a tempdir so real state is never touched. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pp = _load("nova_proactive_peace_t", SCRIPTS / "nova_proactive_peace.py")
SRC = (SCRIPTS / "nova_proactive_peace.py").read_text()
pp.nova_config = types.SimpleNamespace(post_both=mock.MagicMock(), JORDAN_DM="D_TEST")


class _StateFiles:
    """Redirect STATE_FILE and HOLD_QUEUE into a tempdir for the duration of a test."""
    def __enter__(self):
        self.td = tempfile.mkdtemp()
        self.state = Path(self.td) / "state.json"
        self.queue = Path(self.td) / "queue.json"
        self._p = [mock.patch.object(pp, "STATE_FILE", self.state),
                   mock.patch.object(pp, "HOLD_QUEUE", self.queue)]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._p:
            p.stop()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_state_files_under_openclaw_workspace(self):
        self.assertIn('.openclaw/workspace/state/nova_peace_state.json', SRC)
        self.assertIn('.openclaw/workspace/state/nova_peace_hold_queue.json', SRC)


class TestPerformance(unittest.TestCase):
    def test_release_queue_large(self):
        with _StateFiles() as sf:
            sf.queue.write_text(json.dumps({"messages": [
                {"text": f"m{i}", "source": "s", "priority": "low"} for i in range(10_000)]}))
            pp.nova_config.post_both.reset_mock()
            t0 = time.perf_counter()
            pp.release_queue()
            self.assertLess(time.perf_counter() - t0, 1.0)
        pp.nova_config.post_both.assert_called_once()


class TestRetry(unittest.TestCase):
    def test_get_activity_level_tolerates_all_probes_down(self):
        # RETRY GAP: get_activity_level() probes are one-shot; every failure falls through to a
        # time-of-day default, never raising.
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")), \
             mock.patch.object(pp.subprocess, "run", side_effect=OSError("down")), \
             mock.patch.object(pp, "HOUR", 10):
            self.assertIn(pp.get_activity_level(), ("available", "focus_likely"))

    def test_get_focus_mode_failure_is_none(self):
        with mock.patch.object(pp.subprocess, "run", side_effect=RuntimeError("no osascript")):
            self.assertEqual(pp.get_focus_mode(), "none")


class TestUnit(unittest.TestCase):
    def test_should_alert_defaults_available(self):
        with _StateFiles():
            can, reason = pp.should_alert()
        self.assertTrue(can)
        self.assertEqual(reason, "available")

    def test_should_alert_blocks_in_unavailable_states(self):
        with _StateFiles() as sf:
            for st in ("sleeping", "dnd", "meeting"):
                sf.state.write_text(json.dumps({"jordan_state": st}))
                can, reason = pp.should_alert()
                self.assertFalse(can)
                self.assertEqual(reason, st)

    def test_should_alert_deep_focus(self):
        with _StateFiles() as sf:
            sf.state.write_text(json.dumps({"jordan_state": "coding", "focus_mode": "work"}))
            can, reason = pp.should_alert()
        self.assertFalse(can)
        self.assertEqual(reason, "deep_focus")

    def test_queue_message_appends(self):
        with _StateFiles() as sf:
            pp.queue_message("hello", "mysrc", "high")
            q = json.loads(sf.queue.read_text())
        self.assertEqual(q["messages"][0]["text"], "hello")
        self.assertEqual(q["messages"][0]["priority"], "high")


class TestIntegration(unittest.TestCase):
    def test_queue_then_release_digest(self):
        pp.nova_config.post_both.reset_mock()
        with _StateFiles() as sf:
            pp.queue_message("urgent thing", "a", "high")
            pp.queue_message("minor thing", "b", "low")
            pp.release_queue()
            self.assertEqual(json.loads(sf.queue.read_text())["messages"], [])
        digest = pp.nova_config.post_both.call_args[0][0]
        self.assertIn("urgent thing", digest)
        self.assertIn("2 while you were away", digest)

    def test_release_empty_queue_posts_nothing(self):
        pp.nova_config.post_both.reset_mock()
        with _StateFiles():
            pp.release_queue()
        pp.nova_config.post_both.assert_not_called()


class TestFunctional(unittest.TestCase):
    def test_main_releases_on_transition_to_available(self):
        pp.nova_config.post_both.reset_mock()
        with _StateFiles() as sf:
            sf.state.write_text(json.dumps({"jordan_state": "meeting"}))
            sf.queue.write_text(json.dumps({"messages": [{"text": "held", "source": "x", "priority": "low"}]}))
            with mock.patch.object(pp, "get_focus_mode", return_value="none"), \
                 mock.patch.object(pp, "get_screen_state", return_value="active"), \
                 mock.patch.object(pp, "get_activity_level", return_value="available"), \
                 mock.patch.object(pp, "detect_burnout_signals", return_value=[]), \
                 mock.patch.object(pp, "HOUR", 15), mock.patch.object(pp, "log"):
                pp.main()
            saved = json.loads(sf.state.read_text())
        self.assertEqual(saved["jordan_state"], "available")
        self.assertTrue(any("held" in str(c) for c in pp.nova_config.post_both.call_args_list))

    def test_main_deep_focus_holds_and_does_not_release(self):
        pp.nova_config.post_both.reset_mock()
        with _StateFiles() as sf:
            sf.state.write_text(json.dumps({"jordan_state": "available"}))
            with mock.patch.object(pp, "get_focus_mode", return_value="work"), \
                 mock.patch.object(pp, "get_screen_state", return_value="active"), \
                 mock.patch.object(pp, "get_activity_level", return_value="coding"), \
                 mock.patch.object(pp, "detect_burnout_signals", return_value=[]), \
                 mock.patch.object(pp, "HOUR", 10), mock.patch.object(pp, "log"):
                pp.main()
            saved = json.loads(sf.state.read_text())
        self.assertEqual(saved["jordan_state"], "deep_focus")

    def test_main_burnout_nudge_once_per_day(self):
        pp.nova_config.post_both.reset_mock()
        with _StateFiles() as sf:
            sf.state.write_text(json.dumps({"jordan_state": "available", "last_burnout_nudge": ""}))
            with mock.patch.object(pp, "get_focus_mode", return_value="none"), \
                 mock.patch.object(pp, "get_screen_state", return_value="active"), \
                 mock.patch.object(pp, "get_activity_level", return_value="available"), \
                 mock.patch.object(pp, "detect_burnout_signals", return_value=["still_coding_at_23"]), \
                 mock.patch.object(pp, "HOUR", 23), mock.patch.object(pp, "log"):
                pp.main()
            saved = json.loads(sf.state.read_text())
        self.assertEqual(saved["last_burnout_nudge"], pp.TODAY)
        self.assertTrue(any("11pm" in str(c) for c in pp.nova_config.post_both.call_args_list))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_proactive_peace.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--status", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
