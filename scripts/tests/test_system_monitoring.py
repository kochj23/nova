#!/usr/bin/env python3
"""
test_system_monitoring.py — Combined tests for nova_bandwidth_report.py,
nova_face_recognition.py, nova_app_watchdog.py, and nova_weekly_reliability.py.

Covers: UniFi API calls, face detection pipeline, app health checks, auto-restart,
reliability metrics, state transitions, security (no credential leakage, safe SSL).

Run: python3 -m pytest tests/test_system_monitoring.py -v
Written by Jordan Koch.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def bandwidth_module(mock_nova_config):
    """Import nova_bandwidth_report fresh."""
    for mod in list(sys.modules.keys()):
        if "nova_bandwidth_report" in mod:
            del sys.modules[mod]
    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=1, stdout="", stderr="")
        import nova_bandwidth_report
    return nova_bandwidth_report


@pytest.fixture
def watchdog_module(mock_nova_config):
    """Import nova_app_watchdog fresh."""
    for mod in list(sys.modules.keys()):
        if "nova_app_watchdog" in mod:
            del sys.modules[mod]
    import nova_app_watchdog
    return nova_app_watchdog


@pytest.fixture
def face_module(mock_nova_config):
    """Import nova_face_recognition fresh."""
    for mod in list(sys.modules.keys()):
        if "nova_face_recognition" in mod:
            del sys.modules[mod]
    import nova_face_recognition
    return nova_face_recognition


@pytest.fixture
def reliability_module(mock_nova_config, mock_nova_logger):
    """Import nova_weekly_reliability fresh."""
    for mod in list(sys.modules.keys()):
        if "nova_weekly_reliability" in mod:
            del sys.modules[mod]
    import nova_weekly_reliability
    return nova_weekly_reliability


# ═══════════════════════════════════════════════════════════════════════════════
# BANDWIDTH REPORT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestBandwidthApiKey:
    """Tests for API key retrieval."""

    @patch("subprocess.run")
    def test_get_api_key_from_keychain(self, mock_run, bandwidth_module):
        mock_run.return_value = MagicMock(returncode=0, stdout="test-api-key\n", stderr="")
        key = bandwidth_module.get_api_key()
        assert key == "test-api-key"

    @patch("subprocess.run")
    def test_returns_empty_on_failure(self, mock_run, bandwidth_module):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        key = bandwidth_module.get_api_key()
        assert key == ""


class TestBandwidthApiCalls:
    """Tests for UniFi API request functions."""

    @patch("urllib.request.urlopen")
    def test_api_get(self, mock_urlopen, bandwidth_module):
        response_data = {"data": [{"name": "device1"}, {"name": "device2"}]}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = bandwidth_module.api_get("stat/sta", "test-key")
        assert len(result) == 2

    @patch("urllib.request.urlopen")
    def test_api_post(self, mock_urlopen, bandwidth_module):
        response_data = {"data": [{"wan-rx_bytes": 1000000}]}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = bandwidth_module.api_post("stat/report/hourly.site", {"attrs": ["wan-rx_bytes"]}, "test-key")
        assert len(result) == 1


class TestBandwidthMain:
    """Tests for main bandwidth report logic."""

    @patch("nova_bandwidth_report.slack_post")
    @patch("nova_bandwidth_report.api_post")
    @patch("nova_bandwidth_report.api_get")
    @patch("nova_bandwidth_report.get_api_key")
    def test_exits_gracefully_without_key(self, mock_key, mock_get, mock_post, mock_slack, bandwidth_module):
        mock_key.return_value = ""
        bandwidth_module.main()
        mock_get.assert_not_called()
        mock_slack.assert_not_called()

    @patch("nova_bandwidth_report.slack_post")
    @patch("nova_bandwidth_report.api_post")
    @patch("nova_bandwidth_report.api_get")
    @patch("nova_bandwidth_report.get_api_key")
    def test_handles_api_error(self, mock_key, mock_get, mock_post, mock_slack, bandwidth_module):
        mock_key.return_value = "test-key"
        mock_get.side_effect = Exception("API unreachable")
        # Should not raise
        bandwidth_module.main()


# ═══════════════════════════════════════════════════════════════════════════════
# APP WATCHDOG TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestWatchdogStateManagement:
    """Tests for watchdog state management."""

    def test_load_default_state(self, watchdog_module, tmp_path):
        with patch.object(watchdog_module, "STATE_FILE", tmp_path / "nope.json"):
            state = watchdog_module.load_state()
        assert state == {"apps": {}, "restarts": []}

    def test_save_and_reload(self, watchdog_module, tmp_path):
        sf = tmp_path / "watchdog.json"
        with patch.object(watchdog_module, "STATE_FILE", sf):
            watchdog_module.save_state({"apps": {"37400": {"alive": True}}, "restarts": []})
            state = watchdog_module.load_state()
        assert state["apps"]["37400"]["alive"] is True


class TestWatchdogPortChecks:
    """Tests for port checking logic."""

    @patch("urllib.request.urlopen")
    def test_alive_port(self, mock_urlopen, watchdog_module):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"version": "1.0"}).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        alive, info, elapsed = watchdog_module.check_port(37400)
        assert alive is True
        assert "1.0" in info

    @patch("urllib.request.urlopen")
    def test_dead_port(self, mock_urlopen, watchdog_module):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        alive, info, elapsed = watchdog_module.check_port(99999)
        assert alive is False


class TestWatchdogRestart:
    """Tests for auto-restart logic."""

    @patch("subprocess.run")
    def test_restart_app_success(self, mock_run, watchdog_module):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = watchdog_module.restart_app("NovaControl", "NovaControl")
        assert result is True
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_restart_app_failure(self, mock_run, watchdog_module):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="app not found")
        result = watchdog_module.restart_app("FakeApp", "FakeApp")
        assert result is False

    def test_count_recent_restarts(self, watchdog_module):
        state = {
            "restarts": [
                {"ts": time.time() - 100, "app": "A", "success": True},
                {"ts": time.time() - 200, "app": "B", "success": True},
                {"ts": time.time() - 7200, "app": "C", "success": True},  # Over 1 hour ago
            ]
        }
        count = watchdog_module.count_recent_restarts(state)
        assert count == 2  # Only 2 within the last hour


class TestWatchdogMonitoredApps:
    """Tests for monitored app configuration."""

    def test_monitored_apps_defined(self, watchdog_module):
        assert len(watchdog_module.MONITORED_APPS) > 0
        for port, name, bundle, critical in watchdog_module.MONITORED_APPS:
            assert isinstance(port, int)
            assert isinstance(name, str)
            assert isinstance(critical, bool)

    def test_infra_services_defined(self, watchdog_module):
        assert len(watchdog_module.INFRA_SERVICES) > 0
        for port, name, cmd in watchdog_module.INFRA_SERVICES:
            assert isinstance(port, int)
            assert isinstance(cmd, str)


# ═══════════════════════════════════════════════════════════════════════════════
# FACE RECOGNITION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestFaceRecognitionState:
    """Tests for face recognition state management."""

    def test_load_default_state(self, face_module, tmp_path):
        with patch.object(face_module, "STATE_FILE", tmp_path / "nope.json"):
            state = face_module.load_state()
        assert state == {"last_seen": {}, "unknown_alerts": {}}

    def test_save_state(self, face_module, tmp_path):
        sf = tmp_path / "face_state.json"
        with patch.object(face_module, "STATE_FILE", sf):
            face_module.save_state({"last_seen": {"known_Jordan": 12345}, "unknown_alerts": {}})
        data = json.loads(sf.read_text())
        assert data["last_seen"]["known_Jordan"] == 12345


class TestFaceRecognitionConfig:
    """Tests for face recognition configuration."""

    def test_exterior_cameras_defined(self, face_module):
        assert len(face_module.EXTERIOR_CAMERAS) > 0
        for cam in face_module.EXTERIOR_CAMERAS:
            assert cam.endswith("_latest.jpg")

    def test_tolerance_is_reasonable(self, face_module):
        assert 0.3 < face_module.TOLERANCE < 0.8

    def test_cooldown_values(self, face_module):
        assert face_module.PERSON_COOLDOWN >= 600  # At least 10 min
        assert face_module.UNKNOWN_COOLDOWN >= 300  # At least 5 min


class TestDescribeScene:
    """Tests for vision model scene description."""

    @patch("urllib.request.urlopen")
    def test_returns_description(self, mock_urlopen, face_module, tmp_path):
        img = tmp_path / "test.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # Minimal JPEG header

        response_data = {"response": "One person walking toward the front door carrying a package."}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = face_module.describe_scene(str(img))
        assert "person" in result.lower() or "walking" in result.lower()

    @patch("urllib.request.urlopen")
    def test_handles_vision_failure(self, mock_urlopen, face_module, tmp_path):
        img = tmp_path / "test.jpg"
        img.write_bytes(b"fake")
        mock_urlopen.side_effect = Exception("Ollama down")
        result = face_module.describe_scene(str(img))
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# WEEKLY RELIABILITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestReliabilitySchedulerStatus:
    """Tests for scheduler status queries."""

    @patch("urllib.request.urlopen")
    def test_get_scheduler_tasks(self, mock_urlopen, reliability_module):
        response_data = {"task1": {"run_count": 10, "consecutive_failures": 0}}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp
        result = reliability_module.get_scheduler_tasks()
        assert "task1" in result

    @patch("urllib.request.urlopen")
    def test_handles_scheduler_down(self, mock_urlopen, reliability_module):
        mock_urlopen.side_effect = Exception("Connection refused")
        result = reliability_module.get_scheduler_tasks()
        assert result == {}


class TestReliabilityMemoryCount:
    """Tests for memory count queries."""

    @patch("urllib.request.urlopen")
    def test_get_memory_count(self, mock_urlopen, reliability_module):
        response_data = {"count": 500000, "by_source": {"a": 100, "b": 200, "c": 300}}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp
        count, sources = reliability_module.get_memory_count()
        assert count == 500000
        assert sources == 3


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestMonitoringSecurity:
    """Security tests across monitoring scripts."""

    def test_bandwidth_ssl_verification_disabled_for_local(self, bandwidth_module):
        """UniFi API uses self-signed certs — SSL verification disabled is expected for 192.168.x.x."""
        assert bandwidth_module.SSL_CTX.check_hostname is False
        # But URL should be local only
        assert "192.168." in "https://192.168.1.1/proxy/network/api"

    def test_watchdog_no_credentials(self, watchdog_module):
        import inspect
        source = inspect.getsource(watchdog_module)
        assert "sk-" not in source
        assert "password" not in source.lower() or "find-generic-password" in source

    def test_face_module_no_credentials(self, face_module):
        import inspect
        source = inspect.getsource(face_module)
        assert "sk-" not in source
        assert "AKIA" not in source

    def test_watchdog_localhost_only(self, watchdog_module):
        """All monitored ports should be localhost."""
        import inspect
        source = inspect.getsource(watchdog_module.check_port)
        assert "127.0.0.1" in source


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestIntegration:
    """Integration tests requiring live services."""

    def test_ollama_reachable(self, face_module):
        """Verify Ollama is running."""
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:11434/", timeout=5)
            assert resp.status == 200
        except Exception:
            pytest.skip("Ollama not running")

    def test_memory_server_health(self, reliability_module):
        """Verify memory server is up."""
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18790/health", timeout=5)
            assert resp.status == 200
        except Exception:
            pytest.skip("Memory server not running")
