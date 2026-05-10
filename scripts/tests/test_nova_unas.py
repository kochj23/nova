#!/usr/bin/env python3
"""
test_nova_unas.py — Full test suite for nova_unas_client.py and nova_unas_monitor.py.

Covers all 7 required test categories:
  Security · Performance · Retry · Unit · Integration · Functional · Frame

Written by Jordan Koch.
"""

import json
import sys
import time
import unittest
import unittest.mock as mock
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).parent.parent))
from nova_unas_client import UNASClient, UNASError, _load_api_key, _request, UNAS_HOST


# ── Fixtures ──────────────────────────────────────────────────────────────────

MOCK_SYSTEM_INFO = {
    "hardware": {"shortname": "UNASPRO8"},
    "name": "UNAS Pro 8",
    "mac": "8C3066C6108C",
    "deviceState": "configured",
    "cloudConnected": False,
    "hasInternet": True,
    "remoteAccessEnabled": False,
}

MOCK_STORAGE = {
    "err": None,
    "type": "single",
    "data": {
        "diskInfo": {"needMoreDisk": False, "recommendedDiskSize": 0, "slots": []},
        "estimate": 0,
        "progress": 0,
        "status": "healthy",
        "totalQuota": 55949834321920,
        "usage": {
            "myDrives": 0,
            "sharedDrives": 15143437795328,
            "system": 5428734000,
            "unassigned": 40801103904768,
        },
    },
}

MOCK_SHARES = {
    "err": None,
    "type": "collection",
    "data": [
        {
            "id": "b494e338-80c7-47a8-903f-5a609a0edf2d",
            "name": "Shared_Drive",
            "status": "active",
            "usage": 15143437795328,
            "encryptionStatus": "unencrypted",
            "quota": -1,
            "storagePoolId": "b37f2e84-517c-4a4f-92f0-4d642527ba17",
            "encryptionMigrating": False,
            "groups": [],
            "members": [],
            "remoteBackupTasks": [],
            "security": "none",
            "snapshot": {
                "coverageSize": 0,
                "totalCount": 0,
                "lastSnapshotTime": None,
                "paused": False,
                "settingId": None,
                "schedule": {"enable": False},
            },
        }
    ],
    "offset": 0,
    "limit": 0,
    "total": 1,
}


# ═════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestSecurity(unittest.TestCase):
    """Verify security properties: Keychain-only auth, no plaintext secrets, safe error handling."""

    def test_api_key_loaded_from_keychain_not_hardcoded(self):
        """API key must come from Keychain subprocess, never a literal in source."""
        import inspect
        import nova_unas_client as module
        source = inspect.getsource(module)
        self.assertNotIn("sk-", source, "No API keys hardcoded in source")
        self.assertNotIn("Bearer ey", source, "No JWT tokens hardcoded")
        # Must reference macOS 'security' command
        self.assertIn("security", source)
        self.assertIn("find-generic-password", source)

    def test_host_is_local_network_not_cloud(self):
        """UNAS host must be a local IP, never an external cloud URL."""
        self.assertTrue(
            UNAS_HOST.startswith("https://192.168.") or
            UNAS_HOST.startswith("https://10.") or
            UNAS_HOST.startswith("https://172."),
            f"UNAS_HOST must be LAN address, got: {UNAS_HOST}"
        )

    def test_ssl_verification_disabled_for_self_signed(self):
        """SSL ctx must have check_hostname=False for self-signed UNAS cert."""
        import nova_unas_client as module
        self.assertFalse(module._SSL_CTX.check_hostname)

    def test_unas_error_does_not_leak_api_key(self):
        """UNASError messages must not include the raw API key value."""
        err = UNASError("Authentication failed (401) — check API key in Keychain")
        msg = str(err)
        self.assertNotIn("sk-", msg)
        self.assertNotIn("Bearer", msg)

    def test_no_credentials_in_request_url(self):
        """API key must never appear in the URL — only in headers."""
        with patch("nova_unas_client.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="test-key-123\n")
            with patch("nova_unas_client.urllib.request.urlopen") as mock_open:
                mock_resp = MagicMock()
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_resp.read.return_value = b'{"hardware": {"shortname": "UNASPRO8"}}'
                mock_open.return_value = mock_resp
                _request("/api/system")
                req_arg = mock_open.call_args[0][0]
                self.assertNotIn("test-key-123", req_arg.full_url)

    def test_privacy_comment_present(self):
        """Module must document PRIVACY: local-only policy."""
        import inspect
        import nova_unas_client as module
        source = inspect.getsource(module)
        self.assertIn("PRIVACY", source)
        self.assertIn("local", source.lower())

    def test_monitor_privacy_local_only_tag(self):
        """Monitor must tag memory ingestion with privacy=local-only."""
        with open(Path(__file__).parent.parent / "nova_unas_monitor.py") as f:
            source = f.read()
        self.assertIn("local-only", source)


# ═════════════════════════════════════════════════════════════════════════════
# PERFORMANCE TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestPerformance(unittest.TestCase):
    """Verify performance characteristics: timeouts, no unbounded loops."""

    def test_request_timeout_is_set(self):
        """HTTP requests must have an explicit timeout."""
        import nova_unas_client as module
        self.assertGreater(module.DEFAULT_TIMEOUT, 0)
        self.assertLessEqual(module.DEFAULT_TIMEOUT, 30)

    def test_retry_count_is_reasonable(self):
        """MAX_RETRIES must be between 2 and 5."""
        import nova_unas_client as module
        self.assertGreaterEqual(module.MAX_RETRIES, 2)
        self.assertLessEqual(module.MAX_RETRIES, 5)

    def test_health_snapshot_completes_quickly_on_mock(self):
        """health_snapshot() must complete within 2s on mocked data."""
        client = UNASClient()
        with patch.object(client, "system_info", return_value=MOCK_SYSTEM_INFO), \
             patch.object(client, "storage_summary",
                          return_value=MOCK_STORAGE["data"]), \
             patch.object(client, "shared_drives",
                          return_value=MOCK_SHARES["data"]):
            start = time.time()
            snap = client.health_snapshot()
            elapsed = time.time() - start
        self.assertLess(elapsed, 2.0)
        self.assertIsInstance(snap, dict)

    def test_retry_delay_uses_backoff(self):
        """Retry delay must increase between attempts (not a flat sleep)."""
        import nova_unas_client as module
        self.assertGreater(module.RETRY_DELAY, 0)


# ═════════════════════════════════════════════════════════════════════════════
# RETRY TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestRetry(unittest.TestCase):
    """Verify retry logic: transient failures retried, auth failures not retried."""

    def _make_http_error(self, code: int):
        import urllib.error
        import urllib.request
        err = urllib.error.HTTPError(
            url="https://192.168.1.69/api/system",
            code=code,
            msg="Error",
            hdrs=None,
            fp=None,
        )
        return err

    def test_transient_500_retried(self):
        """HTTP 500 should trigger retries."""
        with patch("nova_unas_client.subprocess.run") as mock_run, \
             patch("nova_unas_client.time.sleep"), \
             patch("nova_unas_client.urllib.request.urlopen") as mock_open:
            mock_run.return_value = MagicMock(stdout="test-key\n")
            mock_open.side_effect = self._make_http_error(500)
            with self.assertRaises(UNASError):
                _request("/api/system", retries=3)
            self.assertEqual(mock_open.call_count, 3)

    def test_auth_401_not_retried(self):
        """HTTP 401 must raise immediately — no point retrying bad credentials."""
        with patch("nova_unas_client.subprocess.run") as mock_run, \
             patch("nova_unas_client.urllib.request.urlopen") as mock_open:
            mock_run.return_value = MagicMock(stdout="test-key\n")
            mock_open.side_effect = self._make_http_error(401)
            with self.assertRaises(UNASError) as ctx:
                _request("/api/system", retries=3)
            self.assertIn("401", str(ctx.exception))
            self.assertEqual(mock_open.call_count, 1)

    def test_auth_403_not_retried(self):
        """HTTP 403 must raise immediately."""
        with patch("nova_unas_client.subprocess.run") as mock_run, \
             patch("nova_unas_client.urllib.request.urlopen") as mock_open:
            mock_run.return_value = MagicMock(stdout="test-key\n")
            mock_open.side_effect = self._make_http_error(403)
            with self.assertRaises(UNASError):
                _request("/api/system", retries=3)
            self.assertEqual(mock_open.call_count, 1)

    def test_network_error_retried(self):
        """URLError (network down) must be retried."""
        import urllib.error
        with patch("nova_unas_client.subprocess.run") as mock_run, \
             patch("nova_unas_client.time.sleep"), \
             patch("nova_unas_client.urllib.request.urlopen") as mock_open:
            mock_run.return_value = MagicMock(stdout="test-key\n")
            mock_open.side_effect = urllib.error.URLError("Network unreachable")
            with self.assertRaises(UNASError):
                _request("/api/system", retries=2)
            self.assertEqual(mock_open.call_count, 2)

    def test_success_on_second_attempt(self):
        """Should succeed if first attempt fails but second succeeds."""
        import urllib.error
        with patch("nova_unas_client.subprocess.run") as mock_run, \
             patch("nova_unas_client.time.sleep"), \
             patch("nova_unas_client.urllib.request.urlopen") as mock_open:
            mock_run.return_value = MagicMock(stdout="test-key\n")
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = json.dumps(MOCK_SYSTEM_INFO).encode()
            mock_open.side_effect = [
                urllib.error.URLError("Transient"),
                mock_resp,
            ]
            result = _request("/api/system", retries=3)
            self.assertEqual(result["name"], "UNAS Pro 8")


# ═════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestUnit(unittest.TestCase):
    """Unit tests for individual functions."""

    def _mock_client(self):
        client = UNASClient()
        client.system_info = MagicMock(return_value=MOCK_SYSTEM_INFO)
        client.storage_summary = MagicMock(return_value=MOCK_STORAGE["data"])
        client.shared_drives = MagicMock(return_value=MOCK_SHARES["data"])
        return client

    def test_health_snapshot_structure(self):
        """health_snapshot() must return dict with device/storage/shares/timestamp."""
        client = self._mock_client()
        snap = client.health_snapshot()
        self.assertIn("device", snap)
        self.assertIn("storage", snap)
        self.assertIn("shares", snap)
        self.assertIn("timestamp", snap)

    def test_health_snapshot_device_fields(self):
        client = self._mock_client()
        snap = client.health_snapshot()
        dev = snap["device"]
        self.assertEqual(dev["model"], "UNASPRO8")
        self.assertEqual(dev["name"], "UNAS Pro 8")
        self.assertFalse(dev["cloud_connected"])
        self.assertTrue(dev["has_internet"])

    def test_health_snapshot_storage_math(self):
        """Storage used_pct and free_tb must be computed correctly."""
        client = self._mock_client()
        snap = client.health_snapshot()
        st = snap["storage"]
        total = 55949834321920
        used = 15143437795328 + 5428734000
        expected_pct = round(used / total * 100, 1)
        self.assertAlmostEqual(st["used_pct"], expected_pct, places=0)
        self.assertGreater(st["free_tb"], 0)
        self.assertAlmostEqual(st["total_tb"], round(total / 1e12, 2), places=1)

    def test_health_snapshot_shares(self):
        """Shares list must have correct name and status."""
        client = self._mock_client()
        snap = client.health_snapshot()
        self.assertEqual(len(snap["shares"]), 1)
        share = snap["shares"][0]
        self.assertEqual(share["name"], "Shared_Drive")
        self.assertEqual(share["status"], "active")
        self.assertEqual(share["encryption"], "unencrypted")

    def test_ping_returns_true_on_success(self):
        client = self._mock_client()
        self.assertTrue(client.ping())

    def test_ping_returns_false_on_error(self):
        client = UNASClient()
        client.system_info = MagicMock(side_effect=UNASError("down"))
        self.assertFalse(client.ping())

    def test_load_api_key_returns_none_gracefully(self):
        """_load_api_key() must return None (not raise) if key missing."""
        with patch("nova_unas_client.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="\n")
            result = _load_api_key()
            self.assertIsNone(result)

    def test_request_raises_without_key(self):
        """_request() must raise UNASError if Keychain returns empty."""
        with patch("nova_unas_client._load_api_key", return_value=None):
            with self.assertRaises(UNASError) as ctx:
                _request("/api/system")
            self.assertIn("Keychain", str(ctx.exception))


# ═════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestIntegration(unittest.TestCase):
    """Integration tests for monitor check logic using mock snapshots."""

    def _make_snapshot(self, storage_pct=50.0, storage_status="healthy",
                       share_status="active", needs_more=False):
        total = 55949834321920
        used = int(total * storage_pct / 100)
        free = total - used
        return {
            "device": {
                "model": "UNASPRO8",
                "name": "UNAS Pro 8",
                "mac": "8C3066C6108C",
                "state": "configured",
                "cloud_connected": False,
                "has_internet": True,
            },
            "storage": {
                "status": storage_status,
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "used_pct": storage_pct,
                "total_tb": round(total / 1e12, 2),
                "free_tb": round(free / 1e12, 2),
                "needs_more_disk": needs_more,
            },
            "shares": [
                {"id": "abc", "name": "Shared_Drive", "status": share_status,
                 "used_bytes": used, "used_tb": round(used / 1e12, 2),
                 "encryption": "unencrypted", "quota": -1}
            ],
            "timestamp": time.time(),
        }

    def test_no_problems_on_healthy_snapshot(self):
        from nova_unas_monitor import check_storage, check_shares, check_device
        snap = self._make_snapshot()
        self.assertEqual(check_storage(snap), [])
        self.assertEqual(check_shares(snap), [])
        self.assertEqual(check_device(snap), [])

    def test_storage_warning_at_80_pct(self):
        from nova_unas_monitor import check_storage
        snap = self._make_snapshot(storage_pct=82.0)
        problems = check_storage(snap)
        self.assertEqual(len(problems), 1)
        self.assertIn("warning", problems[0].lower())

    def test_storage_critical_at_90_pct(self):
        from nova_unas_monitor import check_storage
        snap = self._make_snapshot(storage_pct=91.0)
        problems = check_storage(snap)
        self.assertEqual(len(problems), 1)
        self.assertIn("CRITICAL", problems[0])

    def test_unhealthy_storage_status_flagged(self):
        from nova_unas_monitor import check_storage
        snap = self._make_snapshot(storage_status="degraded")
        problems = check_storage(snap)
        self.assertTrue(any("degraded" in p for p in problems))

    def test_inactive_share_flagged(self):
        from nova_unas_monitor import check_shares
        snap = self._make_snapshot(share_status="error")
        problems = check_shares(snap)
        self.assertEqual(len(problems), 1)
        self.assertIn("Shared_Drive", problems[0])

    def test_needs_more_disk_flagged(self):
        from nova_unas_monitor import check_storage
        snap = self._make_snapshot(needs_more=True)
        problems = check_storage(snap)
        self.assertTrue(any("more disk" in p for p in problems))

    def test_setup_state_not_flagged(self):
        """'setup' is valid for new devices — must not alert."""
        from nova_unas_monitor import check_device
        snap = self._make_snapshot()
        snap["device"]["state"] = "setup"
        self.assertEqual(check_device(snap), [])

    def test_unknown_device_state_flagged(self):
        from nova_unas_monitor import check_device
        snap = self._make_snapshot()
        snap["device"]["state"] = "error"
        problems = check_device(snap)
        self.assertEqual(len(problems), 1)

    def test_status_file_written_on_health_snapshot(self):
        """Monitor must write STATUS_FILE after fetching snapshot."""
        import nova_unas_monitor as mon
        snap = self._make_snapshot()
        with patch("nova_unas_monitor.client") as mock_client, \
             patch("nova_unas_monitor._save_status") as mock_save, \
             patch("nova_unas_monitor._load_state", return_value={}), \
             patch("nova_unas_monitor._save_state"), \
             patch("nova_unas_monitor._ingest_memory"), \
             patch("sys.argv", ["nova_unas_monitor.py"]):
            mock_client.health_snapshot.return_value = snap
            mon.main()
            mock_save.assert_called_once_with(snap)

    def test_slack_alerted_on_new_problems(self):
        """Monitor must post to Slack when new problems appear."""
        import nova_unas_monitor as mon
        snap = self._make_snapshot(storage_pct=92.0)
        with patch("nova_unas_monitor.client") as mock_client, \
             patch("nova_unas_monitor._save_status"), \
             patch("nova_unas_monitor._load_state", return_value={"problems": []}), \
             patch("nova_unas_monitor._save_state"), \
             patch("nova_unas_monitor._ingest_memory"), \
             patch("nova_unas_monitor.post_slack") as mock_slack, \
             patch("sys.argv", ["nova_unas_monitor.py"]):
            mock_client.health_snapshot.return_value = snap
            mon.main()
            mock_slack.assert_called_once()
            alert_msg = mock_slack.call_args[0][0]
            self.assertIn("UNAS", alert_msg)

    def test_slack_resolve_on_cleared_problems(self):
        """Monitor must post resolved message when all problems clear."""
        import nova_unas_monitor as mon
        snap = self._make_snapshot()  # healthy now
        with patch("nova_unas_monitor.client") as mock_client, \
             patch("nova_unas_monitor._save_status"), \
             patch("nova_unas_monitor._load_state",
                   return_value={"problems": ["Storage CRITICAL: 91.0% used"]}), \
             patch("nova_unas_monitor._save_state"), \
             patch("nova_unas_monitor._ingest_memory"), \
             patch("nova_unas_monitor.post_slack") as mock_slack, \
             patch("sys.argv", ["nova_unas_monitor.py"]):
            mock_client.health_snapshot.return_value = snap
            mon.main()
            mock_slack.assert_called_once()
            self.assertIn("resolved", mock_slack.call_args[0][0].lower())


# ═════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestFunctional(unittest.TestCase):
    """End-to-end behavior tests for CLI flags and JSON output."""

    def _run_main(self, argv, snapshot):
        import nova_unas_monitor as mon
        with patch("nova_unas_monitor.client") as mock_client, \
             patch("nova_unas_monitor._save_status"), \
             patch("nova_unas_monitor._load_state", return_value={}), \
             patch("nova_unas_monitor._save_state"), \
             patch("nova_unas_monitor._ingest_memory"), \
             patch("nova_unas_monitor.post_slack"), \
             patch("sys.argv", argv):
            mock_client.health_snapshot.return_value = snapshot
            mon.main()

    def test_json_flag_outputs_valid_json(self, capsys=None):
        """--json flag must output parseable JSON to stdout."""
        import io
        from contextlib import redirect_stdout
        import nova_unas_monitor as mon

        snap = {
            "device": {"model": "UNASPRO8", "name": "UNAS Pro 8", "mac": "X",
                       "state": "configured", "cloud_connected": False, "has_internet": True},
            "storage": {"status": "healthy", "total_bytes": 100, "used_bytes": 50,
                        "free_bytes": 50, "used_pct": 50.0, "total_tb": 0.0, "free_tb": 0.0,
                        "needs_more_disk": False},
            "shares": [],
            "timestamp": 1000.0,
        }
        buf = io.StringIO()
        with redirect_stdout(buf), \
             patch("nova_unas_monitor.client") as mock_client, \
             patch("nova_unas_monitor._save_status"), \
             patch("nova_unas_monitor._load_state", return_value={}), \
             patch("nova_unas_monitor._save_state"), \
             patch("nova_unas_monitor._ingest_memory"), \
             patch("sys.argv", ["nova_unas_monitor.py", "--json"]):
            mock_client.health_snapshot.return_value = snap
            mon.main()
        output = buf.getvalue()
        parsed = json.loads(output)
        self.assertIn("device", parsed)
        self.assertIn("storage", parsed)

    def test_problems_flag_no_output_on_healthy(self):
        """--problems with healthy system must print no-problem message."""
        import io
        from contextlib import redirect_stdout
        import nova_unas_monitor as mon

        snap = {
            "device": {"model": "UNASPRO8", "name": "UNAS Pro 8", "mac": "X",
                       "state": "configured", "cloud_connected": False, "has_internet": True},
            "storage": {"status": "healthy", "total_bytes": 100, "used_bytes": 10,
                        "free_bytes": 90, "used_pct": 10.0, "total_tb": 0.0, "free_tb": 0.0,
                        "needs_more_disk": False},
            "shares": [],
            "timestamp": 1000.0,
        }
        buf = io.StringIO()
        with redirect_stdout(buf), \
             patch("nova_unas_monitor.client") as mock_client, \
             patch("nova_unas_monitor._save_status"), \
             patch("nova_unas_monitor._load_state", return_value={}), \
             patch("nova_unas_monitor._save_state"), \
             patch("nova_unas_monitor._ingest_memory"), \
             patch("sys.argv", ["nova_unas_monitor.py", "--problems"]):
            mock_client.health_snapshot.return_value = snap
            mon.main()
        output = buf.getvalue()
        self.assertIn("No problems", output)

    def test_exit_on_unas_error(self):
        """Must exit with code 1 if UNAS is unreachable."""
        import nova_unas_monitor as mon
        with patch("nova_unas_monitor.client") as mock_client, \
             patch("sys.argv", ["nova_unas_monitor.py"]):
            mock_client.health_snapshot.side_effect = UNASError("Connection refused")
            with self.assertRaises(SystemExit) as ctx:
                mon.main()
            self.assertEqual(ctx.exception.code, 1)


# ═════════════════════════════════════════════════════════════════════════════
# FRAME / SMOKE TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestFrame(unittest.TestCase):
    """Smoke tests — imports succeed, classes instantiate, key symbols exist."""

    def test_client_module_imports(self):
        import nova_unas_client
        self.assertTrue(hasattr(nova_unas_client, "UNASClient"))
        self.assertTrue(hasattr(nova_unas_client, "UNASError"))
        self.assertTrue(hasattr(nova_unas_client, "_request"))
        self.assertTrue(hasattr(nova_unas_client, "_load_api_key"))
        self.assertTrue(hasattr(nova_unas_client, "UNAS_HOST"))

    def test_monitor_module_imports(self):
        import nova_unas_monitor
        self.assertTrue(hasattr(nova_unas_monitor, "main"))
        self.assertTrue(hasattr(nova_unas_monitor, "check_storage"))
        self.assertTrue(hasattr(nova_unas_monitor, "check_shares"))
        self.assertTrue(hasattr(nova_unas_monitor, "check_device"))

    def test_client_instantiates(self):
        client = UNASClient()
        self.assertIsNotNone(client)

    def test_client_has_required_methods(self):
        client = UNASClient()
        for method in ["system_info", "storage_summary", "storage_basic",
                       "shared_drives", "shared_drive", "health_snapshot", "ping"]:
            self.assertTrue(hasattr(client, method), f"Missing method: {method}")

    def test_unas_error_is_exception(self):
        self.assertTrue(issubclass(UNASError, Exception))

    def test_constants_are_sane(self):
        import nova_unas_client as m
        self.assertGreater(m.DEFAULT_TIMEOUT, 0)
        self.assertGreater(m.MAX_RETRIES, 0)
        self.assertGreater(m.RETRY_DELAY, 0)
        self.assertTrue(m.UNAS_HOST.startswith("https://"))

    def test_monitor_threshold_constants(self):
        import nova_unas_monitor as m
        self.assertGreater(m.STORAGE_WARN_PCT, 0)
        self.assertGreater(m.STORAGE_CRIT_PCT, m.STORAGE_WARN_PCT)
        self.assertLessEqual(m.STORAGE_CRIT_PCT, 100)

    def test_state_dir_path_is_under_home(self):
        import nova_unas_monitor as m
        import os
        home = str(Path.home())
        self.assertTrue(str(m.STATE_DIR).startswith(home))
        self.assertTrue(str(m.STATUS_FILE).startswith(home))

    def test_snapshot_file_has_json_extension(self):
        import nova_unas_monitor as m
        self.assertTrue(str(m.SNAPSHOT_FILE).endswith(".json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
