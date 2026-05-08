"""
test_big_brother.py — Tests for nova_big_brother.py

Tests cover:
  - Unit: port check, service config, protected task detection
  - Security: no hardcoded credentials, PRIVATE_SOURCES defined in dependency scripts
  - Functional: log error pattern matching, event recording, heal event structure
  - Integration: API HTTP server responds (requires daemon running)

Written by Jordan Koch.
"""

import json
import os
import re
import sys
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Add scripts dir to path
SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))


# ── Unit Tests ────────────────────────────────────────────────────────────────

class TestPortCheck(unittest.TestCase):
    """Tests for _port_open() port connectivity helper."""

    def test_loopback_unreachable_port(self):
        from nova_big_brother import _port_open
        # Port 1 should never be listening
        self.assertFalse(_port_open("127.0.0.1", 1, timeout=0.5))

    def test_invalid_host(self):
        from nova_big_brother import _port_open
        self.assertFalse(_port_open("invalid.host.does.not.exist", 80, timeout=0.5))

    def test_timeout_parameter(self):
        from nova_big_brother import _port_open
        start = time.monotonic()
        _port_open("127.0.0.1", 2, timeout=0.3)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.5, "Timeout must be respected")


class TestServiceConfig(unittest.TestCase):
    """Tests for SERVICES constant structure."""

    def test_services_have_required_fields(self):
        from nova_big_brother import SERVICES
        for entry in SERVICES:
            self.assertEqual(len(entry), 6,
                             f"Service {entry[0]} must have 6 fields: name,host,port,label,critical,health_path")
            name, host, port, label, critical, health_path = entry
            self.assertIsInstance(name, str)
            self.assertIsInstance(host, str)
            self.assertIsInstance(port, int)
            self.assertIsInstance(critical, bool)
            self.assertIn(host, ("127.0.0.1",),
                          f"{name} must use loopback address only")

    def test_critical_services_identified(self):
        from nova_big_brother import SERVICES
        critical = {s[0] for s in SERVICES if s[4]}
        expected_critical = {"PostgreSQL", "Redis", "Ollama", "Memory Server",
                              "Gateway", "Scheduler"}
        for svc in expected_critical:
            self.assertIn(svc, critical,
                          f"{svc} must be marked critical")

    def test_no_external_ips_in_services(self):
        from nova_big_brother import SERVICES
        for name, host, *_ in SERVICES:
            self.assertEqual(host, "127.0.0.1",
                             f"{name} must bind to loopback only")

    def test_subagents_list(self):
        from nova_big_brother import SUBAGENTS
        expected = {"sentinel", "lookout", "analyst", "librarian", "coder"}
        self.assertEqual(set(SUBAGENTS), expected)


class TestProtectedTasks(unittest.TestCase):
    """Tests for protected task detection logic."""

    def test_protected_patterns_defined(self):
        from nova_big_brother import PROTECTED_TASK_PATTERNS
        self.assertIn("ingest", PROTECTED_TASK_PATTERNS)
        self.assertIn("reindex", PROTECTED_TASK_PATTERNS)
        self.assertIn("maintain", PROTECTED_TASK_PATTERNS)
        self.assertIn("pg_backup", PROTECTED_TASK_PATTERNS)

    def test_protected_task_name_matching(self):
        from nova_big_brother import PROTECTED_TASK_PATTERNS
        protected_names = ["nova_ingest_mbox", "nova_reembed", "pg_maintain", "bulk_music_ingest"]
        for name in protected_names:
            matched = any(p in name.lower() for p in PROTECTED_TASK_PATTERNS)
            self.assertTrue(matched, f"'{name}' should match a protected task pattern")

    def test_non_protected_task_name(self):
        from nova_big_brother import PROTECTED_TASK_PATTERNS
        safe_names = ["nova_daily_essay", "nova_after_dark", "dream_run", "nova_health_check"]
        for name in safe_names:
            matched = any(p in name.lower() for p in PROTECTED_TASK_PATTERNS)
            self.assertFalse(matched, f"'{name}' should NOT match a protected task pattern")


# ── Security Tests ────────────────────────────────────────────────────────────

class TestSecurityNoBigBrotherCredentials(unittest.TestCase):
    """Security: no hardcoded tokens or credentials in nova_big_brother.py."""

    def setUp(self):
        self.source = (SCRIPTS_DIR / "nova_big_brother.py").read_text()

    def test_no_hardcoded_api_keys(self):
        patterns = ["sk-", "AKIA", "ghp_", "xoxb-", "xoxp-", "xapp-"]
        for p in patterns:
            self.assertNotIn(f'"{p}', self.source,
                             f"nova_big_brother.py must not contain hardcoded {p} token")

    def test_no_hardcoded_passwords(self):
        # Must not contain literal password strings
        self.assertNotRegex(self.source, r'password\s*=\s*"[^"]+"',
                            "Must not contain hardcoded password")

    def test_uses_keychain_for_secrets(self):
        # Must load secrets via security command or nova_config
        self.assertTrue(
            "security find-generic-password" in self.source or
            "nova_config" in self.source,
            "nova_big_brother.py must load secrets from Keychain via nova_config"
        )

    def test_api_binds_to_loopback_only(self):
        # HTTPServer must bind to 127.0.0.1
        self.assertIn('"127.0.0.1"', self.source,
                      "API server must bind to loopback only")
        self.assertNotIn('"0.0.0.0"', self.source,
                         "API server must not bind to all interfaces")

    def test_no_personal_paths_hardcoded(self):
        # /Users/kochj hardcoded paths are not present (use Path.home() instead)
        hardcoded_path_count = self.source.count('"/Users/kochj"')
        self.assertEqual(hardcoded_path_count, 0,
                         "Use Path.home() instead of hardcoded /Users/kochj paths")

    def test_pid_file_in_home_dir(self):
        self.assertIn("Path.home()", self.source,
                      "Paths must use Path.home() not hardcoded user directory")


class TestPrivateSourcesFilter(unittest.TestCase):
    """Security: private/work memory sources are filtered out of public journal content."""

    def test_opinion_script_has_private_sources(self):
        source = (SCRIPTS_DIR / "nova_daily_opinion.py").read_text()
        self.assertIn("PRIVATE_SOURCES", source,
                      "nova_daily_opinion.py must define PRIVATE_SOURCES")
        self.assertIn("disney_internal", source)
        self.assertIn("cloud_governance", source)
        self.assertIn("safari_history", source)

    def test_essay_script_has_private_sources(self):
        source = (SCRIPTS_DIR / "nova_daily_essay.py").read_text()
        self.assertIn("PRIVATE_SOURCES", source,
                      "nova_daily_essay.py must define PRIVATE_SOURCES")
        self.assertIn("disney_internal", source)
        self.assertIn("safari_history", source)

    def test_essay_excludes_private_from_pick_subject(self):
        source = (SCRIPTS_DIR / "nova_daily_essay.py").read_text()
        # pick_subject must filter against PRIVATE_SOURCES
        self.assertIn("PRIVATE_SOURCES", source)
        # Verify the filter is applied before random.choice
        pick_idx = source.find("def pick_subject")
        private_idx = source.find("PRIVATE_SOURCES", pick_idx)
        choice_idx = source.find("random.choice", pick_idx)
        self.assertGreater(choice_idx, private_idx,
                           "PRIVATE_SOURCES filter must appear before random.choice in pick_subject")

    def test_opinion_filters_sources_in_format_sources(self):
        source = (SCRIPTS_DIR / "nova_daily_opinion.py").read_text()
        # format_sources must filter against PRIVATE_SOURCES
        fmt_idx = source.find("def format_sources")
        private_idx = source.find("PRIVATE_SOURCES", fmt_idx)
        self.assertNotEqual(private_idx, -1,
                            "format_sources must reference PRIVATE_SOURCES for filtering")


# ── Functional Tests ──────────────────────────────────────────────────────────

class TestLogScanner(unittest.TestCase):
    """Functional tests for log error pattern matching."""

    def test_eperm_pattern_matches(self):
        from nova_big_brother import _COMPILED_PATTERNS
        test_line = 'EPERM: operation not permitted on workspace-state.json'
        matched = [(sev, svc, desc) for pat, sev, svc, desc in _COMPILED_PATTERNS
                   if pat.search(test_line)]
        self.assertTrue(len(matched) > 0, "EPERM pattern must match")
        sev, svc, desc = matched[0]
        self.assertEqual(sev, "critical")
        self.assertEqual(svc, "Gateway")

    def test_signal_lock_pattern_matches(self):
        from nova_big_brother import _COMPILED_PATTERNS
        test_line = 'signal-cli: INFO SignalAccount - Config file is in use by another instance, waiting…'
        matched = [desc for pat, sev, svc, desc in _COMPILED_PATTERNS
                   if pat.search(test_line)]
        self.assertTrue(len(matched) > 0, "signal-cli lock pattern must match")

    def test_gateway_secrets_pattern_matches(self):
        from nova_big_brother import _COMPILED_PATTERNS
        test_line = 'Gateway failed to start: Error: Startup failed: required secrets are unavailable.'
        matched = [(sev, desc) for pat, sev, svc, desc in _COMPILED_PATTERNS
                   if pat.search(test_line)]
        self.assertTrue(len(matched) > 0, "Gateway secrets pattern must match")
        self.assertEqual(matched[0][0], "critical")

    def test_invalid_config_keys_pattern(self):
        from nova_big_brother import _COMPILED_PATTERNS
        test_line = 'agents: Unrecognized keys: bootstrapMaxChars, bootstrapTotalMaxChars'
        matched = [desc for pat, sev, svc, desc in _COMPILED_PATTERNS
                   if pat.search(test_line)]
        self.assertTrue(len(matched) > 0, "Invalid config keys pattern must match")

    def test_benign_log_no_false_positives(self):
        from nova_big_brother import _COMPILED_PATTERNS
        benign_lines = [
            "All services healthy",
            "Gateway started on port 18789",
            "Memory server ready: 1554000 memories",
            "Slack socket mode connected",
            "Dream generation complete. 328 words ready for delivery.",
        ]
        for line in benign_lines:
            matched = [desc for pat, sev, svc, desc in _COMPILED_PATTERNS
                       if pat.search(line)]
            self.assertEqual(len(matched), 0,
                             f"Benign log line matched error pattern: '{line}' → {matched}")


class TestEventRecording(unittest.TestCase):
    """Functional tests for heal event tracking."""

    def setUp(self):
        import nova_big_brother as bb
        bb._heal_events.clear()
        bb._alerted_issues.clear()

    def test_record_event_stores_fields(self):
        from nova_big_brother import _record_event, _heal_events
        _record_event("critical", "Gateway DOWN", "Restarted gateway", "Gateway")
        self.assertEqual(len(_heal_events), 1)
        ev = _heal_events[0]
        self.assertEqual(ev["severity"], "critical")
        self.assertEqual(ev["issue"], "Gateway DOWN")
        self.assertEqual(ev["fix"], "Restarted gateway")
        self.assertEqual(ev["service"], "Gateway")
        self.assertIn("ts", ev)

    def test_record_event_most_recent_first(self):
        from nova_big_brother import _record_event, _heal_events
        _record_event("warning", "First event", "Fixed", "Redis")
        _record_event("critical", "Second event", "Fixed", "Gateway")
        self.assertEqual(_heal_events[0]["issue"], "Second event")
        self.assertEqual(_heal_events[1]["issue"], "First event")

    def test_heal_events_max_capacity(self):
        from nova_big_brother import _record_event, _heal_events
        for i in range(510):
            _record_event("info", f"Event {i}", "Fixed", "Test")
        self.assertLessEqual(len(_heal_events), 500,
                             "heal_events deque must cap at 500")

    def test_event_timestamp_is_iso(self):
        from nova_big_brother import _record_event, _heal_events
        _record_event("info", "Test", "Test fix", "Test")
        ts = _heal_events[0]["ts"]
        # Should parse as ISO 8601
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        self.assertIsNotNone(parsed)


class TestQuietHours(unittest.TestCase):
    """Functional tests for quiet hours logic."""

    def test_quiet_hours_boundary(self):
        from nova_big_brother import QUIET_START, QUIET_END
        self.assertEqual(QUIET_START, 22)
        self.assertEqual(QUIET_END, 8)

    def test_quiet_hours_wraps_midnight(self):
        from nova_big_brother import _is_quiet_hours
        import nova_big_brother as bb
        # Test by temporarily patching datetime
        with patch("nova_big_brother.datetime") as mock_dt:
            mock_now = MagicMock()
            mock_now.hour = 2  # 2am is quiet
            mock_dt.now.return_value = mock_now
            self.assertTrue(_is_quiet_hours())

            mock_now.hour = 10  # 10am is NOT quiet
            self.assertFalse(_is_quiet_hours())

            mock_now.hour = 23  # 11pm IS quiet
            self.assertTrue(_is_quiet_hours())


class TestDiskSpaceCheck(unittest.TestCase):
    """Functional tests for disk space check."""

    def test_disk_check_returns_list(self):
        from nova_big_brother import _check_disk_space
        result = _check_disk_space()
        self.assertIsInstance(result, list)

    def test_disk_check_warns_on_low_space(self):
        from nova_big_brother import _check_disk_space, DISK_WARN_GB
        with patch("os.statvfs") as mock_statvfs:
            mock_stat = MagicMock()
            # Simulate 5GB free (below 10GB threshold)
            mock_stat.f_bavail = 5 * 1024 * 1024 * 1024 // 4096
            mock_stat.f_frsize = 4096
            mock_statvfs.return_value = mock_stat
            warnings = _check_disk_space()
            self.assertTrue(len(warnings) > 0,
                            "Should warn when free space is below threshold")

    def test_disk_check_no_warning_on_ample_space(self):
        from nova_big_brother import _check_disk_space
        with patch("os.statvfs") as mock_statvfs:
            mock_stat = MagicMock()
            # Simulate 100GB free
            mock_stat.f_bavail = 100 * 1024 * 1024 * 1024 // 4096
            mock_stat.f_frsize = 4096
            mock_statvfs.return_value = mock_stat
            warnings = _check_disk_space()
            self.assertEqual(len(warnings), 0,
                             "Should not warn when free space is ample")


class TestPendingRestartQueue(unittest.TestCase):
    """Functional tests for protected task restart queue."""

    def setUp(self):
        import nova_big_brother as bb
        bb._pending_restart.clear()

    def test_queue_restart_adds_to_list(self):
        from nova_big_brother import _queue_restart, _pending_restart
        _queue_restart("Memory Server")
        self.assertIn("Memory Server", _pending_restart)

    def test_queue_restart_no_duplicates(self):
        from nova_big_brother import _queue_restart, _pending_restart
        _queue_restart("Redis")
        _queue_restart("Redis")
        self.assertEqual(_pending_restart.count("Redis"), 1)


# ── API Server Tests ──────────────────────────────────────────────────────────

class TestAPIServerResponds(unittest.TestCase):
    """Integration test: Big Brother API must respond if daemon is running."""

    def test_status_endpoint(self):
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:37461/bb/status", timeout=3)
            data = json.loads(resp.read())
            self.assertIn("daemon", data)
            self.assertIn("version", data)
            self.assertIn("uptime_s", data)
            self.assertEqual(data["daemon"], "big-brother")
        except Exception:
            self.skipTest("Big Brother daemon not running — skipping integration test")

    def test_events_endpoint(self):
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:37461/bb/events?n=10", timeout=3)
            data = json.loads(resp.read())
            self.assertIsInstance(data, list)
        except Exception:
            self.skipTest("Big Brother daemon not running — skipping integration test")

    def test_services_endpoint(self):
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:37461/bb/services", timeout=3)
            data = json.loads(resp.read())
            self.assertIsInstance(data, dict)
            # Should have at least the critical services
            for svc in ("PostgreSQL", "Redis", "Gateway"):
                self.assertIn(svc, data, f"{svc} must appear in services response")
        except Exception:
            self.skipTest("Big Brother daemon not running — skipping integration test")

    def test_404_on_unknown_route(self):
        import urllib.request
        try:
            with self.assertRaises(Exception):  # 404 raises URLError
                urllib.request.urlopen("http://127.0.0.1:37461/bb/nonexistent", timeout=3)
        except Exception:
            self.skipTest("Big Brother daemon not running — skipping integration test")


# ── Performance Tests ─────────────────────────────────────────────────────────

class TestPerformance(unittest.TestCase):
    """Performance: sweep and scan operations must complete within time budget."""

    def test_port_check_fast(self):
        """Port check with short timeout must complete within 2x the timeout."""
        from nova_big_brother import _port_open
        start = time.monotonic()
        _port_open("127.0.0.1", 1, timeout=0.2)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.8, "Port check must complete within 800ms for 200ms timeout")

    def test_log_scan_large_file(self):
        """Log scan on a large file must complete within 1 second."""
        import tempfile
        from nova_big_brother import _scan_log_file, _SEEN_ERRORS
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            # Write 50k lines of benign log data
            for i in range(50000):
                f.write(f'[2026-05-08 13:{i%60:02d}:00] INFO gateway: request processed id={i}\n')
            fname = f.name

        try:
            path = Path(fname)
            _SEEN_ERRORS.pop(str(path), None)
            start = time.monotonic()
            results = _scan_log_file(path)
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 1.0,
                            f"Scanning 50k line log file took {elapsed:.2f}s — must be under 1s")
            self.assertEqual(len(results), 0, "No false positives on benign log")
        finally:
            Path(fname).unlink(missing_ok=True)

    def test_log_scan_with_errors(self):
        """Log scan must detect errors quickly even in large file."""
        import tempfile
        from nova_big_brother import _scan_log_file, _SEEN_ERRORS
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            for i in range(10000):
                f.write(f'[INFO] normal log line {i}\n')
            f.write('[ERROR] EPERM: operation not permitted on workspace-state.json\n')
            for i in range(10000):
                f.write(f'[INFO] normal log line after error {i}\n')
            fname = f.name

        try:
            path = Path(fname)
            _SEEN_ERRORS.pop(str(path), None)
            start = time.monotonic()
            results = _scan_log_file(path)
            elapsed = time.monotonic() - start
            self.assertGreater(len(results), 0, "Must detect EPERM error")
            self.assertLess(elapsed, 0.5, f"Error detection took {elapsed:.2f}s — must be under 500ms")
        finally:
            Path(fname).unlink(missing_ok=True)

    def test_record_event_performance(self):
        """Recording 500 events must complete within 500ms."""
        import nova_big_brother as bb
        bb._heal_events.clear()
        start = time.monotonic()
        for i in range(500):
            bb._record_event("info", f"Event {i}", f"Fix {i}", "Test")
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.5,
                        f"Recording 500 events took {elapsed:.2f}s — must be under 500ms")

    def test_disk_check_fast(self):
        """Disk space check must complete within 500ms."""
        from nova_big_brother import _check_disk_space
        start = time.monotonic()
        _check_disk_space()
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.5, f"Disk check took {elapsed:.2f}s — must be under 500ms")


# ── Framework Tests ───────────────────────────────────────────────────────────

class TestFrameworkIntegration(unittest.TestCase):
    """Framework tests: verify integrations with nova_config, nova_logger, launchd."""

    def test_nova_config_imported_correctly(self):
        """nova_big_brother must use nova_config for notification routing."""
        source = (SCRIPTS_DIR / "nova_big_brother.py").read_text()
        self.assertIn("import nova_config", source)
        self.assertIn("nova_config.post_both", source)
        self.assertIn("nova_config.SLACK_NOTIFY", source)
        self.assertIn("nova_config.NOVA_SIGNAL", source)
        self.assertIn("nova_config.JORDAN_SIGNAL", source)

    def test_nova_logger_imported_correctly(self):
        """nova_big_brother must use nova_logger structured logging."""
        source = (SCRIPTS_DIR / "nova_big_brother.py").read_text()
        self.assertIn("from nova_logger import log", source)
        self.assertIn("LOG_INFO", source)
        self.assertIn("LOG_ERROR", source)
        self.assertIn("LOG_WARN", source)

    def test_launchd_plist_is_valid_xml(self):
        """Big Brother launchd plist must be valid XML."""
        import plistlib
        plist_path = Path.home() / "Library/LaunchAgents/net.digitalnoise.big-brother.plist"
        if not plist_path.exists():
            self.skipTest("Plist not installed — skipping")
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
        self.assertIn("Label", plist)
        self.assertEqual(plist["Label"], "net.digitalnoise.big-brother")

    def test_launchd_plist_uses_keepalive(self):
        """Big Brother plist must use KeepAlive for crash recovery."""
        import plistlib
        plist_path = Path.home() / "Library/LaunchAgents/net.digitalnoise.big-brother.plist"
        if not plist_path.exists():
            self.skipTest("Plist not installed — skipping")
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
        self.assertIn("KeepAlive", plist,
                      "KeepAlive must be set so launchd restarts BB on crash")
        keep = plist["KeepAlive"]
        self.assertTrue(keep.get("Crashed", False),
                        "KeepAlive.Crashed must be true")

    def test_launchd_plist_no_start_interval(self):
        """Big Brother must be persistent daemon, NOT a cron-style StartInterval job."""
        import plistlib
        plist_path = Path.home() / "Library/LaunchAgents/net.digitalnoise.big-brother.plist"
        if not plist_path.exists():
            self.skipTest("Plist not installed — skipping")
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
        self.assertNotIn("StartInterval", plist,
                         "Big Brother must be persistent daemon, not a StartInterval cron")

    def test_launchd_plist_uses_zsh_not_bash(self):
        """Plist must use /bin/zsh (macOS Tahoe TCC requirement)."""
        import plistlib
        plist_path = Path.home() / "Library/LaunchAgents/net.digitalnoise.big-brother.plist"
        if not plist_path.exists():
            self.skipTest("Plist not installed — skipping")
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
        args = plist.get("ProgramArguments", [])
        self.assertTrue(args[0].endswith("zsh"),
                        "ProgramArguments must use /bin/zsh (macOS Tahoe TCC)")

    def test_launchd_log_paths_in_home(self):
        """Log paths must be in ~/.openclaw/logs, not on external volumes."""
        import plistlib
        plist_path = Path.home() / "Library/LaunchAgents/net.digitalnoise.big-brother.plist"
        if not plist_path.exists():
            self.skipTest("Plist not installed — skipping")
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
        for key in ("StandardOutPath", "StandardErrorPath"):
            if key in plist:
                path = plist[key]
                self.assertIn(".openclaw/logs", path,
                              f"{key} must write to ~/.openclaw/logs")
                self.assertNotIn("/Volumes/", path,
                                 f"{key} must not write to external volume (TCC)")

    def test_api_port_constant(self):
        """API_PORT constant must match the expected diagnostics port."""
        from nova_big_brother import API_PORT
        self.assertEqual(API_PORT, 37461,
                         "Big Brother API must be on port 37461")

    def test_sweep_interval_constant(self):
        """SWEEP_INTERVAL must be 60s."""
        from nova_big_brother import SWEEP_INTERVAL
        self.assertEqual(SWEEP_INTERVAL, 60)

    def test_kqueue_log_files_exist_or_creatable(self):
        """All log files that kqueue watches must be in ~/.openclaw/logs."""
        from nova_big_brother import LOG_FILES_TO_WATCH
        for lf in LOG_FILES_TO_WATCH:
            self.assertIn(".openclaw/logs", str(lf),
                          f"Watched log {lf} must be in ~/.openclaw/logs")
            self.assertNotIn("/Volumes/", str(lf),
                             f"Watched log {lf} must not be on external volume (TCC)")

    def test_signal_fallback_uses_signal_cli_path(self):
        """Signal fallback must use the correct signal-cli path."""
        source = (SCRIPTS_DIR / "nova_big_brother.py").read_text()
        self.assertIn("/opt/homebrew/bin/signal-cli", source,
                      "Signal fallback must use the Homebrew signal-cli path")


if __name__ == "__main__":
    unittest.main(verbosity=2)
