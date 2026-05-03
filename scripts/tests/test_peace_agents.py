"""test_peace_agents.py — Tests for proactive peace and subagent framework. Written by Jordan Koch."""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open, PropertyMock, AsyncMock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Fixtures from conftest.py: mock_nova_config, mock_nova_logger, tmp_state_dir


# ============================================================================
# Helpers
# ============================================================================

def _make_subprocess_result(stdout="", stderr="", returncode=0):
    """Build a mock subprocess.CompletedProcess."""
    r = MagicMock()
    r.stdout = stdout
    r.stderr = stderr
    r.returncode = returncode
    return r


def _run_async(coro):
    """Run a coroutine synchronously for tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ============================================================================
# PROACTIVE PEACE — get_focus_mode
# ============================================================================


class TestGetFocusMode:
    """Focus mode detection via osascript subprocess calls."""

    @patch("nova_proactive_peace.subprocess.run")
    def test_dnd_detected(self, mock_run, mock_nova_config):
        """DND returns 'dnd' when plutil output contains 'true'."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        mock_run.side_effect = [
            _make_subprocess_result(stdout="0"),       # NSStatusItem
            _make_subprocess_result(stdout="true"),     # DND plutil
        ]
        assert npp.get_focus_mode() == "dnd"

    @patch("nova_proactive_peace.subprocess.run")
    def test_sleep_focus_mode(self, mock_run, mock_nova_config):
        """Sleep focus mode detected from assertion store directory listing."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        mock_run.side_effect = [
            _make_subprocess_result(stdout="0"),
            _make_subprocess_result(stdout="false"),         # not DND
            _make_subprocess_result(stdout="sleep.json\n"),  # assertion files
        ]
        assert npp.get_focus_mode() == "sleep"

    @patch("nova_proactive_peace.subprocess.run")
    def test_work_focus_mode(self, mock_run, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        mock_run.side_effect = [
            _make_subprocess_result(stdout="0"),
            _make_subprocess_result(stdout="false"),
            _make_subprocess_result(stdout="work.plist\n"),
        ]
        assert npp.get_focus_mode() == "work"

    @patch("nova_proactive_peace.subprocess.run")
    def test_personal_focus_mode(self, mock_run, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        mock_run.side_effect = [
            _make_subprocess_result(stdout="0"),
            _make_subprocess_result(stdout="false"),
            _make_subprocess_result(stdout="personal.json\n"),
        ]
        assert npp.get_focus_mode() == "personal"

    @patch("nova_proactive_peace.subprocess.run")
    def test_no_focus_mode(self, mock_run, mock_nova_config):
        """No focus mode active returns 'none'."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        mock_run.side_effect = [
            _make_subprocess_result(stdout="0"),
            _make_subprocess_result(stdout="false"),
            _make_subprocess_result(stdout=""),
        ]
        assert npp.get_focus_mode() == "none"

    @patch("nova_proactive_peace.subprocess.run")
    def test_osascript_timeout_returns_none(self, mock_run, mock_nova_config):
        """If all osascript calls time out, fall back to 'none'."""
        import importlib, subprocess
        import nova_proactive_peace as npp
        importlib.reload(npp)

        mock_run.side_effect = subprocess.TimeoutExpired("osascript", 5)
        assert npp.get_focus_mode() == "none"

    @patch("nova_proactive_peace.subprocess.run")
    def test_osascript_exception_returns_none(self, mock_run, mock_nova_config):
        """Generic exceptions fall back gracefully."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        mock_run.side_effect = OSError("no such command")
        assert npp.get_focus_mode() == "none"


# ============================================================================
# PROACTIVE PEACE — get_screen_state
# ============================================================================


class TestGetScreenState:

    @patch("nova_proactive_peace.subprocess.run")
    def test_screen_locked(self, mock_run, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        mock_run.return_value = _make_subprocess_result(stdout="true")
        assert npp.get_screen_state() == "locked"

    @patch("nova_proactive_peace.subprocess.run")
    def test_screen_active(self, mock_run, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        mock_run.return_value = _make_subprocess_result(stdout="false")
        assert npp.get_screen_state() == "active"

    @patch("nova_proactive_peace.subprocess.run")
    def test_screen_state_exception_returns_active(self, mock_run, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        mock_run.side_effect = Exception("System Events unavailable")
        assert npp.get_screen_state() == "active"


# ============================================================================
# PROACTIVE PEACE — get_activity_level
# ============================================================================


class TestGetActivityLevel:

    @patch("nova_proactive_peace.subprocess.run")
    @patch("nova_proactive_peace.urllib.request.urlopen")
    def test_coding_detected_via_mlxcode_port(self, mock_urlopen, mock_run, mock_nova_config):
        """MLXCode port 37422 responding -> 'coding'."""
        import importlib
        import nova_proactive_peace as npp
        # Override HOUR to a non-sleep, non-focus, non-wind-down hour
        npp.HOUR = 20
        importlib.reload(npp)
        npp.HOUR = 20

        mock_urlopen.return_value = MagicMock()  # port responds
        mock_run.return_value = _make_subprocess_result(stdout="", returncode=1)  # no meeting

        assert npp.get_activity_level() == "coding"

    @patch("nova_proactive_peace.subprocess.run")
    @patch("nova_proactive_peace.urllib.request.urlopen")
    def test_meeting_detected(self, mock_urlopen, mock_run, mock_nova_config):
        """OneOnOne API returning a today-dated meeting -> 'meeting'."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        npp.HOUR = 14
        npp.TODAY = date.today().isoformat()

        mock_urlopen.side_effect = Exception("port closed")  # no MLXCode
        mock_run.return_value = _make_subprocess_result(
            stdout=json.dumps([{"date": npp.TODAY + "T14:00:00"}]),
            returncode=0,
        )

        assert npp.get_activity_level() == "meeting"

    @patch("nova_proactive_peace.subprocess.run")
    @patch("nova_proactive_peace.urllib.request.urlopen")
    def test_sleeping_hours(self, mock_urlopen, mock_run, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        npp.HOUR = 3

        mock_urlopen.side_effect = Exception("port closed")
        mock_run.return_value = _make_subprocess_result(returncode=1)

        assert npp.get_activity_level() == "sleeping"

    @patch("nova_proactive_peace.subprocess.run")
    @patch("nova_proactive_peace.urllib.request.urlopen")
    def test_winding_down_hours(self, mock_urlopen, mock_run, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        npp.HOUR = 22

        mock_urlopen.side_effect = Exception("port closed")
        mock_run.return_value = _make_subprocess_result(returncode=1)

        assert npp.get_activity_level() == "winding_down"

    @patch("nova_proactive_peace.subprocess.run")
    @patch("nova_proactive_peace.urllib.request.urlopen")
    def test_focus_likely_during_focus_hours(self, mock_urlopen, mock_run, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        npp.HOUR = 10

        mock_urlopen.side_effect = Exception("port closed")
        mock_run.return_value = _make_subprocess_result(returncode=1)

        assert npp.get_activity_level() == "focus_likely"

    @patch("nova_proactive_peace.subprocess.run")
    @patch("nova_proactive_peace.urllib.request.urlopen")
    def test_available_outside_special_hours(self, mock_urlopen, mock_run, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        npp.HOUR = 19  # 7pm — not sleep, not focus, not wind-down

        mock_urlopen.side_effect = Exception("port closed")
        mock_run.return_value = _make_subprocess_result(returncode=1)

        assert npp.get_activity_level() == "available"

    @patch("nova_proactive_peace.subprocess.run")
    @patch("nova_proactive_peace.urllib.request.urlopen")
    def test_meeting_with_dict_response_format(self, mock_urlopen, mock_run, mock_nova_config):
        """OneOnOne sometimes returns {"meetings": [...]} instead of a bare list."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        npp.HOUR = 14
        npp.TODAY = date.today().isoformat()

        mock_urlopen.side_effect = Exception("port closed")
        mock_run.return_value = _make_subprocess_result(
            stdout=json.dumps({"meetings": [{"date": npp.TODAY + "T14:00:00"}]}),
            returncode=0,
        )

        assert npp.get_activity_level() == "meeting"


# ============================================================================
# PROACTIVE PEACE — detect_burnout_signals
# ============================================================================


class TestDetectBurnoutSignals:

    @patch("nova_proactive_peace.subprocess.run")
    @patch("nova_proactive_peace.urllib.request.urlopen")
    def test_late_night_coding_detected(self, mock_urlopen, mock_run, mock_nova_config):
        """MLXCode responding at 23:xx -> 'still_coding_at_23'."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        npp.HOUR = 23
        npp.NOW = datetime(2026, 5, 1, 23, 30)  # Thursday

        mock_urlopen.return_value = MagicMock()  # MLXCode running

        signals = npp.detect_burnout_signals()
        assert any("still_coding_at_23" in s for s in signals)

    @patch("nova_proactive_peace.subprocess.run")
    @patch("nova_proactive_peace.urllib.request.urlopen")
    def test_no_burnout_during_normal_hours(self, mock_urlopen, mock_run, mock_nova_config):
        """No signals when it is a normal weekday afternoon."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        npp.HOUR = 14
        npp.NOW = datetime(2026, 5, 1, 14, 0)  # Thursday 2pm

        signals = npp.detect_burnout_signals()
        assert signals == []

    @patch("nova_proactive_peace.subprocess.run")
    @patch("nova_proactive_peace.urllib.request.urlopen")
    def test_weekend_commits_detected(self, mock_urlopen, mock_run, mock_nova_config):
        """Git log finding recent commits on Saturday during focus hours -> 'weekend_commits'."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        # Saturday at 10am
        npp.NOW = datetime(2026, 5, 3, 10, 0)  # Saturday
        npp.HOUR = 10

        mock_urlopen.side_effect = Exception("no apps")

        # Create a mock repo directory structure
        mock_repo = MagicMock()
        mock_repo.iterdir.return_value = [Path("/Volumes/Data/xcode/MLXCode")]

        with patch("nova_proactive_peace.Path") as mock_path_cls:
            # We need to handle multiple Path() calls
            def path_side_effect(arg):
                if arg == "/Volumes/Data/xcode":
                    p = MagicMock()
                    mock_subdir = MagicMock()
                    mock_subdir.__truediv__ = lambda self, other: MagicMock(exists=MagicMock(return_value=True))
                    p.iterdir.return_value = [mock_subdir]
                    return p
                return MagicMock()

            mock_path_cls.side_effect = path_side_effect

            # git log returns a commit
            mock_run.return_value = _make_subprocess_result(stdout="abc1234 fix something")

            signals = npp.detect_burnout_signals()
            assert "weekend_commits" in signals

    @patch("nova_proactive_peace.subprocess.run")
    @patch("nova_proactive_peace.urllib.request.urlopen")
    def test_weekend_no_commits_no_signal(self, mock_urlopen, mock_run, mock_nova_config):
        """Weekend but no recent commits -> no burnout signal."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        npp.NOW = datetime(2026, 5, 3, 10, 0)  # Saturday
        npp.HOUR = 10

        mock_urlopen.side_effect = Exception("no apps")

        with patch("nova_proactive_peace.Path") as mock_path_cls:
            def path_side_effect(arg):
                if arg == "/Volumes/Data/xcode":
                    p = MagicMock()
                    mock_subdir = MagicMock()
                    mock_subdir.__truediv__ = lambda self, other: MagicMock(exists=MagicMock(return_value=True))
                    p.iterdir.return_value = [mock_subdir]
                    return p
                return MagicMock()

            mock_path_cls.side_effect = path_side_effect
            mock_run.return_value = _make_subprocess_result(stdout="")  # no commits

            signals = npp.detect_burnout_signals()
            assert "weekend_commits" not in signals

    @patch("nova_proactive_peace.subprocess.run")
    @patch("nova_proactive_peace.urllib.request.urlopen")
    def test_late_night_no_apps_no_signal(self, mock_urlopen, mock_run, mock_nova_config):
        """Late night but no coding apps running -> no burnout signal."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        npp.HOUR = 23
        npp.NOW = datetime(2026, 5, 1, 23, 30)

        mock_urlopen.side_effect = Exception("port closed")

        signals = npp.detect_burnout_signals()
        assert signals == []


# ============================================================================
# PROACTIVE PEACE — Hold queue management
# ============================================================================


class TestHoldQueue:

    def test_load_queue_empty_when_no_file(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        npp.HOLD_QUEUE = tmp_state_dir / "nonexistent.json"

        result = npp.load_queue()
        assert result == {"messages": []}

    def test_load_queue_existing_file(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        queue_file = tmp_state_dir / "hold_queue.json"
        queue_data = {"messages": [{"text": "test msg", "source": "unit_test"}]}
        queue_file.write_text(json.dumps(queue_data))
        npp.HOLD_QUEUE = queue_file

        result = npp.load_queue()
        assert len(result["messages"]) == 1
        assert result["messages"][0]["text"] == "test msg"

    def test_load_queue_corrupted_file(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        queue_file = tmp_state_dir / "hold_queue.json"
        queue_file.write_text("not valid json {{{")
        npp.HOLD_QUEUE = queue_file

        result = npp.load_queue()
        assert result == {"messages": []}

    def test_save_queue_writes_valid_json(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        queue_file = tmp_state_dir / "hold_queue.json"
        npp.HOLD_QUEUE = queue_file

        data = {"messages": [{"text": "hello", "source": "test"}]}
        npp.save_queue(data)

        assert queue_file.exists()
        loaded = json.loads(queue_file.read_text())
        assert loaded["messages"][0]["text"] == "hello"

    def test_queue_message_appends(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        queue_file = tmp_state_dir / "hold_queue.json"
        npp.HOLD_QUEUE = queue_file

        npp.queue_message("first message", "test_source", priority="low")
        npp.queue_message("second message", "test_source", priority="high")

        result = npp.load_queue()
        assert len(result["messages"]) == 2
        assert result["messages"][0]["text"] == "first message"
        assert result["messages"][0]["priority"] == "low"
        assert result["messages"][1]["text"] == "second message"
        assert result["messages"][1]["priority"] == "high"

    def test_queue_message_includes_timestamp(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        queue_file = tmp_state_dir / "hold_queue.json"
        npp.HOLD_QUEUE = queue_file

        npp.queue_message("timestamped msg", "test")
        result = npp.load_queue()
        assert "queued_at" in result["messages"][0]

    def test_queue_message_default_priority_is_low(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        queue_file = tmp_state_dir / "hold_queue.json"
        npp.HOLD_QUEUE = queue_file

        npp.queue_message("default priority", "test")
        result = npp.load_queue()
        assert result["messages"][0]["priority"] == "low"


# ============================================================================
# PROACTIVE PEACE — release_queue
# ============================================================================


class TestReleaseQueue:

    def test_release_empty_queue_does_nothing(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        queue_file = tmp_state_dir / "hold_queue.json"
        queue_file.write_text(json.dumps({"messages": []}))
        npp.HOLD_QUEUE = queue_file

        npp.release_queue()
        mock_nova_config.post_both.assert_not_called()

    def test_release_queue_posts_digest(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        queue_file = tmp_state_dir / "hold_queue.json"
        queue_file.write_text(json.dumps({"messages": [
            {"text": "Alert one", "source": "lookout", "priority": "high", "queued_at": "2026-05-01T10:00:00"},
            {"text": "Info two", "source": "gardener", "priority": "low", "queued_at": "2026-05-01T10:05:00"},
        ]}))
        npp.HOLD_QUEUE = queue_file

        npp.release_queue()
        mock_nova_config.post_both.assert_called_once()
        posted_text = mock_nova_config.post_both.call_args[0][0]
        assert "2 while you were away" in posted_text
        assert "Priority:" in posted_text
        assert "Alert one" in posted_text

    @pytest.mark.frame
    def test_release_queue_digest_format(self, tmp_state_dir, mock_nova_config):
        """Verify the Slack message structure: header, priority section, other section."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        msgs = [
            {"text": "URGENT thing", "source": "sentinel", "priority": "high", "queued_at": "2026-05-01T10:00:00"},
            {"text": "Routine A", "source": "gardener", "priority": "low", "queued_at": "2026-05-01T10:01:00"},
            {"text": "Routine B", "source": "analyst", "priority": "low", "queued_at": "2026-05-01T10:02:00"},
        ]
        queue_file = tmp_state_dir / "hold_queue.json"
        queue_file.write_text(json.dumps({"messages": msgs}))
        npp.HOLD_QUEUE = queue_file

        npp.release_queue()
        posted_text = mock_nova_config.post_both.call_args[0][0]

        assert "*Held notifications" in posted_text
        assert "*Priority:*" in posted_text
        assert "*Other (2):*" in posted_text

    @pytest.mark.frame
    def test_release_queue_truncates_long_other_list(self, tmp_state_dir, mock_nova_config):
        """When more than 5 low-priority messages, show +N more."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        msgs = [
            {"text": f"Info {i}", "source": "test", "priority": "low", "queued_at": "2026-05-01T10:00:00"}
            for i in range(8)
        ]
        queue_file = tmp_state_dir / "hold_queue.json"
        queue_file.write_text(json.dumps({"messages": msgs}))
        npp.HOLD_QUEUE = queue_file

        npp.release_queue()
        posted_text = mock_nova_config.post_both.call_args[0][0]
        assert "+3 more" in posted_text

    def test_release_queue_clears_messages(self, tmp_state_dir, mock_nova_config):
        """After releasing, the queue file should be empty."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        queue_file = tmp_state_dir / "hold_queue.json"
        queue_file.write_text(json.dumps({"messages": [
            {"text": "test", "source": "s", "priority": "low", "queued_at": "2026-05-01T10:00:00"}
        ]}))
        npp.HOLD_QUEUE = queue_file

        npp.release_queue()

        result = json.loads(queue_file.read_text())
        assert result["messages"] == []


# ============================================================================
# PROACTIVE PEACE — should_alert
# ============================================================================


class TestShouldAlert:

    def test_should_alert_when_no_state_file(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        npp.STATE_FILE = tmp_state_dir / "nonexistent.json"

        can_send, reason = npp.should_alert()
        assert can_send is True
        assert reason == "available"

    def test_should_not_alert_when_sleeping(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        state_file = tmp_state_dir / "state.json"
        state_file.write_text(json.dumps({"jordan_state": "sleeping"}))
        npp.STATE_FILE = state_file

        can_send, reason = npp.should_alert()
        assert can_send is False
        assert reason == "sleeping"

    def test_should_not_alert_when_dnd(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        state_file = tmp_state_dir / "state.json"
        state_file.write_text(json.dumps({"jordan_state": "dnd"}))
        npp.STATE_FILE = state_file

        can_send, reason = npp.should_alert()
        assert can_send is False
        assert reason == "dnd"

    def test_should_not_alert_when_in_meeting(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        state_file = tmp_state_dir / "state.json"
        state_file.write_text(json.dumps({"jordan_state": "meeting"}))
        npp.STATE_FILE = state_file

        can_send, reason = npp.should_alert()
        assert can_send is False
        assert reason == "meeting"

    def test_should_not_alert_deep_focus(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        state_file = tmp_state_dir / "state.json"
        state_file.write_text(json.dumps({"jordan_state": "coding", "focus_mode": "work"}))
        npp.STATE_FILE = state_file

        can_send, reason = npp.should_alert()
        assert can_send is False
        assert reason == "deep_focus"

    def test_should_alert_when_coding_no_work_focus(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        state_file = tmp_state_dir / "state.json"
        state_file.write_text(json.dumps({"jordan_state": "coding", "focus_mode": "none"}))
        npp.STATE_FILE = state_file

        can_send, reason = npp.should_alert()
        assert can_send is True
        assert reason == "available"

    def test_should_alert_corrupted_state_file(self, tmp_state_dir, mock_nova_config):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        state_file = tmp_state_dir / "state.json"
        state_file.write_text("broken json {{{")
        npp.STATE_FILE = state_file

        can_send, reason = npp.should_alert()
        assert can_send is True
        assert reason == "available"


# ============================================================================
# PROACTIVE PEACE — main() state transitions
# ============================================================================


class TestMainStateMachine:
    """Tests for main() state transitions.

    These tests reload the module first, then apply patches and set globals
    so that the module-level HOUR is correct when main() runs its state logic.
    """

    def _setup_npp(self, mock_nova_config, tmp_state_dir, hour=12):
        """Reload npp module and set file paths + HOUR."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        npp.HOUR = hour
        npp.STATE_FILE = tmp_state_dir / "state.json"
        npp.HOLD_QUEUE = tmp_state_dir / "queue.json"
        return npp

    def test_transition_sleeping_to_available_releases_queue(
        self, tmp_state_dir, mock_nova_config
    ):
        """When state transitions from sleeping -> available, held messages are released."""
        npp = self._setup_npp(mock_nova_config, tmp_state_dir, hour=8)

        state_file = tmp_state_dir / "state.json"
        state_file.write_text(json.dumps({"jordan_state": "sleeping"}))

        with patch.object(npp, "get_focus_mode", return_value="none"), \
             patch.object(npp, "get_screen_state", return_value="active"), \
             patch.object(npp, "get_activity_level", return_value="available"), \
             patch.object(npp, "detect_burnout_signals", return_value=[]), \
             patch.object(npp, "release_queue") as mock_release:
            npp.main()
            mock_release.assert_called_once()

    def test_transition_dnd_to_available_releases_queue(
        self, tmp_state_dir, mock_nova_config
    ):
        npp = self._setup_npp(mock_nova_config, tmp_state_dir, hour=12)

        state_file = tmp_state_dir / "state.json"
        state_file.write_text(json.dumps({"jordan_state": "dnd"}))

        with patch.object(npp, "get_focus_mode", return_value="none"), \
             patch.object(npp, "get_screen_state", return_value="active"), \
             patch.object(npp, "get_activity_level", return_value="available"), \
             patch.object(npp, "detect_burnout_signals", return_value=[]), \
             patch.object(npp, "release_queue") as mock_release:
            npp.main()
            mock_release.assert_called_once()

    def test_no_release_when_staying_available(
        self, tmp_state_dir, mock_nova_config
    ):
        """No queue release when state remains available (not a transition from unavailable)."""
        npp = self._setup_npp(mock_nova_config, tmp_state_dir, hour=12)

        state_file = tmp_state_dir / "state.json"
        state_file.write_text(json.dumps({"jordan_state": "available"}))

        with patch.object(npp, "get_focus_mode", return_value="none"), \
             patch.object(npp, "get_screen_state", return_value="active"), \
             patch.object(npp, "get_activity_level", return_value="available"), \
             patch.object(npp, "detect_burnout_signals", return_value=[]), \
             patch.object(npp, "release_queue") as mock_release:
            npp.main()
            mock_release.assert_not_called()

    def test_no_release_on_first_run_unknown_state(
        self, tmp_state_dir, mock_nova_config
    ):
        """On first run with no previous state file, don't release (prev_state=unknown)."""
        npp = self._setup_npp(mock_nova_config, tmp_state_dir, hour=12)
        npp.STATE_FILE = tmp_state_dir / "nonexistent_state.json"

        with patch.object(npp, "get_focus_mode", return_value="none"), \
             patch.object(npp, "get_screen_state", return_value="active"), \
             patch.object(npp, "get_activity_level", return_value="available"), \
             patch.object(npp, "detect_burnout_signals", return_value=[]), \
             patch.object(npp, "release_queue") as mock_release:
            npp.main()
            mock_release.assert_not_called()

    def test_state_file_preserves_last_burnout_nudge(
        self, tmp_state_dir, mock_nova_config
    ):
        """Persistent key last_burnout_nudge is carried forward across runs."""
        npp = self._setup_npp(mock_nova_config, tmp_state_dir, hour=12)

        state_file = tmp_state_dir / "state.json"
        state_file.write_text(json.dumps({
            "jordan_state": "available",
            "last_burnout_nudge": "2026-05-01",
        }))

        with patch.object(npp, "get_focus_mode", return_value="none"), \
             patch.object(npp, "get_screen_state", return_value="active"), \
             patch.object(npp, "get_activity_level", return_value="available"), \
             patch.object(npp, "detect_burnout_signals", return_value=[]):
            npp.main()

        new_state = json.loads(state_file.read_text())
        assert new_state["last_burnout_nudge"] == "2026-05-01"

    @pytest.mark.frame
    def test_state_file_json_structure(
        self, tmp_state_dir, mock_nova_config
    ):
        """Verify state file contains all expected keys."""
        npp = self._setup_npp(mock_nova_config, tmp_state_dir, hour=10)

        with patch.object(npp, "get_focus_mode", return_value="work"), \
             patch.object(npp, "get_screen_state", return_value="active"), \
             patch.object(npp, "get_activity_level", return_value="coding"), \
             patch.object(npp, "detect_burnout_signals", return_value=[]):
            npp.main()

        state = json.loads(npp.STATE_FILE.read_text())
        expected_keys = {"jordan_state", "focus_mode", "screen", "activity",
                         "burnout_signals", "updated_at", "date", "last_burnout_nudge"}
        assert expected_keys.issubset(set(state.keys()))
        assert state["jordan_state"] == "deep_focus"
        assert state["focus_mode"] == "work"


# ============================================================================
# PROACTIVE PEACE — Burnout nudge logic in main()
# ============================================================================


class TestBurnoutNudge:
    """Tests for burnout nudge logic in main().

    Uses patch.object on the reloaded module to ensure patches bind correctly.
    """

    def _setup_npp(self, mock_nova_config, tmp_state_dir, hour=23, today="2026-05-02"):
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)
        npp.HOUR = hour
        npp.TODAY = today
        npp.STATE_FILE = tmp_state_dir / "state.json"
        npp.HOLD_QUEUE = tmp_state_dir / "queue.json"
        return npp

    def test_burnout_nudge_fires_when_signal_detected(
        self, tmp_state_dir, mock_nova_config
    ):
        npp = self._setup_npp(mock_nova_config, tmp_state_dir, hour=23)

        with patch.object(npp, "get_focus_mode", return_value="none"), \
             patch.object(npp, "get_screen_state", return_value="active"), \
             patch.object(npp, "get_activity_level", return_value="coding"), \
             patch.object(npp, "detect_burnout_signals", return_value=["still_coding_at_23"]):
            npp.main()

        mock_nova_config.post_both.assert_called()
        posted_text = mock_nova_config.post_both.call_args[0][0]
        assert "11pm" in posted_text

    def test_burnout_nudge_only_once_per_day(
        self, tmp_state_dir, mock_nova_config
    ):
        """If last_burnout_nudge matches today, no nudge is sent."""
        npp = self._setup_npp(mock_nova_config, tmp_state_dir, hour=23)

        state_file = tmp_state_dir / "state.json"
        state_file.write_text(json.dumps({
            "jordan_state": "coding",
            "last_burnout_nudge": "2026-05-02",  # already nudged today
        }))

        with patch.object(npp, "get_focus_mode", return_value="none"), \
             patch.object(npp, "get_screen_state", return_value="active"), \
             patch.object(npp, "get_activity_level", return_value="coding"), \
             patch.object(npp, "detect_burnout_signals", return_value=["still_coding_at_23"]):
            npp.main()

        # post_both should NOT be called for burnout nudge
        mock_nova_config.post_both.assert_not_called()

    def test_weekend_nudge_message(
        self, tmp_state_dir, mock_nova_config
    ):
        npp = self._setup_npp(mock_nova_config, tmp_state_dir, hour=10, today="2026-05-03")

        with patch.object(npp, "get_focus_mode", return_value="none"), \
             patch.object(npp, "get_screen_state", return_value="active"), \
             patch.object(npp, "get_activity_level", return_value="focus_likely"), \
             patch.object(npp, "detect_burnout_signals", return_value=["weekend_commits"]):
            npp.main()

        mock_nova_config.post_both.assert_called()
        posted_text = mock_nova_config.post_both.call_args[0][0]
        assert "Weekend" in posted_text or "weekend" in posted_text.lower()

    def test_burnout_nudge_updates_state_file(
        self, tmp_state_dir, mock_nova_config
    ):
        """After nudge, state file should record today's date as last_burnout_nudge."""
        npp = self._setup_npp(mock_nova_config, tmp_state_dir, hour=23)

        with patch.object(npp, "get_focus_mode", return_value="none"), \
             patch.object(npp, "get_screen_state", return_value="active"), \
             patch.object(npp, "get_activity_level", return_value="coding"), \
             patch.object(npp, "detect_burnout_signals", return_value=["still_coding_at_23"]):
            npp.main()

        state = json.loads(npp.STATE_FILE.read_text())
        assert state["last_burnout_nudge"] == "2026-05-02"


# ============================================================================
# SUBAGENT FRAMEWORK — SubAgent base class
# ============================================================================


class TestSubAgentBase:

    @patch("nova_subagent.redis.from_url")
    def test_subagent_init(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """SubAgent.__init__ connects to Redis and sets up pubsub."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_agent"
            channels = ["test"]

            async def handle(self, task):
                return {"ok": True}

        agent = TestAgent()
        assert agent.name == "test_agent"
        assert agent._running is False
        assert agent._task_count == 0

    @patch("nova_subagent.redis.from_url")
    def test_subagent_register(self, mock_redis_from_url, mock_nova_config, mock_nova_logger, tmp_path):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)
        nova_subagent.REGISTRY_PATH = tmp_path / "subagents" / "runs.json"

        class TestAgent(nova_subagent.SubAgent):
            name = "test_reg"
            model = "test-model:1b"
            channels = ["test"]
            description = "Test registration"

            async def handle(self, task):
                return None

        agent = TestAgent()
        agent._register()

        registry = json.loads(nova_subagent.REGISTRY_PATH.read_text())
        assert "test_reg" in registry["runs"]
        assert registry["runs"]["test_reg"]["status"] == "running"
        assert registry["runs"]["test_reg"]["model"] == "test-model:1b"

    @patch("nova_subagent.redis.from_url")
    def test_subagent_deregister(self, mock_redis_from_url, mock_nova_config, mock_nova_logger, tmp_path):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)
        nova_subagent.REGISTRY_PATH = tmp_path / "subagents" / "runs.json"

        class TestAgent(nova_subagent.SubAgent):
            name = "test_dereg"
            channels = ["test"]

            async def handle(self, task):
                return None

        agent = TestAgent()
        agent._register()
        agent._task_count = 5
        agent._deregister()

        registry = json.loads(nova_subagent.REGISTRY_PATH.read_text())
        assert registry["runs"]["test_dereg"]["status"] == "stopped"
        assert registry["runs"]["test_dereg"]["task_count"] == 5
        mock_redis.delete.assert_called()

    @patch("nova_subagent.redis.from_url")
    def test_subagent_registry_empty_on_first_load(self, mock_redis_from_url, mock_nova_config, mock_nova_logger, tmp_path):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)
        nova_subagent.REGISTRY_PATH = tmp_path / "subagents" / "runs.json"

        class TestAgent(nova_subagent.SubAgent):
            name = "test_empty"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()
        registry = agent._load_registry()
        assert registry == {"version": 2, "runs": {}}

    @patch("nova_subagent.redis.from_url")
    def test_is_backend_healthy_ollama(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_health"
            backend = "ollama"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()

        with patch("nova_subagent.urllib.request.urlopen") as mock_url:
            mock_url.return_value = MagicMock()
            assert agent.is_backend_healthy() is True

        with patch("nova_subagent.urllib.request.urlopen") as mock_url:
            mock_url.side_effect = Exception("connection refused")
            assert agent.is_backend_healthy() is False

    @patch("nova_subagent.redis.from_url")
    def test_is_backend_healthy_mlx(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_health_mlx"
            backend = "mlx"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()

        with patch("nova_subagent.urllib.request.urlopen") as mock_url:
            mock_url.return_value = MagicMock()
            assert agent.is_backend_healthy() is True


# ============================================================================
# SUBAGENT — LLM Inference
# ============================================================================


class TestSubAgentInference:

    @patch("nova_subagent.redis.from_url")
    def test_infer_ollama_success(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_infer"
            backend = "ollama"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"response": "Hello from Ollama"}).encode()

        with patch("nova_subagent.urllib.request.urlopen", return_value=mock_resp) as mock_url:
            result = _run_async(agent.infer("test prompt", system="be helpful"))
            assert result == "Hello from Ollama"

    @patch("nova_subagent.redis.from_url")
    def test_infer_ollama_failure_raises(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_infer_fail"
            backend = "ollama"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()

        with patch("nova_subagent.urllib.request.urlopen", side_effect=Exception("Ollama down")):
            with pytest.raises(Exception, match="Ollama down"):
                _run_async(agent.infer("test prompt"))

    @patch("nova_subagent.redis.from_url")
    def test_infer_mlx_success(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_infer_mlx"
            backend = "mlx"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": "Hello from MLX"}}]
        }).encode()

        with patch("nova_subagent.urllib.request.urlopen", return_value=mock_resp):
            result = _run_async(agent.infer("test prompt", system="be helpful"))
            assert result == "Hello from MLX"

    @patch("nova_subagent.redis.from_url")
    def test_infer_unknown_backend_raises(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_bad_backend"
            backend = "xyzzy"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()
        with pytest.raises(ValueError, match="Unknown backend"):
            _run_async(agent.infer("test"))


# ============================================================================
# SUBAGENT — dispatch / publish
# ============================================================================


class TestSubAgentDispatch:

    @patch("nova_subagent.redis.from_url")
    def test_dispatch_publishes_to_channel(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        nova_subagent.SubAgent.dispatch("email", {"type": "new_email", "subject": "Test"})

        mock_redis.publish.assert_called_once()
        channel_arg = mock_redis.publish.call_args[0][0]
        assert channel_arg == "nova:task:email"

        payload = json.loads(mock_redis.publish.call_args[0][1])
        assert payload["type"] == "new_email"
        assert "_dispatched_at" in payload
        assert "id" in payload

    @patch("nova_subagent.redis.from_url")
    def test_publish_result_includes_agent_metadata(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_publish"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()
        task = {"id": "task-123", "_channel": "nova:task:test"}
        result = {"summary": "done"}

        _run_async(agent._publish_result(task, result))

        mock_redis.publish.assert_called_once()
        channel = mock_redis.publish.call_args[0][0]
        assert channel == "nova:result:test_publish"

        published = json.loads(mock_redis.publish.call_args[0][1])
        assert published["_agent"] == "test_publish"
        assert published["_task_id"] == "task-123"
        assert "_completed_at" in published


# ============================================================================
# ANALYST AGENT
# ============================================================================


class TestAnalystAgent:

    @patch("nova_subagent.redis.from_url")
    def test_analyst_handle_empty_content_returns_none(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_analyst
        importlib.reload(nova_agent_analyst)

        agent = nova_agent_analyst.AnalystAgent()
        result = _run_async(agent.handle({"type": "email", "content": ""}))
        assert result is None

    @patch("nova_subagent.redis.from_url")
    def test_analyst_handle_valid_json_response(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_analyst
        importlib.reload(nova_agent_analyst)

        agent = nova_agent_analyst.AnalystAgent()

        llm_response = json.dumps({
            "summary": "Important email about project deadline",
            "priority": "high",
            "action_items": ["Reply by Friday"],
            "sentiment": "urgent",
            "flag_jordan": True,
        })

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
            with patch.object(agent, "report_to_jordan", new_callable=AsyncMock) as mock_report:
                with patch.object(agent, "remember", new_callable=AsyncMock):
                    result = _run_async(agent.handle({
                        "type": "email",
                        "content": "Project deadline is Friday",
                        "subject": "URGENT: Deadline",
                    }))

        assert result["priority"] == "high"
        assert result["source_type"] == "email"
        assert result["flag_jordan"] is True
        mock_report.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_analyst_handle_think_tags_stripped(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """deepseek-r1 wraps reasoning in <think>...</think> — these must be stripped."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_analyst
        importlib.reload(nova_agent_analyst)

        agent = nova_agent_analyst.AnalystAgent()

        llm_response = '<think>Let me analyze this...</think>{"summary": "Test", "priority": "low", "action_items": [], "sentiment": "neutral", "flag_jordan": false}'

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
            with patch.object(agent, "notify", new_callable=AsyncMock):
                with patch.object(agent, "remember", new_callable=AsyncMock):
                    result = _run_async(agent.handle({
                        "type": "alert",
                        "content": "Test alert",
                        "subject": "Test",
                    }))

        assert result["summary"] == "Test"
        assert result["priority"] == "low"

    @patch("nova_subagent.redis.from_url")
    def test_analyst_handle_unparseable_response_fallback(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """When LLM returns non-JSON, fallback to raw text summary."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_analyst
        importlib.reload(nova_agent_analyst)

        agent = nova_agent_analyst.AnalystAgent()

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value="Just some text, no JSON here."):
            with patch.object(agent, "notify", new_callable=AsyncMock):
                with patch.object(agent, "remember", new_callable=AsyncMock):
                    result = _run_async(agent.handle({
                        "type": "email",
                        "content": "Hello there",
                        "subject": "Hi",
                    }))

        assert result["priority"] == "medium"
        assert result["flag_jordan"] is False
        assert "Just some text" in result["summary"]

    @patch("nova_subagent.redis.from_url")
    def test_analyst_inference_failure_returns_none(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_analyst
        importlib.reload(nova_agent_analyst)

        agent = nova_agent_analyst.AnalystAgent()

        with patch.object(agent, "infer", new_callable=AsyncMock, side_effect=Exception("Ollama down")):
            result = _run_async(agent.handle({
                "type": "email",
                "content": "Test",
                "subject": "Test",
            }))

        assert result is None

    @pytest.mark.frame
    @patch("nova_subagent.redis.from_url")
    def test_analyst_slack_message_format(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Verify Slack message includes emoji, priority, type, subject, summary, action items."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_analyst
        importlib.reload(nova_agent_analyst)

        agent = nova_agent_analyst.AnalystAgent()

        llm_response = json.dumps({
            "summary": "Meeting rescheduled to next week",
            "priority": "medium",
            "action_items": ["Update calendar", "Notify team"],
            "sentiment": "neutral",
            "flag_jordan": False,
        })

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
            with patch.object(agent, "notify", new_callable=AsyncMock) as mock_notify:
                with patch.object(agent, "remember", new_callable=AsyncMock):
                    _run_async(agent.handle({
                        "type": "meeting",
                        "content": "Meeting rescheduled",
                        "subject": "Team sync",
                    }))

        msg = mock_notify.call_args[0][0]
        assert "*Analyst Report*" in msg
        assert "MEDIUM" in msg
        assert "meeting" in msg
        assert "Action Items" in msg


# ============================================================================
# CODER AGENT
# ============================================================================


class TestCoderAgent:

    @patch("nova_subagent.redis.from_url")
    def test_coder_handle_empty_content_returns_none(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_coder
        importlib.reload(nova_agent_coder)

        agent = nova_agent_coder.CoderAgent()
        result = _run_async(agent.handle({"type": "review", "content": ""}))
        assert result is None

    @patch("nova_subagent.redis.from_url")
    def test_coder_handle_valid_review(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_coder
        importlib.reload(nova_agent_coder)

        agent = nova_agent_coder.CoderAgent()

        llm_response = json.dumps({
            "summary": "Well-structured code with minor issues",
            "issues": [
                {"severity": "medium", "description": "Missing error handling", "file": "main.py", "line": 42}
            ],
            "security_concerns": [],
            "suggestions": ["Add try/except around network call"],
            "quality_score": 7,
            "flag_jordan": False,
        })

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
            with patch.object(agent, "notify", new_callable=AsyncMock) as mock_notify:
                result = _run_async(agent.handle({
                    "type": "review",
                    "content": "def main(): pass",
                    "file": "main.py",
                    "repo": "MLXCode",
                }))

        assert result["quality_score"] == 7
        assert result["source_repo"] == "MLXCode"
        mock_notify.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_coder_security_concerns_flag_jordan(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Security concerns always flag Jordan."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_coder
        importlib.reload(nova_agent_coder)

        agent = nova_agent_coder.CoderAgent()

        llm_response = json.dumps({
            "summary": "Critical SQL injection vulnerability",
            "issues": [{"severity": "critical", "description": "SQL injection in login", "file": "auth.py", "line": 15}],
            "security_concerns": ["SQL injection via unsanitized user input"],
            "quality_score": 2,
            "flag_jordan": True,
        })

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
            with patch.object(agent, "report_to_jordan", new_callable=AsyncMock) as mock_report:
                result = _run_async(agent.handle({
                    "type": "review",
                    "content": "query = f'SELECT * FROM users WHERE name = {user_input}'",
                    "file": "auth.py",
                }))

        mock_report.assert_called_once()
        msg = mock_report.call_args[0][0]
        assert "Security" in msg

    @patch("nova_subagent.redis.from_url")
    def test_coder_no_think_tag_stripping(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Coder also strips /no_think tags from qwen3-coder."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_coder
        importlib.reload(nova_agent_coder)

        agent = nova_agent_coder.CoderAgent()

        llm_response = '/no_think {"summary": "Clean", "issues": [], "quality_score": 9, "flag_jordan": false}'

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
            result = _run_async(agent.handle({
                "type": "review",
                "content": "print('hello')",
            }))

        assert result["quality_score"] == 9

    @patch("nova_subagent.redis.from_url")
    def test_coder_reads_content_from_diff_key(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Coder checks 'content', then 'diff', then 'text' keys."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_coder
        importlib.reload(nova_agent_coder)

        agent = nova_agent_coder.CoderAgent()

        llm_response = json.dumps({"summary": "OK", "issues": [], "quality_score": 8, "flag_jordan": False})

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
            result = _run_async(agent.handle({
                "type": "review",
                "diff": "+def new_func(): pass",
            }))

        assert result is not None


# ============================================================================
# GARDENER AGENT
# ============================================================================


class TestGardenerAgent:

    @patch("nova_subagent.redis.from_url")
    def test_gardener_handle_with_source(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_gardener
        importlib.reload(nova_agent_gardener)

        agent = nova_agent_gardener.MemoryGardener()

        with patch.object(agent, "_scan_source", new_callable=AsyncMock, return_value={"findings": []}) as mock_scan:
            result = _run_async(agent.handle({"source": "email_archive"}))
            mock_scan.assert_called_once_with("email_archive")

    @patch("nova_subagent.redis.from_url")
    def test_gardener_handle_without_source_runs_full_scan(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_gardener
        importlib.reload(nova_agent_gardener)

        agent = nova_agent_gardener.MemoryGardener()

        with patch.object(agent, "_full_scan", new_callable=AsyncMock, return_value={"findings": []}) as mock_scan:
            result = _run_async(agent.handle({}))
            mock_scan.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_gardener_auto_merge_keeps_longest(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Auto-merge should keep the longest memory and delete shorter duplicates."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_gardener
        importlib.reload(nova_agent_gardener)

        agent = nova_agent_gardener.MemoryGardener()

        memories = [
            {"id": "short", "text": "Hello"},
            {"id": "long", "text": "Hello there, this is a much longer memory with more detail"},
        ]

        def mock_urlopen(url_or_req, timeout=None):
            url = url_or_req if isinstance(url_or_req, str) else url_or_req.full_url
            resp = MagicMock()
            if "/get?id=short" in url:
                resp.read.return_value = json.dumps(memories[0]).encode()
            elif "/get?id=long" in url:
                resp.read.return_value = json.dumps(memories[1]).encode()
            elif "/forget" in url:
                resp.read.return_value = b'{"ok": true}'
            return resp

        with patch("nova_agent_gardener.urllib.request.urlopen", side_effect=mock_urlopen):
            deleted = _run_async(agent._auto_merge(["short", "long"]))

        assert deleted == 1  # short one deleted

    @patch("nova_subagent.redis.from_url")
    def test_gardener_auto_merge_less_than_two_ids_no_op(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_gardener
        importlib.reload(nova_agent_gardener)

        agent = nova_agent_gardener.MemoryGardener()
        deleted = _run_async(agent._auto_merge(["only_one"]))
        assert deleted == 0

    @patch("nova_subagent.redis.from_url")
    def test_gardener_scan_source_few_memories_returns_empty(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """If fewer than 3 memories, skip scanning."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_gardener
        importlib.reload(nova_agent_gardener)

        agent = nova_agent_gardener.MemoryGardener()

        def mock_urlopen(url, timeout=None):
            resp = MagicMock()
            resp.read.return_value = json.dumps([{"id": "1", "text": "only one"}]).encode()
            return resp

        with patch("nova_agent_gardener.urllib.request.urlopen", side_effect=mock_urlopen):
            result = _run_async(agent._scan_source("email"))

        assert result == {"findings": []}


# ============================================================================
# LIBRARIAN AGENT
# ============================================================================


class TestLibrarianAgent:

    @patch("nova_subagent.redis.from_url")
    def test_librarian_dispatch_curate_batch(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_librarian
        importlib.reload(nova_agent_librarian)

        agent = nova_agent_librarian.LibrarianAgent()

        with patch.object(agent, "_curate_batch", new_callable=AsyncMock, return_value={"findings": []}) as mock_curate:
            result = _run_async(agent.handle({"type": "curate_batch", "source": "email"}))
            mock_curate.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_librarian_dispatch_check_duplicates(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_librarian
        importlib.reload(nova_agent_librarian)

        agent = nova_agent_librarian.LibrarianAgent()

        with patch.object(agent, "_check_duplicates", new_callable=AsyncMock, return_value={"duplicates": []}) as mock_dup:
            result = _run_async(agent.handle({"type": "check_duplicates", "text": "test memory"}))
            mock_dup.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_librarian_dispatch_scan_source(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_librarian
        importlib.reload(nova_agent_librarian)

        agent = nova_agent_librarian.LibrarianAgent()

        with patch.object(agent, "_scan_source", new_callable=AsyncMock, return_value=None) as mock_scan:
            result = _run_async(agent.handle({"type": "scan_source", "source": "music"}))
            mock_scan.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_librarian_curate_batch_too_few_memories(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_librarian
        importlib.reload(nova_agent_librarian)

        agent = nova_agent_librarian.LibrarianAgent()

        with patch.object(agent, "recall", new_callable=AsyncMock, return_value=[{"id": "1", "text": "solo"}]):
            result = _run_async(agent._curate_batch({"query": "test", "batch_size": 5}))
            assert result is None

    @patch("nova_subagent.redis.from_url")
    def test_librarian_check_duplicates_empty_text(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_librarian
        importlib.reload(nova_agent_librarian)

        agent = nova_agent_librarian.LibrarianAgent()
        result = _run_async(agent._check_duplicates({"text": ""}))
        assert result is None

    @pytest.mark.frame
    @patch("nova_subagent.redis.from_url")
    def test_librarian_findings_report_format(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Verify Slack report includes all expected sections."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_librarian
        importlib.reload(nova_agent_librarian)

        agent = nova_agent_librarian.LibrarianAgent()

        memories = [
            {"id": "a", "text": "Memory A", "source": "test", "score": 0.95},
            {"id": "b", "text": "Memory B", "source": "test", "score": 0.90},
            {"id": "c", "text": "Memory C", "source": "test", "score": 0.85},
        ]

        llm_response = json.dumps({
            "findings": [
                {"type": "duplicate", "severity": "high", "memory_ids": ["a", "b"],
                 "description": "Same info restated", "recommendation": "merge"}
            ],
            "stats": {"memories_analyzed": 3, "duplicates_found": 1},
        })

        with patch.object(agent, "recall", new_callable=AsyncMock, return_value=memories):
            with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
                with patch.object(agent, "report_to_jordan", new_callable=AsyncMock) as mock_report:
                    result = _run_async(agent._curate_batch({"query": "test", "batch_size": 5}))

        mock_report.assert_called_once()
        msg = mock_report.call_args[0][0]
        assert "*Librarian Report*" in msg
        assert "DUPLICATE" in msg
        assert "Recommendation" in msg


# ============================================================================
# LOOKOUT AGENT
# ============================================================================


class TestLookoutAgent:

    @patch("nova_subagent.redis.from_url")
    def test_lookout_no_image_returns_none(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_lookout
        importlib.reload(nova_agent_lookout)

        agent = nova_agent_lookout.LookoutAgent()
        result = _run_async(agent.handle({"type": "vision", "camera": "front_door"}))
        assert result is None

    @patch("nova_subagent.redis.from_url")
    def test_lookout_vehicle_suppressed(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Vehicle detections should be suppressed (too noisy)."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_lookout
        importlib.reload(nova_agent_lookout)

        agent = nova_agent_lookout.LookoutAgent()

        llm_response = json.dumps({
            "description": "Car parked on street",
            "anomaly_detected": True,
            "anomaly_type": "vehicle",
            "severity": "low",
            "flag_jordan": False,
        })

        with patch.object(agent, "_infer_vision", new_callable=AsyncMock, return_value=llm_response):
            result = _run_async(agent.handle({
                "type": "vision",
                "camera": "street",
                "image_base64": "AAAA",
            }))

        assert result["anomaly_detected"] is False

    @patch("nova_subagent.redis.from_url")
    def test_lookout_genuine_anomaly_notifies(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_lookout
        importlib.reload(nova_agent_lookout)

        agent = nova_agent_lookout.LookoutAgent()

        llm_response = json.dumps({
            "description": "Unknown person at back gate",
            "anomaly_detected": True,
            "anomaly_type": "person",
            "severity": "high",
            "confidence": 0.85,
            "details": "Unrecognized person trying gate",
            "flag_jordan": True,
        })

        with patch.object(agent, "_infer_vision", new_callable=AsyncMock, return_value=llm_response):
            with patch.object(agent, "report_to_jordan", new_callable=AsyncMock) as mock_report:
                result = _run_async(agent.handle({
                    "type": "vision",
                    "camera": "back_gate",
                    "image_base64": "AAAA",
                }))

        assert result["anomaly_detected"] is True
        mock_report.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_lookout_reads_image_from_path(self, mock_redis_from_url, mock_nova_config, mock_nova_logger, tmp_path):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_lookout
        importlib.reload(nova_agent_lookout)

        agent = nova_agent_lookout.LookoutAgent()

        # Create a fake image file
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"\xff\xd8\xff\xe0fake_jpeg_data")

        llm_response = json.dumps({
            "description": "Normal scene",
            "anomaly_detected": False,
            "severity": "none",
            "flag_jordan": False,
        })

        with patch.object(agent, "_infer_vision", new_callable=AsyncMock, return_value=llm_response):
            result = _run_async(agent.handle({
                "type": "vision",
                "camera": "test",
                "image_path": str(img_path),
            }))

        assert result is not None
        assert result["anomaly_detected"] is False

    @patch("nova_subagent.redis.from_url")
    def test_lookout_image_read_failure(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_lookout
        importlib.reload(nova_agent_lookout)

        agent = nova_agent_lookout.LookoutAgent()
        result = _run_async(agent.handle({
            "type": "vision",
            "camera": "test",
            "image_path": "/nonexistent/image.jpg",
        }))
        assert result is None

    @pytest.mark.frame
    @patch("nova_subagent.redis.from_url")
    def test_lookout_alert_message_format(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_lookout
        importlib.reload(nova_agent_lookout)

        agent = nova_agent_lookout.LookoutAgent()

        llm_response = json.dumps({
            "description": "Animal in yard",
            "anomaly_detected": True,
            "anomaly_type": "animal",
            "severity": "medium",
            "confidence": 0.7,
            "details": "Coyote spotted",
            "flag_jordan": False,
        })

        with patch.object(agent, "_infer_vision", new_callable=AsyncMock, return_value=llm_response):
            with patch.object(agent, "notify", new_callable=AsyncMock) as mock_notify:
                _run_async(agent.handle({
                    "type": "vision",
                    "camera": "backyard",
                    "image_base64": "AAAA",
                }))

        msg = mock_notify.call_args[0][0]
        assert "*Lookout Alert*" in msg
        assert "MEDIUM" in msg
        assert "backyard" in msg
        assert "Confidence" in msg


# ============================================================================
# SENTINEL AGENT
# ============================================================================


class TestSentinelAgent:

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_dispatch_nmap(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        with patch.object(agent, "_analyze_nmap", new_callable=AsyncMock, return_value=None) as mock_nmap:
            _run_async(agent.handle({"type": "nmap_scan"}))
            mock_nmap.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_dispatch_camera_alert(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        with patch.object(agent, "_analyze_camera", new_callable=AsyncMock, return_value=None) as mock_cam:
            _run_async(agent.handle({"type": "camera_alert"}))
            mock_cam.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_dispatch_unifi_event(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        with patch.object(agent, "_analyze_unifi", new_callable=AsyncMock, return_value=None) as mock_unifi:
            _run_async(agent.handle({"type": "unifi_event"}))
            mock_unifi.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_dispatch_threat_assessment(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        with patch.object(agent, "_threat_assessment", new_callable=AsyncMock, return_value=None) as mock_threat:
            _run_async(agent.handle({"type": "threat_assessment"}))
            mock_threat.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_dispatch_generic(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        with patch.object(agent, "_generic_security", new_callable=AsyncMock, return_value=None) as mock_gen:
            _run_async(agent.handle({"type": "something_else"}))
            mock_gen.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_parse_response_valid_json(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        response = json.dumps({"risk_level": "high", "flag_jordan": True, "summary": "Threat detected"})
        result = agent._parse_response(response)
        assert result["risk_level"] == "high"
        assert result["flag_jordan"] is True

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_parse_response_with_think_tags(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        response = '<think>Analyzing the signals...</think>{"risk_level": "low", "flag_jordan": false}'
        result = agent._parse_response(response)
        assert result["risk_level"] == "low"

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_parse_response_invalid_json_fallback(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        result = agent._parse_response("This is just plain text, no JSON.")
        assert result["risk_level"] == "unknown"
        assert result["flag_jordan"] is False
        assert "plain text" in result["summary"]

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_camera_vehicle_suppression(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Vehicle-only camera events should be suppressed (returns None)."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        result = _run_async(agent._analyze_camera({
            "type": "camera_alert",
            "smart_types": ["vehicle", "licensePlate"],
            "camera": "street_cam",
        }))
        assert result is None

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_nmap_no_devices_returns_none(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        # Mock the NovaControl API calls to return no devices
        with patch("nova_agent_sentinel.urllib.request.urlopen", side_effect=Exception("API down")):
            result = _run_async(agent._analyze_nmap({"type": "nmap_scan"}))

        assert result is None

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_threat_assessment_no_signals_returns_none(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()
        result = _run_async(agent._threat_assessment({"type": "threat_assessment", "signals": []}))
        assert result is None

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_generic_security_empty_text_returns_none(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()
        result = _run_async(agent._generic_security({"text": ""}))
        assert result is None

    @pytest.mark.frame
    @patch("nova_subagent.redis.from_url")
    def test_sentinel_report_security_format(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Verify security report message format with emoji, risk level, findings."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        result = {
            "risk_level": "critical",
            "summary": "Unauthorized device detected on network",
            "findings": [
                {"description": "Unknown MAC address on VLAN 10"},
            ],
            "flag_jordan": True,
        }

        with patch.object(agent, "report_to_jordan", new_callable=AsyncMock) as mock_report:
            _run_async(agent._report_security(result, "Network Scan"))

        mock_report.assert_called_once()
        msg = mock_report.call_args[0][0]
        assert "*Sentinel" in msg
        assert "CRITICAL" in msg
        assert "Network Scan" in msg
        assert "Unauthorized device" in msg


# ============================================================================
# FUNCTIONAL — Full workflow tests
# ============================================================================


class TestFunctionalWorkflows:

    @pytest.mark.functional
    def test_full_cycle_sleep_queue_wake_release(
        self, tmp_state_dir, mock_nova_config
    ):
        """Full cycle: sleeping -> messages queued -> wake up -> digest released."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        state_file = tmp_state_dir / "state.json"
        queue_file = tmp_state_dir / "queue.json"
        npp.STATE_FILE = state_file
        npp.HOLD_QUEUE = queue_file

        # Step 1: Run during sleep — establishes sleeping state
        npp.HOUR = 3
        with patch.object(npp, "get_focus_mode", return_value="sleep"), \
             patch.object(npp, "get_screen_state", return_value="locked"), \
             patch.object(npp, "get_activity_level", return_value="sleeping"), \
             patch.object(npp, "detect_burnout_signals", return_value=[]):
            npp.main()

        state = json.loads(state_file.read_text())
        assert state["jordan_state"] == "sleeping"

        # Step 2: Queue some messages while sleeping
        npp.queue_message("Nightly report completed", "nightly_report", "low")
        npp.queue_message("Critical: New unknown device on network", "sentinel", "high")
        queue = npp.load_queue()
        assert len(queue["messages"]) == 2

        # Step 3: Jordan wakes up — transition to available
        npp.HOUR = 8
        with patch.object(npp, "get_focus_mode", return_value="none"), \
             patch.object(npp, "get_screen_state", return_value="active"), \
             patch.object(npp, "get_activity_level", return_value="available"), \
             patch.object(npp, "detect_burnout_signals", return_value=[]):
            npp.main()

        # Verify digest was posted
        mock_nova_config.post_both.assert_called()
        posted_text = mock_nova_config.post_both.call_args[0][0]
        assert "2 while you were away" in posted_text
        assert "Critical: New unknown device" in posted_text

        # Verify queue is now empty
        queue = npp.load_queue()
        assert len(queue["messages"]) == 0

    @pytest.mark.functional
    def test_state_persistence_across_runs(
        self, tmp_state_dir, mock_nova_config
    ):
        """State file is correctly read and written across multiple main() calls."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        npp.STATE_FILE = tmp_state_dir / "state.json"
        npp.HOLD_QUEUE = tmp_state_dir / "queue.json"
        npp.TODAY = "2026-05-02"

        # Run 1: coding with work focus -> deep_focus
        npp.HOUR = 10
        with patch.object(npp, "get_focus_mode", return_value="work"), \
             patch.object(npp, "get_screen_state", return_value="active"), \
             patch.object(npp, "get_activity_level", return_value="coding"), \
             patch.object(npp, "detect_burnout_signals", return_value=[]):
            npp.main()

        state = json.loads(npp.STATE_FILE.read_text())
        assert state["jordan_state"] == "deep_focus"

        # Run 2: still coding, but with a burnout signal at 23:00
        npp.HOUR = 23
        with patch.object(npp, "get_focus_mode", return_value="none"), \
             patch.object(npp, "get_screen_state", return_value="active"), \
             patch.object(npp, "get_activity_level", return_value="coding"), \
             patch.object(npp, "detect_burnout_signals", return_value=["still_coding_at_23"]):
            npp.main()

        state = json.loads(npp.STATE_FILE.read_text())
        assert state["last_burnout_nudge"] == "2026-05-02"

        # Run 3: burnout signal again, but same day — should not nudge again
        mock_nova_config.post_both.reset_mock()
        with patch.object(npp, "get_focus_mode", return_value="none"), \
             patch.object(npp, "get_screen_state", return_value="active"), \
             patch.object(npp, "get_activity_level", return_value="coding"), \
             patch.object(npp, "detect_burnout_signals", return_value=["still_coding_at_23"]):
            npp.main()

        # post_both should NOT be called again (already nudged today)
        mock_nova_config.post_both.assert_not_called()

    @pytest.mark.functional
    @patch("nova_subagent.redis.from_url")
    def test_agent_configuration_properties(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Verify all agents have correct configuration: name, model, channels, backend."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib

        # Reload all agent modules
        import nova_agent_analyst
        import nova_agent_coder
        import nova_agent_gardener
        import nova_agent_librarian
        import nova_agent_lookout
        import nova_agent_sentinel
        for mod in [nova_agent_analyst, nova_agent_coder, nova_agent_gardener,
                     nova_agent_librarian, nova_agent_lookout, nova_agent_sentinel]:
            importlib.reload(mod)

        agents = {
            "analyst": nova_agent_analyst.AnalystAgent(),
            "coder": nova_agent_coder.CoderAgent(),
            "gardener": nova_agent_gardener.MemoryGardener(),
            "librarian": nova_agent_librarian.LibrarianAgent(),
            "lookout": nova_agent_lookout.LookoutAgent(),
            "sentinel": nova_agent_sentinel.SecuritySentinel(),
        }

        for name, agent in agents.items():
            assert agent.name == name, f"{name} has wrong name: {agent.name}"
            assert agent.model, f"{name} has no model"
            assert isinstance(agent.channels, list), f"{name} channels not a list"
            assert len(agent.channels) > 0, f"{name} has no channels"
            assert agent.backend in ("ollama", "mlx"), f"{name} has invalid backend: {agent.backend}"
            assert agent.description, f"{name} has no description"

        # Specific model checks
        assert agents["analyst"].model == "deepseek-r1:8b"
        assert agents["coder"].model == "qwen3-coder:30b"
        assert agents["lookout"].model == "qwen3-vl:4b"
        assert agents["librarian"].backend == "mlx"
        assert agents["sentinel"].model == "deepseek-r1:8b"

    @pytest.mark.functional
    def test_should_alert_integration_with_queue(self, tmp_state_dir, mock_nova_config):
        """The should_alert + queue_message pattern used by other scripts."""
        import importlib
        import nova_proactive_peace as npp
        importlib.reload(npp)

        state_file = tmp_state_dir / "state.json"
        queue_file = tmp_state_dir / "queue.json"
        npp.STATE_FILE = state_file
        npp.HOLD_QUEUE = queue_file

        # Set state to sleeping
        state_file.write_text(json.dumps({"jordan_state": "sleeping"}))

        # Script checks should_alert, decides to queue
        can_send, reason = npp.should_alert()
        assert can_send is False

        npp.queue_message("Something happened while you slept", "test_script", "low")

        # Verify it's queued
        queue = npp.load_queue()
        assert len(queue["messages"]) == 1
        assert queue["messages"][0]["source"] == "test_script"
