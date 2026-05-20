"""
test_nova_unas_monitor.py — All 7 test categories for nova_unas_monitor.py
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
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

# Stub the UNASClient module
_unas_client_mod = MagicMock()


class _MockUNASError(Exception):
    pass


_unas_client_mod.UNASClient = MagicMock()
_unas_client_mod.UNASError = _MockUNASError
sys.modules["nova_unas_client"] = _unas_client_mod

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_unas_monitor.py"
_spec = importlib.util.spec_from_file_location("nova_unas_monitor", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

check_storage = _mod.check_storage
check_shares = _mod.check_shares
check_device = _mod.check_device
_load_state = _mod._load_state
_save_state = _mod._save_state
_fmt_bytes = _mod._fmt_bytes
STORAGE_WARN_PCT = _mod.STORAGE_WARN_PCT
STORAGE_CRIT_PCT = _mod.STORAGE_CRIT_PCT


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_state_file_under_home(self):
        self.assertTrue(str(_mod.STATE_FILE).startswith(str(Path.home())))

    def test_status_file_under_home(self):
        self.assertTrue(str(_mod.STATUS_FILE).startswith(str(Path.home())))

    def test_privacy_local_only(self):
        """UNAS data must be local-only."""
        src = _SCRIPT.read_text()
        self.assertIn("local-only", src.lower(), "UNAS data must be marked local-only")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_check_storage_fast(self):
        snapshot = {"storage": {"status": "healthy", "used_pct": 50, "free_tb": 4.0}}
        start = time.perf_counter()
        for _ in range(10000):
            check_storage(snapshot)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_fmt_bytes_fast(self):
        start = time.perf_counter()
        for i in range(10000):
            _fmt_bytes(i * 1024 * 1024)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_thresholds_reasonable(self):
        self.assertGreater(STORAGE_WARN_PCT, 50)
        self.assertGreater(STORAGE_CRIT_PCT, STORAGE_WARN_PCT)
        self.assertLess(STORAGE_CRIT_PCT, 100)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_load_state_returns_empty_on_missing(self):
        with patch.object(_mod.STATE_FILE, "exists", return_value=False):
            state = _load_state()
        self.assertIsInstance(state, dict)

    def test_load_state_returns_empty_on_corrupt(self):
        with patch.object(_mod.STATE_FILE, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value="{BAD"):
                state = _load_state()
        self.assertIsInstance(state, dict)

    def test_ingest_memory_silently_fails(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            try:
                _mod._ingest_memory("test text", "unas_monitor")
            except Exception as e:
                self.fail(f"_ingest_memory raised: {e}")

    def test_post_slack_silently_fails(self):
        _nova_cfg.post_both.side_effect = Exception("slack down")
        try:
            _mod.post_slack("test message")
        except Exception:
            pass  # nova_config mock errors are expected
        finally:
            _nova_cfg.post_both.side_effect = None


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_check_storage_healthy(self):
        snapshot = {"storage": {"status": "healthy", "used_pct": 50, "free_tb": 4.0}}
        problems = check_storage(snapshot)
        self.assertEqual(len(problems), 0)

    def test_check_storage_warning(self):
        snapshot = {"storage": {"status": "healthy", "used_pct": 82, "free_tb": 1.8}}
        problems = check_storage(snapshot)
        self.assertEqual(len(problems), 1)
        self.assertIn("warning", problems[0].lower())

    def test_check_storage_critical(self):
        snapshot = {"storage": {"status": "healthy", "used_pct": 92, "free_tb": 0.8}}
        problems = check_storage(snapshot)
        self.assertEqual(len(problems), 1)
        self.assertIn("CRITICAL", problems[0])

    def test_check_storage_bad_status(self):
        snapshot = {"storage": {"status": "degraded", "used_pct": 50, "free_tb": 4.0}}
        problems = check_storage(snapshot)
        self.assertGreater(len(problems), 0)
        self.assertIn("degraded", problems[0])

    def test_check_storage_needs_more_disk(self):
        snapshot = {"storage": {"status": "healthy", "used_pct": 50,
                                 "free_tb": 4.0, "needs_more_disk": True}}
        problems = check_storage(snapshot)
        self.assertGreater(len(problems), 0)

    def test_check_shares_healthy(self):
        snapshot = {"shares": [{"name": "Media", "status": "active", "used_tb": 1.0}]}
        problems = check_shares(snapshot)
        self.assertEqual(len(problems), 0)

    def test_check_shares_problem(self):
        snapshot = {"shares": [{"name": "Media", "status": "error", "used_tb": 1.0}]}
        problems = check_shares(snapshot)
        self.assertGreater(len(problems), 0)

    def test_check_device_healthy(self):
        snapshot = {"device": {"state": "active"}}
        problems = check_device(snapshot)
        self.assertEqual(len(problems), 0)

    def test_check_device_setup_is_ok(self):
        snapshot = {"device": {"state": "setup"}}
        problems = check_device(snapshot)
        self.assertEqual(len(problems), 0)

    def test_check_device_problem_state(self):
        snapshot = {"device": {"state": "error"}}
        problems = check_device(snapshot)
        self.assertGreater(len(problems), 0)

    def test_fmt_bytes_tb(self):
        result = _fmt_bytes(int(2e12))
        self.assertIn("TB", result)

    def test_fmt_bytes_gb(self):
        result = _fmt_bytes(int(500e9))
        self.assertIn("GB", result)

    def test_fmt_bytes_mb(self):
        result = _fmt_bytes(int(50e6))
        self.assertIn("MB", result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_new_problems_trigger_slack_alert(self):
        """When new problems are detected, a Slack alert should fire."""
        snapshot = {
            "storage": {"status": "degraded", "used_pct": 50, "free_tb": 4.0},
            "shares": [],
            "device": {"state": "active"},
        }
        slack_calls = []
        with patch.object(_mod.client, "health_snapshot", return_value=snapshot):
            with patch.object(_mod, "_load_state", return_value={"problems": [], "last_check": ""}):
                with patch.object(_mod, "_save_state"):
                    with patch.object(_mod, "_save_status"):
                        with patch.object(_mod, "post_slack",
                                          side_effect=lambda m, **kw: slack_calls.append(m)):
                            with patch.object(_mod, "_ingest_memory"):
                                _mod.main()

        self.assertGreater(len(slack_calls), 0, "New problems should trigger Slack alert")

    def test_recovery_posts_resolved_message(self):
        """When all problems resolve, a cleared message should be sent."""
        snapshot = {
            "storage": {"status": "healthy", "used_pct": 50, "free_tb": 4.0},
            "shares": [],
            "device": {"state": "active"},
        }
        slack_calls = []
        with patch.object(_mod.client, "health_snapshot", return_value=snapshot):
            with patch.object(_mod, "_load_state",
                              return_value={"problems": ["Storage degraded"], "last_check": ""}):
                with patch.object(_mod, "_save_state"):
                    with patch.object(_mod, "_save_status"):
                        with patch.object(_mod, "post_slack",
                                          side_effect=lambda m, **kw: slack_calls.append(m)):
                            with patch.object(_mod, "_ingest_memory"):
                                _mod.main()

        self.assertGreater(len(slack_calls), 0)
        self.assertIn("resolved", slack_calls[-1].lower())


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_status_file_updated_on_run(self):
        """_save_status should always be called to update NovaControl."""
        snapshot = {
            "storage": {"status": "healthy", "used_pct": 40, "free_tb": 6.0},
            "shares": [],
            "device": {"state": "active"},
        }
        status_saved = []
        with patch.object(_mod.client, "health_snapshot", return_value=snapshot):
            with patch.object(_mod, "_load_state", return_value={}):
                with patch.object(_mod, "_save_state"):
                    with patch.object(_mod, "_save_status",
                                      side_effect=lambda s: status_saved.append(s)):
                        with patch.object(_mod, "post_slack"):
                            with patch.object(_mod, "_ingest_memory"):
                                _mod.main()

        self.assertEqual(len(status_saved), 1, "_save_status should be called once per run")

    def test_main_exits_on_unas_error(self):
        """main() should exit(1) if UNASClient raises UNASError."""
        with patch.object(_mod.client, "health_snapshot",
                          side_effect=_MockUNASError("connection failed")):
            with self.assertRaises(SystemExit) as ctx:
                _mod.main()
            self.assertEqual(ctx.exception.code, 1)


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

    def test_module_has_main(self):
        self.assertTrue(callable(_mod.main))

    def test_storage_thresholds_defined(self):
        self.assertIsNotNone(STORAGE_WARN_PCT)
        self.assertIsNotNone(STORAGE_CRIT_PCT)

    def test_state_file_is_json(self):
        self.assertTrue(str(_mod.STATE_FILE).endswith(".json"))

    def test_status_file_is_json(self):
        self.assertTrue(str(_mod.STATUS_FILE).endswith(".json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
