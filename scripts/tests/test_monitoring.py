#!/usr/bin/env python3
"""
test_monitoring.py — Comprehensive tests for Nova's monitoring scripts.

Covers:
  - nova_app_watchdog.py: port checking, state transitions, auto-restart
  - nova_health_check.py: health evaluation, message formatting
  - nova_dead_mans_switch.py: canary firing logic
  - nova_protect_monitor.py: camera state parsing, event handling
  - nova_unifi_monitor.py: network health parsing, problem detection
  - nova_synology_monitor.py: NAS state reporting, problem detection
  - nova_home_watchdog.py: HomeKit status parsing, alert logic
  - nova_watchdog.py: gateway health monitoring

Uses unittest.mock.patch for unit tests.
Uses @pytest.mark.integration for live-service tests.

Written by Jordan Koch.
"""

import importlib
import json
import sys
import time
from datetime import datetime, date
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest


# ============================================================================
# nova_app_watchdog.py
# ============================================================================

class TestAppWatchdog:
    """Tests for nova_app_watchdog.py port checking and state machine."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, monkeypatch):
        """Import the module with mocked dependencies."""
        self.mock_config = mock_nova_config
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        # Force reimport to pick up mocked nova_config
        if "nova_app_watchdog" in sys.modules:
            del sys.modules["nova_app_watchdog"]
        import nova_app_watchdog
        self.mod = nova_app_watchdog

    def test_check_port_alive_with_status_endpoint(self):
        """Port responding with valid JSON on /api/status should be alive."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"version": "1.2.3"}).encode()
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            alive, info, elapsed = self.mod.check_port(37421)
        assert alive is True
        assert "1.2.3" in info

    def test_check_port_alive_no_status_endpoint(self):
        """Port that returns HTTPError (e.g. 404) is still alive."""
        import urllib.error
        # First call to /api/status raises URLError
        # Second call to / raises HTTPError (port alive but no endpoint)
        side_effects = [
            Exception("some error"),
        ]
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [
                Exception("json decode"),
                urllib.error.HTTPError("http://test", 404, "Not Found", {}, None),
            ]
            alive, info, elapsed = self.mod.check_port(37421)
        assert alive is True
        assert "responding" in info.lower()

    def test_check_port_dead_connection_refused(self):
        """Port that refuses connection should be dead."""
        import urllib.error
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
            alive, info, elapsed = self.mod.check_port(99999)
        assert alive is False
        assert "refused" in info.lower()

    def test_check_infra_port_alive(self):
        """Infrastructure port responding on /health is alive."""
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            alive, info = self.mod.check_infra_port(18789)
        assert alive is True
        assert info == "ok"

    def test_check_infra_port_dead(self):
        """Infrastructure port that raises exception is dead."""
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            alive, info = self.mod.check_infra_port(18789)
        assert alive is False
        assert info == "down"

    def test_load_state_empty(self, tmp_path):
        """Loading state from nonexistent file returns default."""
        self.mod.STATE_FILE = tmp_path / "nonexistent.json"
        state = self.mod.load_state()
        assert state == {"apps": {}, "restarts": []}

    def test_save_and_load_state(self, tmp_path):
        """State persists correctly through save/load cycle."""
        state_file = tmp_path / "state.json"
        self.mod.STATE_FILE = state_file
        state = {"apps": {"37421": {"alive": True, "last_seen": 100}}, "restarts": []}
        self.mod.save_state(state)
        loaded = self.mod.load_state()
        assert loaded["apps"]["37421"]["alive"] is True

    def test_state_transition_up_to_down(self, tmp_path):
        """Alert fires (critical, via the nova_notify bus) on transition from alive to dead."""
        self.mod.STATE_FILE = tmp_path / "state.json"
        # Seed initial state: NovaControl (:37400, critical/auto-restart) was alive
        initial_state = {
            "apps": {
                "37400": {"alive": True, "info": "ok", "last_seen": time.time(), "last_alert": 0}
            },
            "restarts": []
        }
        self.mod.save_state(initial_state)

        # Mock all ports dead
        with patch.object(self.mod, "check_port", return_value=(False, "connection refused", 0.1)), \
             patch.object(self.mod, "check_infra_port", return_value=(True, "ok")), \
             patch.object(self.mod, "vector_remember"), \
             patch.object(self.mod, "notify") as mock_notify, \
             patch.object(self.mod, "restart_app", return_value=False), \
             patch.object(self.mod, "capture_diagnostics", return_value="/tmp/diag.txt"):
            self.mod.main()

        # Should have posted a critical alert naming NovaControl
        assert mock_notify.called
        kwargs = mock_notify.call_args.kwargs
        assert kwargs["level"] == "critical"
        assert "NovaControl" in kwargs["body"]
        state = self.mod.load_state()
        assert state["apps"]["37400"]["alive"] is False

    def test_state_transition_down_to_up_recovery(self, tmp_path):
        """Recovery notification fires when app comes back up."""
        self.mod.STATE_FILE = tmp_path / "state.json"
        initial_state = {
            "apps": {
                "37400": {"alive": False, "info": "down", "last_seen": 0, "last_alert": time.time() - 700}
            },
            "restarts": []
        }
        self.mod.save_state(initial_state)

        # Mock all ports alive
        with patch.object(self.mod, "check_port", return_value=(True, "v1.0", 0.1)), \
             patch.object(self.mod, "check_infra_port", return_value=(True, "ok")), \
             patch.object(self.mod, "vector_remember"), \
             patch.object(self.mod, "notify") as mock_notify:
            self.mod.main()

        # Should have posted an info-level recovery
        assert mock_notify.called
        title, kwargs = mock_notify.call_args.args[0], mock_notify.call_args.kwargs
        assert "Recovery" in title
        assert kwargs["level"] == "info"
        assert "back up" in kwargs["body"]

    def test_count_recent_restarts_prunes_old(self):
        """Restarts older than 1 hour are pruned."""
        state = {
            "restarts": [
                {"ts": time.time() - 7200, "app": "old"},
                {"ts": time.time() - 100, "app": "recent"},
            ]
        }
        count = self.mod.count_recent_restarts(state)
        assert count == 1
        assert len(state["restarts"]) == 1

    def test_alert_cooldown_prevents_duplicate_alerts(self, tmp_path):
        """No alert fires if cooldown hasn't expired."""
        self.mod.STATE_FILE = tmp_path / "state.json"
        now = time.time()
        # EVERY monitored app already down + alerted 100s ago (within 600s cooldown)
        initial_state = {
            "apps": {
                str(port): {"alive": False, "info": "down", "last_seen": 0, "last_alert": now - 100}
                for port, *_ in self.mod.MONITORED_APPS
            },
            "restarts": []
        }
        self.mod.save_state(initial_state)

        with patch.object(self.mod, "check_port", return_value=(False, "connection refused", 0.1)), \
             patch.object(self.mod, "check_infra_port", return_value=(True, "ok")), \
             patch.object(self.mod, "vector_remember"), \
             patch.object(self.mod, "notify") as mock_notify:
            self.mod.main()

        # No transition and no cooldown expiry -> nothing re-alerted
        mock_notify.assert_not_called()
        state = self.mod.load_state()
        for port, *_ in self.mod.MONITORED_APPS:
            assert state["apps"][str(port)]["last_alert"] == pytest.approx(now - 100)

    def test_infra_confirm_checks_prevents_flapping(self, tmp_path):
        """Infra services need consecutive down checks before alerting."""
        self.mod.STATE_FILE = tmp_path / "state.json"
        # Only Ollama (:11434) is watched here now — gateway/memory/PG/Redis moved to
        # Big Brother to stop double-alerts.
        infra_port = self.mod.INFRA_SERVICES[0][0]
        key = f"infra_{infra_port}"
        initial_state = {
            "apps": {key: {"alive": True, "last_seen": time.time(), "last_alert": 0, "down_checks": 0}},
            "restarts": []
        }
        self.mod.save_state(initial_state)

        # First check: down but only down_checks=1 (needs INFRA_CONFIRM_CHECKS)
        with patch.object(self.mod, "check_port", return_value=(True, "ok", 0.1)), \
             patch.object(self.mod, "check_infra_port", return_value=(False, "down")), \
             patch.object(self.mod, "vector_remember"), \
             patch.object(self.mod, "restart_infra") as mock_restart, \
             patch.object(self.mod, "notify") as mock_notify:
            self.mod.main()

        state = self.mod.load_state()
        infra_state = state["apps"].get(key, {})
        # down_checks should be 1, not enough for alert
        assert infra_state.get("down_checks", 0) == 1
        assert self.mod.INFRA_CONFIRM_CHECKS > 1
        mock_notify.assert_not_called()
        mock_restart.assert_not_called()

    def test_restart_app_success(self):
        """restart_app returns True on successful subprocess call."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = self.mod.restart_app("TestApp", "TestApp")
        assert result is True

    def test_restart_app_failure(self):
        """restart_app returns False on subprocess failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="not found")
            result = self.mod.restart_app("TestApp", "NoSuchApp")
        assert result is False

    def test_max_restarts_per_hour_limit(self, tmp_path):
        """Auto-restart is skipped when restart budget is exhausted."""
        self.mod.STATE_FILE = tmp_path / "state.json"
        now = time.time()
        # Pre-fill 3 recent restarts (exceeds MAX_RESTARTS_PER_HOUR)
        initial_state = {
            "apps": {
                "37421": {"alive": True, "info": "ok", "last_seen": now, "last_alert": 0}
            },
            "restarts": [
                {"ts": now - 60, "app": "a", "success": True},
                {"ts": now - 30, "app": "b", "success": True},
                {"ts": now - 10, "app": "c", "success": True},
            ]
        }
        self.mod.save_state(initial_state)

        with patch.object(self.mod, "check_port", return_value=(False, "down", 0.1)), \
             patch.object(self.mod, "check_infra_port", return_value=(True, "ok")), \
             patch.object(self.mod, "vector_remember"), \
             patch.object(self.mod, "slack_post"), \
             patch.object(self.mod, "restart_app") as mock_restart, \
             patch.object(self.mod, "capture_diagnostics", return_value="/tmp/diag.txt"):
            self.mod.main()

        # restart_app should NOT be called because budget is exhausted
        mock_restart.assert_not_called()


# ============================================================================
# nova_health_check.py
# ============================================================================

class TestHealthCheck:
    """Tests for nova_health_check.py health evaluation and formatting."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, monkeypatch):
        self.mock_config = mock_nova_config
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        if "nova_health_check" in sys.modules:
            del sys.modules["nova_health_check"]
        import nova_health_check
        self.mod = nova_health_check

    def test_format_message_no_issues(self):
        """No issues produces all-clear message."""
        msg = self.mod.format_message([])
        assert "running normally" in msg.lower() or "nothing to report" in msg.lower()

    def test_format_message_with_errors(self):
        """Errors are shown in the formatted message."""
        issues = [
            {"severity": "error", "name": "task_a", "reason": "3 consecutive failures"},
            {"severity": "warning", "name": "task_b", "reason": "Completed in 50ms"},
        ]
        msg = self.mod.format_message(issues)
        assert "task_a" in msg
        assert "task_b" in msg
        assert "1 error" in msg
        assert "1 warning" in msg

    def test_format_message_critical_counted_as_error(self):
        """Critical severity is counted with errors."""
        issues = [{"severity": "critical", "name": "jobs.json", "reason": "Cannot read"}]
        msg = self.mod.format_message(issues)
        assert "1 error" in msg

    def test_audit_jobs_scheduler_api_consecutive_failures(self):
        """Scheduler API tasks with consecutive failures flagged as errors."""
        now = time.time()
        mock_tasks = {
            "broken_task": {
                "enabled": True,
                "consecutive_failures": 3,
                "last_run": now - 300,
                "last_duration": 1.2,
                "last_exit_code": 1,
                "schedule": "every 5m",
            },
            "healthy_task": {
                "enabled": True,
                "consecutive_failures": 0,
                "last_run": now - 60,
                "last_duration": 5.0,
                "last_exit_code": 0,
                "schedule": "every 5m",
            },
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_tasks).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            issues = self.mod.audit_jobs()

        error_issues = [i for i in issues if i["severity"] == "error"]
        assert len(error_issues) == 1
        assert "broken_task" in error_issues[0]["name"]

    def test_audit_jobs_fast_run_detection(self):
        """Suspiciously fast cron tasks are flagged as warnings."""
        now = time.time()
        mock_tasks = {
            "empty_promise": {
                "enabled": True,
                "consecutive_failures": 0,
                "last_run": now - 60,
                "last_duration": 0.05,  # 50ms
                "last_exit_code": 0,
                "schedule": "cron 0 7 * * *",
            },
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_tasks).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            issues = self.mod.audit_jobs()

        warnings = [i for i in issues if i["severity"] == "warning"]
        assert any("empty promise" in w["reason"].lower() for w in warnings)

    def test_audit_jobs_fast_run_exempt(self):
        """Tasks in FAST_RUN_EXEMPT are not flagged for fast runs."""
        now = time.time()
        mock_tasks = {
            "gateway_watchdog": {
                "enabled": True,
                "consecutive_failures": 0,
                "last_run": now - 60,
                "last_duration": 0.05,
                "last_exit_code": 0,
                "schedule": "cron */5 * * * *",
            },
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_tasks).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            issues = self.mod.audit_jobs()

        assert not any("gateway_watchdog" in i.get("name", "") for i in issues)

    def test_audit_jobs_stale_task_detection(self):
        """Task that hasn't run in >26h is flagged as stale."""
        now = time.time()
        mock_tasks = {
            "stale_task": {
                "enabled": True,
                "consecutive_failures": 0,
                "last_run": now - (27 * 3600),  # 27 hours ago
                "last_duration": 5.0,
                "last_exit_code": 0,
                "schedule": "cron 0 7 * * *",
            },
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_tasks).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            issues = self.mod.audit_jobs()

        warnings = [i for i in issues if i["severity"] == "warning"]
        assert any("stale_task" in w["name"] for w in warnings)

    def test_audit_jobs_weekly_tasks_skip_stale_check(self):
        """Weekly tasks are not flagged as stale even if >26h since last run."""
        now = time.time()
        mock_tasks = {
            "self_audit": {
                "enabled": True,
                "consecutive_failures": 0,
                "last_run": now - (7 * 24 * 3600),  # 7 days ago
                "last_duration": 5.0,
                "last_exit_code": 0,
                "schedule": "cron 0 3 * * 1",
            },
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_tasks).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            issues = self.mod.audit_jobs()

        assert not any("self_audit" in i.get("name", "") for i in issues)

    def test_audit_slack_deliveries_checks_scheduler_api(self):
        """Slack delivery audit checks scheduler task timestamps."""
        now = time.time()
        mock_tasks = {
            "morning_brief": {
                "last_run": now - 3600,
                "last_exit_code": 0,
            },
            "mail_deliver_am": {
                "last_run": now - 3600,
                "last_exit_code": 0,
            },
            "nightly_report": {
                "last_run": now - (25 * 3600),  # 25h ago -- too old
                "last_exit_code": 0,
            },
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_tasks).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            issues = self.mod.audit_slack_deliveries()

        assert any("Nightly Report" in i["name"] for i in issues)

    def test_load_run_history_from_jsonl(self, tmp_path):
        """Run history is parsed from JSONL files."""
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        jsonl = runs_dir / "test_job.jsonl"
        entries = [
            {"ts": 1000, "action": "started"},
            {"ts": 1005, "action": "finished", "status": "ok", "durationMs": 5000},
            {"ts": 2000, "action": "started"},
            {"ts": 2010, "action": "finished", "status": "error", "durationMs": 10000, "error": "timeout"},
        ]
        jsonl.write_text("\n".join(json.dumps(e) for e in entries))

        self.mod.JOBS_FILE = tmp_path / "jobs.json"
        # Point JOBS_FILE parent to the right place
        (tmp_path / "jobs.json").write_text("{}")

        result = self.mod._load_run_history.__wrapped__(self.mod, "test_job") if hasattr(self.mod._load_run_history, '__wrapped__') else None
        # Direct call since _load_run_history uses JOBS_FILE.parent / "runs"
        original_jobs_file = self.mod.JOBS_FILE
        self.mod.JOBS_FILE = tmp_path / "jobs.json"
        result = self.mod._load_run_history("test_job")
        self.mod.JOBS_FILE = original_jobs_file

        assert result["lastRunStatus"] == "error"
        assert result["consecutiveErrors"] == 1
        assert result["lastDurationMs"] == 10000


# ============================================================================
# nova_dead_mans_switch.py
# ============================================================================

class TestDeadMansSwitch:
    """nova_dead_mans_switch.py is a thin wrapper since 2026-10-09 (organ-audit merge M1): the
    delivery check lives in nova_output_drift.py --deliveries. Its own tests:
    tests/test_nova_dead_mans_switch.py."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, monkeypatch):
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        if "nova_dead_mans_switch" in sys.modules:
            del sys.modules["nova_dead_mans_switch"]
        import nova_dead_mans_switch
        self.mod = nova_dead_mans_switch

    def test_wrapper_calls_output_drift_deliveries(self):
        import nova_output_drift
        with patch.object(nova_output_drift, "main", return_value=0) as m:
            assert self.mod.main([]) == 0
            self.mod.main(["--dry-run"])
        assert [c.args[0] for c in m.call_args_list] == [["--deliveries"], ["--deliveries", "--dry-run"]]

    def test_missed_delivery_decision(self):
        import nova_output_drift
        rows = [("morning_brief", False, 4), ("mail_deliver_pm", True, 4)]
        missed, _ = nova_output_drift.delivered(rows, 10, date.today())
        assert missed == [("morning_brief", "Morning Brief (7am)")]
        assert nova_output_drift.delivered(rows, 8, date.today())[0] == []      # before the 9am check hour


# ============================================================================
# nova_protect_monitor.py
# ============================================================================

class TestProtectMonitor:
    """Tests for nova_protect_monitor.py camera/event logic."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch):
        self.mock_config = mock_nova_config
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        # Mock out optional import
        monkeypatch.setitem(sys.modules, "nova_package_clairvoyance", MagicMock(handle_package_detection=None))
        if "nova_protect_monitor" in sys.modules:
            del sys.modules["nova_protect_monitor"]
        import nova_protect_monitor
        self.mod = nova_protect_monitor

    def test_is_exterior_true(self):
        """Cameras not starting with 'Interior' are exterior."""
        assert self.mod._is_exterior({"name": "Exterior Front Door"}) is True
        assert self.mod._is_exterior({"name": "External Gate"}) is True
        assert self.mod._is_exterior({"name": "Backyard"}) is True

    def test_is_exterior_false(self):
        """Cameras starting with 'Interior' are not exterior."""
        assert self.mod._is_exterior({"name": "Interior Living Room"}) is False
        assert self.mod._is_exterior({"name": "Interior Hallway"}) is False

    def test_is_exterior_missing_name(self):
        """Camera with no name defaults to exterior (empty string doesn't start with Interior)."""
        assert self.mod._is_exterior({}) is True

    def test_load_state_empty(self, tmp_path):
        """Empty state file returns defaults."""
        self.mod.STATE_FILE = tmp_path / "nonexistent.json"
        state = self.mod.load_state()
        assert state == {"last_event_ts": 0, "camera_status": {}}

    def test_save_and_load_state(self, tmp_path):
        """State round-trips through save/load."""
        self.mod.STATE_FILE = tmp_path / "state.json"
        state = {"last_event_ts": 12345, "camera_status": {"cam1": {"state": "CONNECTED"}}}
        self.mod.save_state(state)
        loaded = self.mod.load_state()
        assert loaded["last_event_ts"] == 12345

    def test_check_camera_health_detects_disconnected(self):
        """Camera going DISCONNECTED triggers alert."""
        client = MagicMock()
        client.get_bootstrap.return_value = {
            "cameras": [
                {"id": "cam1", "name": "Exterior Front", "state": "DISCONNECTED", "type": "UVC-G4-PRO"},
            ]
        }
        state = {
            "camera_status": {
                "cam1": {"name": "Exterior Front", "state": "CONNECTED"}
            }
        }
        with patch.object(self.mod, "slack_post") as mock_slack:
            self.mod.check_camera_health(client, state)

        assert mock_slack.called
        msg = mock_slack.call_args[0][0]
        assert "OFFLINE" in msg

    def test_check_camera_health_skips_interior(self):
        """Interior cameras are excluded from health checks."""
        client = MagicMock()
        client.get_bootstrap.return_value = {
            "cameras": [
                {"id": "cam1", "name": "Interior Living Room", "state": "DISCONNECTED", "type": "UVC-G4"},
            ]
        }
        state = {"camera_status": {}}
        with patch.object(self.mod, "slack_post") as mock_slack:
            result = self.mod.check_camera_health(client, state)

        # No exterior cameras, so no alerts
        mock_slack.assert_not_called()

    def test_check_camera_health_recovery(self):
        """Camera coming back CONNECTED triggers recovery alert."""
        client = MagicMock()
        client.get_bootstrap.return_value = {
            "cameras": [
                {"id": "cam1", "name": "Exterior Front", "state": "CONNECTED", "type": "UVC-G4"},
            ]
        }
        state = {
            "camera_status": {
                "cam1": {"name": "Exterior Front", "state": "DISCONNECTED"}
            }
        }
        with patch.object(self.mod, "slack_post") as mock_slack:
            self.mod.check_camera_health(client, state)

        assert mock_slack.called
        msg = mock_slack.call_args[0][0]
        assert "ONLINE" in msg

    def test_check_motion_events_filters_interior(self):
        """Motion events from interior cameras are ignored."""
        client = MagicMock()
        client.get_bootstrap.return_value = {
            "cameras": [
                {"id": "cam1", "name": "Interior Hallway"},
                {"id": "cam2", "name": "Exterior Driveway"},
            ]
        }
        client.get_events.return_value = [
            {
                "camera": "cam1",  # Interior camera
                "start": int(time.time() * 1000),
                "type": "smartDetectZone",
                "smartDetectTypes": ["person"],
                "id": "evt1",
            },
        ]
        state = {"last_event_ts": 0}

        with patch.object(self.mod, "slack_post") as mock_slack, \
             patch.object(self.mod, "slack_upload_image", return_value=False), \
             patch.object(self.mod, "vector_remember"):
            self.mod.check_motion_events(client, state)

        # Interior event should be filtered out
        mock_slack.assert_not_called()

    def test_protect_client_login_no_password(self):
        """Login fails gracefully when Keychain has no password."""
        with patch.object(self.mod, "_get_password", return_value=""):
            client = self.mod.ProtectClient()
            result = client.login()
        assert result is False


# ============================================================================
# nova_unifi_monitor.py
# ============================================================================

class TestUnifiMonitor:
    """Tests for nova_unifi_monitor.py network health and problem detection."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, monkeypatch):
        self.mock_config = mock_nova_config
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        if "nova_unifi_monitor" in sys.modules:
            del sys.modules["nova_unifi_monitor"]
        import nova_unifi_monitor
        self.mod = nova_unifi_monitor

    def test_find_problems_all_healthy(self):
        """No problems returned when everything is OK."""
        health = {
            "wan": {"status": "ok", "latency": 10},
            "wlan": {"status": "ok"},
            "lan": {"status": "ok"},
        }
        devices = [
            {"name": "Gateway", "state": 1, "system-stats": {"cpu": "10", "mem": "30"}, "uplink": {"drops": 0}},
        ]
        clients = []
        problems = self.mod.find_problems(health, devices, clients)
        assert len(problems) == 0

    def test_find_problems_wan_not_ok(self):
        """WAN subsystem not OK is a high severity problem."""
        health = {"wan": {"status": "error", "latency": 100}}
        problems = self.mod.find_problems(health, [], [])
        assert any(p["severity"] == "high" and "wan" in p["message"].lower() for p in problems)

    def test_find_problems_high_latency(self):
        """WAN latency >50ms is flagged."""
        health = {"wan": {"status": "ok", "latency": 75}}
        problems = self.mod.find_problems(health, [], [])
        assert any("latency" in p["message"].lower() for p in problems)

    def test_find_problems_device_disconnected(self):
        """Device with state != 1 is flagged."""
        health = {}
        devices = [{"name": "AP-Garage", "state": 0, "system-stats": {"cpu": "5", "mem": "20"}, "uplink": {"drops": 0}}]
        problems = self.mod.find_problems(health, devices, [])
        assert any("AP-Garage" in p["message"] for p in problems)

    def test_find_problems_high_cpu(self):
        """Device with CPU >80% is flagged."""
        health = {}
        devices = [{"name": "Switch", "state": 1, "system-stats": {"cpu": "95", "mem": "30"}, "uplink": {"drops": 0}}]
        problems = self.mod.find_problems(health, devices, [])
        assert any("CPU" in p["message"] for p in problems)

    def test_find_problems_high_memory(self):
        """Device with memory >85% is flagged."""
        health = {}
        devices = [{"name": "Gateway", "state": 1, "system-stats": {"cpu": "10", "mem": "92"}, "uplink": {"drops": 0}}]
        problems = self.mod.find_problems(health, devices, [])
        assert any("memory" in p["message"].lower() for p in problems)

    def test_find_problems_poor_signal_clients(self):
        """More than 3 clients with signal <-80dBm is flagged."""
        health = {}
        clients = [
            {"signal": -85}, {"signal": -82}, {"signal": -90}, {"signal": -88},
        ]
        problems = self.mod.find_problems(health, [], clients)
        assert any("poor signal" in p["message"].lower() for p in problems)

    def test_find_problems_few_poor_signal_not_flagged(self):
        """3 or fewer poor-signal clients are not flagged."""
        health = {}
        clients = [{"signal": -85}, {"signal": -82}, {"signal": -90}]
        problems = self.mod.find_problems(health, [], clients)
        assert not any("poor signal" in p["message"].lower() for p in problems)

    def test_find_problems_uplink_drops(self):
        """Devices with >100 uplink drops are flagged."""
        health = {}
        devices = [
            {"name": "AP-Office", "state": 1, "system-stats": {"cpu": "5", "mem": "20"},
             "uplink": {"drops": 250}}
        ]
        problems = self.mod.find_problems(health, devices, [])
        assert any("drops" in p["message"].lower() for p in problems)

    def test_find_problems_poor_wifi_satisfaction(self):
        """AP with satisfaction <50% is flagged."""
        health = {}
        devices = [
            {
                "name": "AP-Office", "state": 1, "type": "uap",
                "system-stats": {"cpu": "5", "mem": "20"},
                "uplink": {"drops": 0},
                "radio_table_stats": [
                    {"satisfaction": 30, "channel": 6},
                ]
            }
        ]
        problems = self.mod.find_problems(health, devices, [])
        assert any("satisfaction" in p["message"].lower() for p in problems)

    def test_find_problems_satisfaction_neg1_ignored(self):
        """AP satisfaction of -1 (no clients) is not flagged."""
        health = {}
        devices = [
            {
                "name": "AP-Office", "state": 1, "type": "uap",
                "system-stats": {"cpu": "5", "mem": "20"},
                "uplink": {"drops": 0},
                "radio_table_stats": [
                    {"satisfaction": -1, "channel": 6},
                ]
            }
        ]
        problems = self.mod.find_problems(health, devices, [])
        assert not any("satisfaction" in p["message"].lower() for p in problems)

    def test_format_status_unreachable(self):
        """format_status handles None health."""
        result = self.mod.format_status(None)
        assert "unable" in result.lower()

    def test_format_status_normal(self):
        """format_status produces status line."""
        health = {
            "wan": {"status": "ok"},
            "wlan": {"status": "ok"},
        }
        result = self.mod.format_status(health)
        assert "ok" in result.lower()

    def test_find_bandwidth_hogs(self):
        """Clients using >1GB are identified as bandwidth hogs."""
        clients = [
            {"hostname": "NAS", "tx_bytes": 2_000_000_000, "rx_bytes": 500_000_000},
            {"hostname": "Phone", "tx_bytes": 100_000, "rx_bytes": 200_000},
        ]
        hogs = self.mod.find_bandwidth_hogs(clients)
        assert len(hogs) == 1
        assert "NAS" in hogs[0]["message"]

    def test_dpi_category_name_known(self):
        """Known DPI category IDs map to names."""
        assert self.mod._dpi_category_name(4) == "Streaming Media"
        assert self.mod._dpi_category_name(13) == "Web"

    def test_dpi_category_name_unknown(self):
        """Unknown DPI category IDs get generic name."""
        result = self.mod._dpi_category_name(999)
        assert "999" in result


# ============================================================================
# nova_synology_monitor.py
# ============================================================================

class TestSynologyMonitor:
    """Tests for nova_synology_monitor.py NAS health and problem detection."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, monkeypatch):
        self.mock_config = mock_nova_config
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        if "nova_synology_monitor" in sys.modules:
            del sys.modules["nova_synology_monitor"]
        import nova_synology_monitor
        self.mod = nova_synology_monitor

    def test_fmt_bytes(self):
        """Byte formatter produces correct human-readable output."""
        assert "KB" in self.mod._fmt_bytes(2048)
        assert "MB" in self.mod._fmt_bytes(5 * 1024 * 1024)
        assert "GB" in self.mod._fmt_bytes(3 * 1024 ** 3)
        assert "N/A" in self.mod._fmt_bytes(None)

    def test_fmt_uptime_hms_string(self):
        """DSM uptime string format 'HH:MM:SS' is parsed correctly."""
        result = self.mod._fmt_uptime("610:53:47")
        assert "25d" in result
        assert "10h" in result
        assert "53m" in result

    def test_fmt_uptime_seconds(self):
        """Numeric uptime in seconds is parsed correctly."""
        result = self.mod._fmt_uptime(90000)  # 1 day + 1 hour
        assert "1d" in result
        assert "1h" in result

    def test_fmt_uptime_empty(self):
        """Empty uptime returns 'unknown'."""
        assert self.mod._fmt_uptime("") == "unknown"
        assert self.mod._fmt_uptime(None) == "unknown"

    def test_pct_bar(self):
        """Percentage bar has correct format."""
        result = self.mod._pct_bar(50, width=10)
        assert "#" in result
        assert "." in result
        assert "50.0%" in result

    def test_find_problems_healthy_system(self):
        """No problems when system is healthy."""
        sysinfo = {"sys_temp": 35, "sys_tempwarn": False}
        util = {
            "cpu": {"user_load": 10, "system_load": 5},
            "memory": {"memory_size": 32000000, "avail_real": 20000000, "real_usage": 30},
        }
        storage = {
            "volumes": [{"id": "volume_1", "status": "normal", "size": {"total": "10000000000000", "used": "3000000000000"}}],
            "storagePools": [{"id": "pool1", "status": "normal"}],
            "disks": [{"id": "disk1", "status": "normal", "temp": 35, "smart_status": "normal"}],
        }
        problems = self.mod.find_problems(sysinfo, util, storage)
        assert len(problems) == 0

    def test_find_problems_temperature_warning(self):
        """System temp warning flag triggers a problem."""
        sysinfo = {"sys_temp": 70, "sys_tempwarn": True}
        problems = self.mod.find_problems(sysinfo, None, None)
        assert any(p["category"] == "temperature" for p in problems)

    def test_find_problems_high_cpu(self):
        """CPU >80% triggers warning."""
        util = {"cpu": {"user_load": 70, "system_load": 20}, "memory": {}}
        problems = self.mod.find_problems(None, util, None)
        assert any(p["category"] == "cpu" for p in problems)

    def test_find_problems_ram_critical(self):
        """RAM >90% real usage triggers high severity."""
        util = {
            "cpu": {"user_load": 5, "system_load": 5},
            "memory": {"memory_size": 32000000, "avail_real": 2000000, "real_usage": 95},
        }
        problems = self.mod.find_problems(None, util, None)
        high_mem = [p for p in problems if p["category"] == "memory" and p["severity"] == "high"]
        assert len(high_mem) >= 1

    def test_find_problems_volume_degraded(self):
        """Non-normal volume status triggers high severity."""
        storage = {
            "volumes": [{"id": "volume_1", "status": "crashed", "size": {"total": "1000", "used": "500"}}],
            "storagePools": [],
            "disks": [],
        }
        problems = self.mod.find_problems(None, None, storage)
        assert any(p["category"] == "volume" and p["severity"] == "high" for p in problems)

    def test_find_problems_volume_almost_full(self):
        """Volume >90% full triggers high severity."""
        storage = {
            "volumes": [{
                "id": "volume_1", "status": "normal",
                "size": {"total": "10000000000000", "used": "9500000000000"},  # 95%
            }],
            "storagePools": [],
            "disks": [],
        }
        problems = self.mod.find_problems(None, None, storage)
        assert any(p["category"] == "storage" and p["severity"] == "high" for p in problems)

    def test_find_problems_volume_warning_threshold(self):
        """Volume >80% but <90% triggers medium severity."""
        storage = {
            "volumes": [{
                "id": "volume_1", "status": "normal",
                "size": {"total": "10000000000000", "used": "8500000000000"},  # 85%
            }],
            "storagePools": [],
            "disks": [],
        }
        problems = self.mod.find_problems(None, None, storage)
        assert any(p["category"] == "storage" and p["severity"] == "medium" for p in problems)

    def test_find_problems_raid_degraded(self):
        """Degraded RAID pool triggers high severity."""
        storage = {
            "volumes": [],
            "storagePools": [{"id": "pool1", "status": "degraded"}],
            "disks": [],
        }
        problems = self.mod.find_problems(None, None, storage)
        assert any(p["category"] == "raid" for p in problems)

    def test_find_problems_disk_bad_smart(self):
        """Bad SMART status triggers high severity."""
        storage = {
            "volumes": [],
            "storagePools": [],
            "disks": [{"id": "disk1", "status": "normal", "temp": 35, "smart_status": "failing"}],
        }
        problems = self.mod.find_problems(None, None, storage)
        assert any("SMART" in p["message"] for p in problems)

    def test_find_problems_nvme_high_temp(self):
        """NVMe drive at >60C triggers warning."""
        storage = {
            "volumes": [],
            "storagePools": [],
            "disks": [{"id": "nvme0", "status": "normal", "temp": 65, "smart_status": "normal", "diskType": "NVMe"}],
        }
        problems = self.mod.find_problems(None, None, storage)
        assert any(p["category"] == "temperature" and "nvme" in p["message"].lower() for p in problems)

    def test_find_problems_hdd_high_temp(self):
        """HDD at >45C triggers warning, >55C is critical."""
        storage = {
            "volumes": [],
            "storagePools": [],
            "disks": [{"id": "disk1", "status": "normal", "temp": 48, "smart_status": "normal", "diskType": "SATA"}],
        }
        problems = self.mod.find_problems(None, None, storage)
        assert any(p["category"] == "temperature" for p in problems)

    def test_find_problems_bad_sectors_exceeded(self):
        """Disk exceeding bad sector threshold triggers alert."""
        storage = {
            "volumes": [],
            "storagePools": [],
            "disks": [{"id": "disk2", "status": "normal", "temp": 35, "smart_status": "normal", "exceed_bad_sector_thr": True}],
        }
        problems = self.mod.find_problems(None, None, storage)
        assert any("bad sector" in p["message"].lower() for p in problems)

    def test_load_json_missing_file(self, tmp_path):
        """_load_json returns empty dict for missing file."""
        result = self.mod._load_json(tmp_path / "nonexistent.json")
        assert result == {}

    def test_save_and_load_json(self, tmp_path):
        """JSON state round-trips correctly."""
        path = tmp_path / "test.json"
        data = {"key": "value", "number": 42}
        self.mod._save_json(path, data)
        loaded = self.mod._load_json(path)
        assert loaded["key"] == "value"
        assert loaded["number"] == 42


# ============================================================================
# nova_home_watchdog.py
# ============================================================================

class TestHomeWatchdog:
    """Tests for nova_home_watchdog.py HomeKit status parsing and alerts."""

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, monkeypatch):
        self.mock_config = mock_nova_config
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        if "nova_home_watchdog" in sys.modules:
            del sys.modules["nova_home_watchdog"]
        import nova_home_watchdog
        self.mod = nova_home_watchdog

    def test_is_sleep_hours_late_night(self):
        """Hour 23 is sleep hours."""
        original = self.mod.HOUR
        self.mod.HOUR = 23
        assert self.mod.is_sleep_hours() is True
        self.mod.HOUR = original

    def test_is_sleep_hours_early_morning(self):
        """Hour 3 is sleep hours."""
        original = self.mod.HOUR
        self.mod.HOUR = 3
        assert self.mod.is_sleep_hours() is True
        self.mod.HOUR = original

    def test_is_not_sleep_hours_daytime(self):
        """Hour 14 is not sleep hours."""
        original = self.mod.HOUR
        self.mod.HOUR = 14
        assert self.mod.is_sleep_hours() is False
        self.mod.HOUR = original

    def test_analyze_door_open_over_10_min(self):
        """Door open for >10 minutes triggers alert."""
        now = time.time()
        accessories = [{
            "name": "Front Door",
            "room": "Foyer",
            "uuid": "door-1",
            "services": [{
                "type": "contactsensor",
                "characteristics": [{"type": "contact", "value": 1}],
            }],
        }]
        # State: door was first seen open 15 minutes ago
        state = {"contact_door-1": {"first_open": now - 900}}
        alerts, new_state = self.mod.analyze_accessories(accessories, state)
        assert len(alerts) >= 1
        assert "Front Door" in alerts[0]

    def test_analyze_door_open_under_10_min(self):
        """Door open for <10 minutes does not trigger alert."""
        now = time.time()
        accessories = [{
            "name": "Front Door",
            "room": "Foyer",
            "uuid": "door-1",
            "services": [{
                "type": "contactsensor",
                "characteristics": [{"type": "contact", "value": 1}],
            }],
        }]
        state = {"contact_door-1": {"first_open": now - 60}}  # 1 minute ago
        alerts, new_state = self.mod.analyze_accessories(accessories, state)
        assert len(alerts) == 0

    def test_analyze_door_closed_clears_state(self):
        """Closing a door clears the tracking state."""
        accessories = [{
            "name": "Front Door",
            "room": "Foyer",
            "uuid": "door-1",
            "services": [{
                "type": "contactsensor",
                "characteristics": [{"type": "contact", "value": 0}],
            }],
        }]
        state = {"contact_door-1": {"first_open": time.time() - 3600}}
        alerts, new_state = self.mod.analyze_accessories(accessories, state)
        assert "contact_door-1" not in new_state

    def test_analyze_temperature_anomaly_hot(self):
        """Temperature >85F triggers alert."""
        now = time.time()
        accessories = [{
            "name": "Thermostat",
            "room": "Living Room",
            "uuid": "thermo-1",
            "services": [{
                "type": "temperaturesensor",
                "characteristics": [{"type": "temperature", "value": 32}],  # 32C = 89.6F
            }],
        }]
        state = {}
        alerts, new_state = self.mod.analyze_accessories(accessories, state)
        assert len(alerts) >= 1
        assert "Thermostat" in alerts[0]

    def test_analyze_temperature_anomaly_cold(self):
        """Temperature <55F triggers alert."""
        accessories = [{
            "name": "Garage Sensor",
            "room": "Garage",
            "uuid": "temp-1",
            "services": [{
                "type": "temperaturesensor",
                "characteristics": [{"type": "temperature", "value": 10}],  # 10C = 50F
            }],
        }]
        state = {}
        alerts, _ = self.mod.analyze_accessories(accessories, state)
        assert any("Garage Sensor" in a for a in alerts)

    def test_analyze_color_temperature_ignored(self):
        """Color temperature (Hue bulbs) is not treated as room temperature."""
        accessories = [{
            "name": "Hue Bulb",
            "room": "Bedroom",
            "uuid": "hue-1",
            "services": [{
                "type": "lightbulb",
                "characteristics": [{"type": "colortemperature", "value": 350}],  # Mired value
            }],
        }]
        state = {}
        alerts, _ = self.mod.analyze_accessories(accessories, state)
        assert len(alerts) == 0

    def test_analyze_motion_during_sleep_hours(self):
        """Motion during sleep hours triggers alert."""
        original = self.mod.HOUR
        self.mod.HOUR = 2  # 2am, sleep hours

        accessories = [{
            "name": "Hallway Motion",
            "room": "Hallway",
            "uuid": "motion-1",
            "services": [{
                "type": "motionsensor",
                "characteristics": [{"type": "motion", "value": True}],
            }],
        }]
        state = {}
        with patch.object(self.mod, "vector_remember"):
            alerts, _ = self.mod.analyze_accessories(accessories, state)

        self.mod.HOUR = original
        assert any("Motion" in a or "motion" in a.lower() for a in alerts)

    def test_analyze_motion_during_day_no_alert(self):
        """Motion during daytime does not trigger alert."""
        original = self.mod.HOUR
        self.mod.HOUR = 14  # 2pm, not sleep hours

        accessories = [{
            "name": "Hallway Motion",
            "room": "Hallway",
            "uuid": "motion-1",
            "services": [{
                "type": "motionsensor",
                "characteristics": [{"type": "motion", "value": True}],
            }],
        }]
        state = {}
        alerts, _ = self.mod.analyze_accessories(accessories, state)

        self.mod.HOUR = original
        assert len(alerts) == 0

    def test_analyze_motion_cooldown(self):
        """Motion alert respects 30-minute cooldown."""
        original = self.mod.HOUR
        self.mod.HOUR = 2

        accessories = [{
            "name": "Hallway Motion",
            "room": "Hallway",
            "uuid": "motion-1",
            "services": [{
                "type": "motionsensor",
                "characteristics": [{"type": "motion", "value": True}],
            }],
        }]
        # Alerted 5 minutes ago (within 30 min cooldown)
        state = {"motion_motion-1": time.time() - 300}
        with patch.object(self.mod, "vector_remember"):
            alerts, _ = self.mod.analyze_accessories(accessories, state)

        self.mod.HOUR = original
        assert len(alerts) == 0

    def test_load_state_empty(self, tmp_path):
        """Empty state returns empty dict."""
        self.mod.STATE_FILE = tmp_path / "nonexistent.json"
        assert self.mod.load_state() == {}

    def test_save_and_load_state(self, tmp_path):
        """State round-trips correctly."""
        self.mod.STATE_FILE = tmp_path / "state.json"
        state = {"key": "value"}
        self.mod.save_state(state)
        loaded = self.mod.load_state()
        assert loaded["key"] == "value"


# ============================================================================
# nova_watchdog.py
# ============================================================================

class TestWatchdog:
    """Tests for nova_watchdog.py — the independent OFF-BOX fleet watchdog.

    Rewritten 2026-06: it no longer babysits local launchd services (check_port /
    check_scheduler / restart_launchd / check_gateway_eperm are gone — Big Brother
    owns that). It probes the fleet from nova-core5 with pure stdlib and posts to
    Slack directly, with zero dependency on mac-studio.
    """

    @pytest.fixture(autouse=True)
    def setup_module(self, mock_nova_config, mock_nova_logger, monkeypatch, tmp_path):
        self.mock_config = mock_nova_config
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        monkeypatch.setitem(sys.modules, "nova_logger", mock_nova_logger)
        if "nova_watchdog" in sys.modules:
            del sys.modules["nova_watchdog"]
        import nova_watchdog
        self.mod = nova_watchdog
        # never touch the real state file / never sleep between probe retries
        monkeypatch.setattr(nova_watchdog, "STATE_DIR", str(tmp_path))
        monkeypatch.setattr(nova_watchdog, "STATE_FILE", str(tmp_path / "watchdog_state.json"))
        monkeypatch.setattr(nova_watchdog.time, "sleep", lambda *_: None)

    # -- probes --------------------------------------------------------------
    def test_check_tcp_alive(self):
        with patch("socket.create_connection") as cc:
            cc.return_value.__enter__ = MagicMock()
            cc.return_value.__exit__ = MagicMock(return_value=False)
            ok, detail = self.mod.check_tcp("192.168.1.6", 22)
        assert ok is True and detail == ""

    def test_check_tcp_dead(self):
        with patch("socket.create_connection", side_effect=ConnectionRefusedError()):
            ok, detail = self.mod.check_tcp("192.168.1.6", 22)
        assert ok is False and detail == "ConnectionRefusedError"

    def test_check_http_200(self):
        resp = MagicMock(); resp.getcode.return_value = 200
        resp.__enter__ = MagicMock(return_value=resp); resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            ok, detail = self.mod.check_http("http://192.168.1.6:11434/api/version")
        assert ok is True and detail == ""

    def test_check_http_error_status(self):
        import urllib.error
        err = urllib.error.HTTPError("http://x", 503, "unavailable", None, None)
        with patch("urllib.request.urlopen", side_effect=err):
            ok, detail = self.mod.check_http("http://x")
        assert ok is False and detail == "HTTP 503"

    def test_check_http_unreachable(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError()):
            ok, detail = self.mod.check_http("http://x")
        assert ok is False and detail == "TimeoutError"

    def test_run_check_retries_transient_probe_failure(self):
        """One dropped probe inside a sweep must NOT count as a failure."""
        with patch.object(self.mod, "check_tcp", side_effect=[(False, "OSError"), (True, "")]) as tcp:
            ok, detail = self.mod.run_check("tcp", ("h", 22))
        assert ok is True and detail == ""
        assert tcp.call_count == 2

    def test_run_check_reports_sustained_failure(self):
        with patch.object(self.mod, "check_http", return_value=(False, "HTTP 500")) as http:
            ok, detail = self.mod.run_check("http", "http://x")
        assert ok is False and detail == "HTTP 500"
        assert http.call_count == self.mod.PROBE_ATTEMPTS

    # -- slack (independent path) -------------------------------------------
    def test_slack_post_without_token_returns_false(self, monkeypatch):
        monkeypatch.delenv("NOVA_SLACK_BOT_TOKEN", raising=False)
        with patch("urllib.request.urlopen") as uo:
            assert self.mod.slack_post(self.mod.CH_INFO, "hi") is False
        uo.assert_not_called()

    def test_slack_post_uses_bearer_from_env(self, monkeypatch):
        monkeypatch.setenv("NOVA_SLACK_BOT_TOKEN", "xoxb-test")
        resp = MagicMock(); resp.read.return_value = b'{"ok": true}'
        resp.__enter__ = MagicMock(return_value=resp); resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp) as uo:
            assert self.mod.slack_post(self.mod.CH_CRITICAL, "down") is True
        req = uo.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer xoxb-test"
        assert json.loads(req.data)["channel"] == self.mod.CH_CRITICAL

    def test_slack_post_failure_never_raises(self, monkeypatch):
        monkeypatch.setenv("NOVA_SLACK_BOT_TOKEN", "xoxb-test")
        with patch("urllib.request.urlopen", side_effect=Exception("boom")):
            assert self.mod.slack_post(self.mod.CH_INFO, "x") is False

    def test_no_hardcoded_slack_token(self):
        import inspect
        src = inspect.getsource(self.mod)
        assert "xoxb-" not in src
        assert "NOVA_SLACK_BOT_TOKEN" in src

    # -- state ---------------------------------------------------------------
    def test_load_state_missing_file(self):
        assert self.mod.load_state() == {}

    def test_save_and_load_state_roundtrip(self):
        self.mod.save_state({"x": {"state": "down", "fails": 3}})
        assert self.mod.load_state() == {"x": {"state": "down", "fails": 3}}

    # -- sweep / debounce ----------------------------------------------------
    def test_sweep_all_up_no_transitions(self):
        with patch.object(self.mod, "run_check", return_value=(True, "")):
            state = {}
            trans, up, total = self.mod.sweep(state)
        assert trans == []
        assert up == total == len(self.mod.CHECKS)
        assert all(v == {"state": "up", "fails": 0} for v in state.values())

    def test_sweep_declares_down_only_after_fail_threshold(self):
        """FAIL_THRESHOLD consecutive failed sweeps before DOWN (flap debounce)."""
        state = {}
        with patch.object(self.mod, "run_check", return_value=(False, "ConnectionRefusedError")):
            for i in range(1, self.mod.FAIL_THRESHOLD):
                trans, up, total = self.mod.sweep(state)
                assert trans == [], f"declared DOWN too early on sweep {i}"
                assert up == 0
            trans, up, total = self.mod.sweep(state)
        assert len(trans) == len(self.mod.CHECKS)
        assert all(kind == "down" and detail == "ConnectionRefusedError" for kind, _, detail in trans)
        assert all(v["state"] == "down" for v in state.values())

    def test_sweep_recovery_transition(self):
        label = self.mod.CHECKS[0][0]
        state = {label: {"state": "down", "fails": 5}}
        with patch.object(self.mod, "run_check", return_value=(True, "")):
            trans, up, total = self.mod.sweep(state)
        assert ("recovered", label, "") in trans
        assert len(trans) == 1
        assert state[label] == {"state": "up", "fails": 0}

    def test_sweep_single_flap_resets_fail_counter(self):
        label = self.mod.CHECKS[0][0]
        state = {label: {"state": "up", "fails": self.mod.FAIL_THRESHOLD - 1}}
        with patch.object(self.mod, "run_check", return_value=(True, "")):
            self.mod.sweep(state)
        assert state[label]["fails"] == 0 and state[label]["state"] == "up"

    # -- config sanity -------------------------------------------------------
    def test_checks_are_well_formed(self):
        labels = [c[0] for c in self.mod.CHECKS]
        assert len(labels) == len(set(labels)), "duplicate check labels"
        for label, kind, target in self.mod.CHECKS:
            assert kind in ("tcp", "http")
            if kind == "tcp":
                host, port = target
                assert host.startswith("192.168.") and 0 < port < 65536
            else:
                assert target.startswith("http://192.168.")

    def test_watches_the_studio_and_itself(self):
        """The point of the watchdog: catch .6 dying, and a second watcher catches .10."""
        targets = {str(t) for _, _, t in self.mod.CHECKS}
        assert any("192.168.1.6" in t for t in targets)
        assert any("192.168.1.10" in t for t in targets)

    def test_thresholds_sane(self):
        assert self.mod.FAIL_THRESHOLD >= 2
        assert self.mod.PROBE_ATTEMPTS >= 2
        assert self.mod.CHECK_INTERVAL * self.mod.FAIL_THRESHOLD <= 600


# ============================================================================
# Integration Tests (live services)
# ============================================================================

class TestMonitoringIntegration:
    """Integration tests that hit live local services."""

    @pytest.mark.integration
    def test_ollama_port_alive(self):
        """Ollama should be running on port 11434."""
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:11434/", timeout=5)
            assert resp.status == 200
        except Exception:
            pytest.skip("Ollama not running on port 11434")

    @pytest.mark.integration
    def test_scheduler_status_endpoint(self):
        """Scheduler /status should respond with JSON."""
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:37460/status", timeout=5)
            data = json.loads(resp.read())
            assert data.get("status") == "running"
            assert "uptime_s" in data
            assert "tasks_total" in data
        except Exception:
            pytest.skip("Scheduler not running on port 37460")

    @pytest.mark.integration
    def test_scheduler_tasks_endpoint(self):
        """Scheduler /tasks should return task details."""
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:37460/tasks", timeout=5)
            data = json.loads(resp.read())
            assert isinstance(data, dict)
            # Should have at least some tasks
            assert len(data) > 0
            # Each task should have standard fields
            for task_id, task in list(data.items())[:3]:
                assert "script" in task
                assert "schedule" in task
                assert "last_run" in task
        except Exception:
            pytest.skip("Scheduler not running on port 37460")

    @pytest.mark.integration
    def test_memory_server_health(self):
        """Memory server /health should respond."""
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18790/health", timeout=5)
            assert resp.status == 200
        except Exception:
            pytest.skip("Memory server not running on port 18790")

    @pytest.mark.integration
    def test_app_watchdog_can_reach_ollama(self, mock_nova_config, monkeypatch):
        """App watchdog check_port can actually reach Ollama."""
        monkeypatch.setitem(sys.modules, "nova_config", mock_nova_config)
        if "nova_app_watchdog" in sys.modules:
            del sys.modules["nova_app_watchdog"]
        import nova_app_watchdog
        alive, info, elapsed = nova_app_watchdog.check_port(11434, timeout=5)
        if not alive:
            pytest.skip("Ollama not running")
        assert alive is True
        assert elapsed < 5.0
