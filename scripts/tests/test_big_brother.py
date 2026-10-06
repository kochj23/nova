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
            # Fleet is distributed since the nova-core (.2) migration: loopback for
            # local services, 192.168.1.x for LAN nodes. Never a public address.
            self.assertRegex(host, r"^(127\.0\.0\.1|192\.168\.1\.\d{1,3})$",
                             f"{name} host {host!r} must be loopback or LAN (192.168.1.x)")

    def test_critical_services_identified(self):
        from nova_big_brother import SERVICES
        critical = {s[0] for s in SERVICES if s[4]}
        # PostgreSQL is split into the local pgbouncer hop + the .2 primary.
        # Gateway v2 moved to nova-core (.2) as a systemd unit (2026-07-13) and is
        # deliberately NOT in SERVICES — .2's own watchdog owns it (see nova_big_brother.py).
        expected_critical = {"DB access (→.2)", "DB primary (.2 Beelink)", "Redis",
                              "Ollama", "Memory Server", "Scheduler"}
        for svc in expected_critical:
            self.assertIn(svc, critical,
                          f"{svc} must be marked critical")

    def test_no_public_ips_in_services(self):
        """Every probe target is loopback or RFC1918 LAN — never a public IP/hostname."""
        from nova_big_brother import SERVICES
        for name, host, *_ in SERVICES:
            self.assertRegex(host, r"^(127\.0\.0\.1|192\.168\.1\.\d{1,3})$",
                             f"{name} host {host!r} must be loopback or 192.168.1.x")

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

    def test_api_binds_lan_per_policy(self):
        # Diagnostics API is LAN-bound on purpose (README policy 2026-10-01, commit 526a71c)
        # so the other fleet nodes can scrape it. Pin the exact bind so a silent
        # regression to loopback (or a stray second listener) is caught.
        self.assertIn('HTTPServer(("0.0.0.0", API_PORT), BBHandler)', self.source,
                      "Diagnostics API must bind 0.0.0.0:API_PORT per LAN policy")
        self.assertEqual(self.source.count("HTTPServer(("), 1,
                         "Exactly one HTTP listener expected")

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

    def test_config_defines_private_sources(self):
        # nova_daily_opinion.py was removed as dead code (adf1671, superseded by
        # nova_journal.py). The canonical PRIVATE_SOURCES set now lives in nova_config.
        import nova_config
        self.assertIsInstance(nova_config.PRIVATE_SOURCES, set)
        for src in ("work_internal", "cloud_governance", "safari_history"):
            self.assertIn(src, nova_config.PRIVATE_SOURCES,
                          f"nova_config.PRIVATE_SOURCES must include {src}")

    def test_essay_script_has_private_sources(self):
        source = (SCRIPTS_DIR / "nova_daily_essay.py").read_text()
        self.assertIn("PRIVATE_SOURCES", source,
                      "nova_daily_essay.py must define PRIVATE_SOURCES")
        self.assertIn("work_internal", source)
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

    def test_journal_filters_private_sources(self):
        # nova_journal.py (the opinion/journal successor) must route every memory
        # fetch through nova_config.filter_private_memories before any public output.
        source = (SCRIPTS_DIR / "nova_journal.py").read_text()
        self.assertIn("nova_config.filter_private_memories(", source,
                      "nova_journal.py must filter memories via nova_config.filter_private_memories")


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
        self.assertIn("nova_config.SLACK_BB", source)   # was SLACK_NOTIFY; rerouted to #nova-critical (3d8b45d)
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
        """SWEEP_INTERVAL is 90s (raised from 60 in 54e0fc8 to cut alert spam)."""
        from nova_big_brother import SWEEP_INTERVAL
        self.assertEqual(SWEEP_INTERVAL, 90)

    def test_kqueue_log_files_exist_or_creatable(self):
        """kqueue-watched logs live in ~/.openclaw/logs or /tmp (canary/livetv/channel-scan
        write there, 997504e) — never on an external volume (TCC)."""
        from nova_big_brother import LOG_FILES_TO_WATCH
        for lf in LOG_FILES_TO_WATCH:
            self.assertTrue(".openclaw/logs" in str(lf) or str(lf).startswith("/tmp/"),
                            f"Watched log {lf} must be in ~/.openclaw/logs or /tmp")
            self.assertNotIn("/Volumes/", str(lf),
                             f"Watched log {lf} must not be on external volume (TCC)")

    def test_signal_fallback_uses_signal_cli_path(self):
        """Signal fallback must use the correct signal-cli path."""
        source = (SCRIPTS_DIR / "nova_big_brother.py").read_text()
        self.assertIn("/opt/homebrew/bin/signal-cli", source,
                      "Signal fallback must use the Homebrew signal-cli path")



# ── House categories added 2026-10-05 (Security, Retry, Unit, Integration, Functional, Frame) ──
# TestPerformance already lives above. Everything below is offline: Slack/Discord/local-notify
# senders are stubbed at module load, state/metrics files and the nova.jsonl log go to a tempdir.

import io
import subprocess
import tempfile
import types
from contextlib import redirect_stderr, redirect_stdout

import nova_big_brother as bb
import nova_config as _cfg
import nova_logger as _nl

SRC = (SCRIPTS_DIR / "nova_big_brother.py").read_text()
_TMP = Path(tempfile.mkdtemp(prefix="bb-test-"))
bb.PID_FILE = _TMP / "big-brother.pid"
bb.STATE_FILE = _TMP / "big-brother-state.json"
bb.METRICS_FILE = _TMP / "bb-metrics.json"
_nl.LOG_FILE = _TMP / "nova.jsonl"          # nova_logger.log() writes here at call time


class _CfgProxy:
    """nova_config as Big Brother sees it: real constants/helpers, but the senders are stubs.
    Only this module's view is replaced, so other test files keep the real post_both."""
    def __init__(self, real):
        self._real = real
        self.post_both = MagicMock(name="post_both")        # never reach Slack/Discord from a test
        self.notify_local = MagicMock(name="notify_local")  # never pop a macOS notification

    def __getattr__(self, name):
        return getattr(self._real, name)


bb.nova_config = CFG = _CfgProxy(_cfg)


def _reset_notify_state():
    bb._digest_buffer.clear(); bb._issue_last_alerted.clear(); bb._alerted_issues.clear()
    bb._last_digest_post = 0.0
    CFG.post_both = MagicMock(name="post_both"); CFG.notify_local = MagicMock(name="notify_local")


def _resp(payload, status=200):
    r = MagicMock(); r.status = status; r.read.return_value = json.dumps(payload).encode()
    return r


def _get(path):
    """Drive BBHandler.do_GET without a socket; returns the decoded JSON body."""
    h = bb.BBHandler.__new__(bb.BBHandler)
    h.path = path; h.wfile = io.BytesIO()
    h.send_response = MagicMock(); h.send_header = MagicMock(); h.end_headers = MagicMock()
    h.do_GET()
    return h.send_response.call_args[0][0], json.loads(h.wfile.getvalue() or b"null")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn("nova_config.slack_bot_token()", SRC)      # Slack token comes from the Keychain helper

    def test_sql_is_parameterized_and_writes_are_bounded(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r'execute\([^)]*%\s*\(', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"churn", "claude_queue", "incidents", "service_registry"})

    def test_model_allowlist_keeps_cloud_to_research(self):
        cloud = {m for m in bb._ALLOWED_MODELS if m.startswith("openrouter/")}
        self.assertEqual(cloud, {"openrouter/qwen/qwen3-235b-a22b-2507"})
        self.assertTrue(bb._OPENROUTER_ALLOWED_AGENTS <= {"research", "main", "chat"})

    def test_notify_strips_markup_before_the_local_banner(self):
        _reset_notify_state()
        bb._notify_immediate(":rotating_light: *Gateway DOWN* :x:", is_critical=True)
        title, body = CFG.notify_local.call_args[0][:2]
        self.assertEqual((title, body), ("Nova — Big Brother", "Gateway DOWN"))
        self.assertTrue(CFG.notify_local.call_args[1]["critical"])


class TestRetry(unittest.TestCase):
    def test_recall_check_retries_twice_then_succeeds(self):
        calls = [OSError("conn reset"), OSError("timeout"), _resp({"memories": [], "count": 0})]
        with patch.object(bb, "_hnsw_reindex_running", lambda: False), patch.object(bb.time, "sleep") as slp, \
             patch.object(bb.urllib.request, "urlopen", side_effect=calls) as u:
            self.assertTrue(bb._check_memory_server_recall())
        self.assertEqual(u.call_count, 3)
        self.assertEqual([c[0][0] for c in slp.call_args_list], [5, 5])   # backoff between attempts

    def test_recall_check_gives_up_after_three(self):
        with patch.object(bb, "_hnsw_reindex_running", lambda: False), patch.object(bb.time, "sleep"), \
             patch.object(bb.urllib.request, "urlopen", side_effect=OSError("down")) as u:
            self.assertFalse(bb._check_memory_server_recall())
        self.assertEqual(u.call_count, 3)

    def test_notify_falls_back_to_raw_slack_then_signal(self):
        # RETRY GAP: _notify_immediate — post_both is tried once; failure cascades to raw Slack HTTP, then signal-cli
        _reset_notify_state()
        CFG.post_both = MagicMock(side_effect=RuntimeError("gateway dead"))
        with patch.object(_cfg, "slack_bot_token", lambda: "tok"), \
             patch.object(bb.urllib.request, "urlopen", side_effect=OSError("slack 500")) as u, \
             patch.object(bb.subprocess, "run") as sp:
            bb._notify_immediate("boom", is_critical=True)
        self.assertEqual(u.call_count, 1)
        self.assertEqual(json.loads(u.call_args[0][0].data)["channel"], _cfg.SLACK_BB)
        self.assertEqual(sp.call_args[0][0][0], "/opt/homebrew/bin/signal-cli")

    def test_protected_task_probe_fails_open(self):
        # RETRY GAP: _is_protected_task_running — one urlopen; any failure means "not protected"
        with patch.object(bb.urllib.request, "urlopen", side_effect=OSError("scheduler down")):
            self.assertFalse(bb._is_protected_task_running())
        with patch.object(bb.urllib.request, "urlopen", side_effect=[_resp({"tasks_running": 1}), OSError("x")]):
            self.assertTrue(bb._is_protected_task_running())       # can't see names -> conservative

    def test_http_healthy_is_one_shot(self):
        # RETRY GAP: _http_healthy — a single probe; score history (3 of 5) is the dampener, not a retry
        with patch.object(bb.urllib.request, "urlopen", side_effect=OSError("refused")) as u:
            self.assertFalse(bb._http_healthy("127.0.0.1", 1, "/health"))
        self.assertEqual(u.call_count, 1)


class TestUnit(unittest.TestCase):
    def setUp(self):
        bb._service_score_history.clear(); bb._service_check_interval.clear()
        bb._service_last_checked.clear(); bb._service_healthy_since.clear()
        bb._service_restart_times.clear(); bb._service_crash_loop_until.clear()
        _reset_notify_state()

    def test_score_history_needs_three_of_five(self):
        self.assertTrue(bb._score_history_confirms_down("X", False))       # first sample: trust it
        self.assertFalse(bb._score_history_confirms_down("X", True))
        self.assertFalse(bb._score_history_confirms_down("X", False))      # 2 of 3 down
        self.assertTrue(bb._score_history_confirms_down("X", False))       # 3 of 4 down
        for _ in range(5):
            bb._score_history_confirms_down("X", True)
        self.assertFalse(bb._score_history_confirms_down("X", False))      # window slid clean

    def test_adaptive_interval(self):
        self.assertEqual(bb._get_service_interval("Y"), bb.SWEEP_INTERVAL)
        self.assertTrue(bb._should_check_now("Y"))
        bb._update_adaptive_interval("Y", True)
        self.assertEqual(bb._get_service_interval("Y"), bb.SWEEP_INTERVAL)
        self.assertFalse(bb._should_check_now("Y"))
        bb._service_healthy_since["Y"] -= bb.HEALTHY_STRETCH_S + 1
        bb._update_adaptive_interval("Y", True)
        self.assertEqual(bb._get_service_interval("Y"), bb.RELAXED_INTERVAL)
        bb._update_adaptive_interval("Y", False)
        self.assertEqual(bb._get_service_interval("Y"), bb.HEIGHTENED_INTERVAL)
        self.assertNotIn("Y", bb._service_healthy_since)

    def test_crash_loop_detection(self):
        with patch.object(bb, "_escalate_to_claude") as esc:
            self.assertFalse(bb._check_crash_loop("Redis"))
            self.assertFalse(bb._check_crash_loop("Redis"))
            self.assertTrue(bb._check_crash_loop("Redis"))                 # third restart in the window
            self.assertTrue(bb._check_crash_loop("Redis"))                 # cooldown holds
        esc.assert_called_once()
        self.assertEqual(esc.call_args[1]["priority"], 2)
        CFG.post_both.assert_called_once()
        self.assertIn("Crash-loop detected: Redis", CFG.post_both.call_args[0][0])

    def test_maybe_notify_cooldown(self):
        bb._maybe_notify("k", "first", is_critical=True, cooldown=600)
        bb._maybe_notify("k", "second", is_critical=True, cooldown=600)
        self.assertEqual(CFG.post_both.call_count, 1)
        bb._issue_last_alerted["k"] -= 601
        bb._maybe_notify("k", "third", is_critical=True, cooldown=600)
        self.assertEqual(CFG.post_both.call_count, 2)

    def test_key_to_bb_name_edges(self):
        self.assertIsNone(bb._key_to_bb_name("no-such-key"))
        self.assertIsNone(bb._key_to_bb_name(None))
        for key, name in bb._KEY_TO_BB_NAME.items():
            self.assertEqual(bb._key_to_bb_name(key), name)

    def test_quiet_hours_boundaries(self):
        with patch("nova_big_brother.datetime") as dt:
            now = MagicMock(); dt.now.return_value = now
            for hour, quiet in ((22, True), (21, False), (7, True), (8, False), (0, True)):
                now.hour = hour
                self.assertEqual(bb._is_quiet_hours(), quiet, hour)

    def test_non_critical_notify_is_buffered_not_posted(self):
        bb._notify("warn only")
        CFG.post_both.assert_not_called()
        self.assertEqual([m for _, m in bb._digest_buffer], ["warn only"])


class TestIntegration(unittest.TestCase):
    def setUp(self):
        _reset_notify_state()

    def test_escalator_helpers_are_imported_not_reimplemented(self):
        import nova_bb_escalator as esc
        self.assertIs(bb.should_notify, esc.should_notify)
        self.assertIs(bb._resolve_escalation, esc._resolve_escalation)
        self.assertNotIn("def should_notify", SRC)

    def test_notify_then_flush_digest_posts_one_rollup_to_the_digest_channel(self):
        for _ in range(3):
            bb._notify(":x: Plex DOWN")
        bb._notify("RESOLVED: Redis")
        bb._flush_digest()
        CFG.post_both.assert_called_once()
        msg, kw = CFG.post_both.call_args[0][0], CFG.post_both.call_args[1]
        self.assertEqual(kw["slack_channel"], _cfg.SLACK_DIGEST)
        self.assertIn("1 issues (3 events)", msg)
        self.assertIn("Plex DOWN (x3)", msg)
        self.assertIn("1 auto-resolved", msg)
        self.assertEqual(bb._digest_buffer, [])

    def test_critical_notify_goes_straight_to_the_critical_channel(self):
        bb._maybe_notify("gw", ":rotating_light: Gateway DOWN", is_critical=True)
        CFG.post_both.assert_called_once_with(":rotating_light: Gateway DOWN", slack_channel=_cfg.SLACK_BB)
        self.assertEqual(bb._digest_buffer, [])

    def test_service_is_up_routes_http_vs_port(self):
        with patch.object(bb, "_http_healthy", return_value=True) as h, patch.object(bb, "_port_open", return_value=False) as p:
            self.assertTrue(bb._service_is_up("Ollama", "127.0.0.1", 11434, "/api/version"))
            self.assertFalse(bb._service_is_up("Redis", "127.0.0.1", 6379, None))
        h.assert_called_once_with("127.0.0.1", 11434, "/api/version"); p.assert_called_once_with("127.0.0.1", 6379)

    def test_record_event_persists_state_to_the_redirected_file(self):
        bb._heal_events.clear()
        bb._record_event("info", "state write", "ok", "Test")
        state = json.loads(bb.STATE_FILE.read_text())
        self.assertEqual(state["events_total"], 1)
        self.assertEqual(state["pid"], os.getpid())
        self.assertTrue(str(bb.STATE_FILE).startswith(str(_TMP)))


class TestFunctional(unittest.TestCase):
    def setUp(self):
        _reset_notify_state(); bb._heal_events.clear()

    def test_api_events_endpoint_serves_newest_first_with_limit(self):
        for i in range(5):
            bb._record_event("info", f"ev{i}", "fix", "Svc")
        status, body = _get("/bb/events?n=2")
        self.assertEqual(status, 200)
        self.assertEqual([e["issue"] for e in body], ["ev4", "ev3"])
        status, body = _get("/bb/events?n=junk")
        self.assertEqual(len(body), 5)                    # bad n falls back to the default of 100

    def test_api_unknown_route_is_404(self):
        status, _ = _get("/bb/nonexistent")
        self.assertEqual(status, 404)

    def test_digest_flush_respects_interval_and_empty_buffer(self):
        bb._last_digest_post = time.time()
        bb._notify("buffered")
        bb._flush_digest()
        CFG.post_both.assert_not_called()
        bb._last_digest_post = 0.0; bb._digest_buffer.clear()
        bb._flush_digest()
        CFG.post_both.assert_not_called()                 # nothing to say -> nothing posted

    def test_digest_post_failure_never_escapes(self):
        CFG.post_both = MagicMock(side_effect=RuntimeError("slack 500"))
        bb._notify("one thing")
        with patch.object(_cfg, "slack_bot_token", lambda: ""), redirect_stderr(io.StringIO()):
            bb._flush_digest()                             # fallback token empty -> silently logged
        self.assertEqual(bb._digest_buffer, [])

    def test_log_scan_resumes_and_handles_rotation(self):
        log = _TMP / "scan.log"
        log.write_text("fine\n")
        bb._SEEN_ERRORS.pop(str(log), None)
        self.assertEqual(bb._scan_log_file(log), [])
        with open(log, "a") as f:
            f.write("No space left on device\n")
        found = bb._scan_log_file(log)
        self.assertEqual([(s, svc, d) for s, svc, d, _ in found], [("critical", "System", "Disk full")])
        log.write_text("OOM killer\n")                       # shorter file == rotated -> rescanned from 0
        self.assertEqual(bb._scan_log_file(log)[0][2], "Out of memory condition")
        self.assertEqual(bb._scan_log_file(_TMP / "missing.log"), [])

    def test_metrics_round_trip_through_tempdir(self):
        bb._metrics.clear(); bb._metrics.append({"t": 1, "issues": 0})
        bb._flush_metrics()
        bb._metrics.clear(); bb._load_metrics()
        self.assertEqual(list(bb._metrics), [{"t": 1, "issues": 0}])


class TestFrame(unittest.TestCase):
    def test_import_never_starts_the_daemon(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_big_brother"], cwd=str(SCRIPTS_DIR),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_main_is_a_daemon_with_a_pid_file_and_sweep_loop(self):
        self.assertIn("_write_pid()", SRC)
        self.assertIn("while not _shutdown.is_set():", SRC)
        self.assertNotIn("argparse", SRC)                   # no CLI surface; launchd owns the lifecycle


if __name__ == "__main__":
    unittest.main(verbosity=2)
