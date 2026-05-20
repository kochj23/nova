"""
test_nova_proactive_peace.py — All 7 test categories for nova_proactive_peace.py
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
_nova_cfg.JORDAN_DM = "D0AMPB3F4T0"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_proactive_peace.py"
_spec = importlib.util.spec_from_file_location("nova_proactive_peace", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

should_alert = _mod.should_alert
queue_message = _mod.queue_message
release_queue = _mod.release_queue
load_queue = _mod.load_queue
save_queue = _mod.save_queue


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA"]
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

    def test_state_files_are_local(self):
        self.assertTrue(str(_mod.STATE_FILE).startswith(str(Path.home())))
        self.assertTrue(str(_mod.HOLD_QUEUE).startswith(str(Path.home())))

    def test_no_external_apis_called(self):
        """Peace script must not call external APIs for state checks."""
        src = _SCRIPT.read_text()
        # Should only use local ports, not openai.com or anthropic.com
        self.assertNotIn("openai.com", src)
        self.assertNotIn("anthropic.com", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_should_alert_fast(self):
        with patch.object(_mod, "STATE_FILE", Path("/nonexistent/state.json")):
            start = time.perf_counter()
            for _ in range(1000):
                should_alert()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2, f"should_alert 1000x: {elapsed:.3f}s")

    def test_load_queue_fast(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            qf = Path(tmpdir) / "queue.json"
            with patch.object(_mod, "HOLD_QUEUE", qf):
                start = time.perf_counter()
                for _ in range(200):
                    load_queue()
                elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.3)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_focus_mode_returns_none_on_applescript_failure(self):
        """get_focus_mode must return 'none' when AppleScript fails."""
        with patch("subprocess.run", side_effect=Exception("osascript failed")):
            result = _mod.get_focus_mode()
        self.assertEqual(result, "none")

    def test_get_screen_state_returns_active_on_failure(self):
        """get_screen_state must return 'active' when check fails."""
        with patch("subprocess.run", side_effect=Exception("permission denied")):
            result = _mod.get_screen_state()
        self.assertEqual(result, "active")

    def test_get_activity_level_returns_string_on_failure(self):
        """get_activity_level must return a valid string even on failure."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with patch("subprocess.run", side_effect=Exception("curl failed")):
                result = _mod.get_activity_level()
        self.assertIsInstance(result, str)
        self.assertIn(result, ["meeting", "coding", "sleeping", "winding_down",
                                "focus_likely", "available"])


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_should_alert_returns_true_when_no_state_file(self):
        """should_alert must return (True, 'available') when state file missing."""
        with patch.object(_mod, "STATE_FILE", Path("/nonexistent/state.json")):
            can_send, reason = should_alert()
        self.assertTrue(can_send)
        self.assertEqual(reason, "available")

    def test_should_alert_false_when_sleeping(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"jordan_state": "sleeping"}, f)
            tmp = f.name
        with patch.object(_mod, "STATE_FILE", Path(tmp)):
            can_send, reason = should_alert()
        os.unlink(tmp)
        self.assertFalse(can_send)

    def test_should_alert_false_when_dnd(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"jordan_state": "dnd"}, f)
            tmp = f.name
        with patch.object(_mod, "STATE_FILE", Path(tmp)):
            can_send, reason = should_alert()
        os.unlink(tmp)
        self.assertFalse(can_send)

    def test_should_alert_true_when_available(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"jordan_state": "available"}, f)
            tmp = f.name
        with patch.object(_mod, "STATE_FILE", Path(tmp)):
            can_send, reason = should_alert()
        os.unlink(tmp)
        self.assertTrue(can_send)

    def test_load_queue_empty_when_no_file(self):
        with patch.object(_mod, "HOLD_QUEUE", Path("/nonexistent/queue.json")):
            result = load_queue()
        self.assertEqual(result["messages"], [])

    def test_save_queue_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            qf = Path(tmpdir) / "hold_queue.json"
            with patch.object(_mod, "HOLD_QUEUE", qf):
                save_queue({"messages": [{"text": "test", "source": "test", "priority": "low"}]})
                loaded = load_queue()
        self.assertEqual(len(loaded["messages"]), 1)
        self.assertEqual(loaded["messages"][0]["text"], "test")

    def test_queue_message_adds_to_queue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            qf = Path(tmpdir) / "queue.json"
            with patch.object(_mod, "HOLD_QUEUE", qf):
                queue_message("test alert", "test_script", "high")
                queue = load_queue()
        self.assertEqual(len(queue["messages"]), 1)
        self.assertEqual(queue["messages"][0]["priority"], "high")
        self.assertEqual(queue["messages"][0]["source"], "test_script")

    def test_sleep_hours_range(self):
        self.assertIn(3, _mod.SLEEP_HOURS)
        self.assertNotIn(8, _mod.SLEEP_HOURS)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_release_queue_clears_messages(self):
        """release_queue must clear the queue after posting."""
        with tempfile.TemporaryDirectory() as tmpdir:
            qf = Path(tmpdir) / "queue.json"
            with patch.object(_mod, "HOLD_QUEUE", qf):
                save_queue({"messages": [
                    {"text": "Alert 1", "source": "s1", "priority": "high",
                     "queued_at": "2026-05-13T10:00:00"},
                ]})
                with patch.object(_mod, "slack_post"):
                    release_queue()
                queue = load_queue()
        self.assertEqual(len(queue["messages"]), 0, "Queue must be empty after release")

    def test_release_queue_posts_to_slack(self):
        slack_calls = []
        with tempfile.TemporaryDirectory() as tmpdir:
            qf = Path(tmpdir) / "queue.json"
            with patch.object(_mod, "HOLD_QUEUE", qf):
                save_queue({"messages": [
                    {"text": "Important alert", "source": "s1", "priority": "high",
                     "queued_at": "2026-05-13T10:00:00"},
                ]})
                with patch.object(_mod, "slack_post",
                                   side_effect=lambda t, **kw: slack_calls.append(t)):
                    release_queue()
        self.assertGreater(len(slack_calls), 0, "Should have posted to Slack")
        self.assertIn("Important alert", slack_calls[0])

    def test_main_saves_state_file(self):
        """main() must save a state file after running."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = Path(tmpdir) / "peace_state.json"
            qf = Path(tmpdir) / "queue.json"
            with patch.object(_mod, "STATE_FILE", sf):
                with patch.object(_mod, "HOLD_QUEUE", qf):
                    with patch.object(_mod, "get_focus_mode", return_value="none"):
                        with patch.object(_mod, "get_screen_state", return_value="active"):
                            with patch.object(_mod, "get_activity_level", return_value="available"):
                                with patch.object(_mod, "detect_burnout_signals", return_value=[]):
                                    _mod.main()
            self.assertTrue(sf.exists(), "State file must be written by main()")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_burnout_nudge_sent_at_late_hour(self):
        """Burnout nudge must be sent when coding at 23:00."""
        slack_calls = []
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = Path(tmpdir) / "state.json"
            qf = Path(tmpdir) / "queue.json"
            with patch.object(_mod, "STATE_FILE", sf):
                with patch.object(_mod, "HOLD_QUEUE", qf):
                    with patch.object(_mod, "HOUR", 23):
                        with patch.object(_mod, "get_focus_mode", return_value="none"):
                            with patch.object(_mod, "get_screen_state", return_value="active"):
                                with patch.object(_mod, "get_activity_level", return_value="available"):
                                    with patch.object(_mod, "detect_burnout_signals",
                                                       return_value=["still_coding_at_23"]):
                                        with patch.object(_mod, "slack_post",
                                                           side_effect=lambda t, **kw: slack_calls.append(t)):
                                            _mod.main()
        self.assertGreater(len(slack_calls), 0, "Burnout nudge must be sent")

    def test_release_queue_separates_high_and_low_priority(self):
        """release_queue must show Priority: section and Other: section."""
        with tempfile.TemporaryDirectory() as tmpdir:
            qf = Path(tmpdir) / "queue.json"
            slack_posts = []
            with patch.object(_mod, "HOLD_QUEUE", qf):
                save_queue({"messages": [
                    {"text": "HIGH: Critical system alert", "source": "monitor",
                     "priority": "high", "queued_at": "2026-05-13T10:00:00"},
                    {"text": "LOW: App suggestion", "source": "suggestions",
                     "priority": "low", "queued_at": "2026-05-13T10:01:00"},
                ]})
                with patch.object(_mod, "slack_post",
                                   side_effect=lambda t, **kw: slack_posts.append(t)):
                    release_queue()
        self.assertGreater(len(slack_posts), 0)
        msg = slack_posts[0]
        self.assertIn("Priority", msg)
        self.assertIn("Other", msg)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_proactive_peace.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertIsInstance(_mod.SLEEP_HOURS, range)
        self.assertIsInstance(_mod.FOCUS_HOURS, list)
        self.assertIsInstance(_mod.STATE_FILE, Path)
        self.assertIsInstance(_mod.HOLD_QUEUE, Path)

    def test_all_functions_callable(self):
        for fn in [should_alert, queue_message, release_queue,
                    load_queue, save_queue,
                    _mod.get_focus_mode, _mod.get_screen_state,
                    _mod.get_activity_level, _mod.detect_burnout_signals, _mod.main]:
            self.assertTrue(callable(fn))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
