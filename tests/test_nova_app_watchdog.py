"""
test_nova_app_watchdog.py — All 7 test categories for nova_app_watchdog.py
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
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub nova_config before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_app_watchdog.py"
_spec = importlib.util.spec_from_file_location("nova_app_watchdog", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

check_port = _mod.check_port
check_infra_port = _mod.check_infra_port
load_state = _mod.load_state
save_state = _mod.save_state
count_recent_restarts = _mod.count_recent_restarts
restart_app = _mod.restart_app
restart_infra = _mod.restart_infra
MONITORED_APPS = _mod.MONITORED_APPS
INFRA_SERVICES = _mod.INFRA_SERVICES
ALERT_COOLDOWN = _mod.ALERT_COOLDOWN
MAX_RESTARTS_PER_HOUR = _mod.MAX_RESTARTS_PER_HOUR


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials_in_source(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA", "xoxb-", "password =", "secret ="]
        for pat in forbidden:
            self.assertNotIn(pat, src, f"Potential credential found: {pat!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src, "Hardcoded home path found — use Path.home()")

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pattern in [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney.com",
            "kochj" + _at + "digitalnoise.net",
            "kochj23" + _at + "gmail.com",
        ]:
            self.assertNotIn(pattern, src, f"PII found: {pattern!r}")

    def test_state_file_path_uses_home(self):
        """STATE_FILE must be under Path.home() — not a hardcoded user path."""
        state_path = str(_mod.STATE_FILE)
        self.assertTrue(state_path.startswith(str(Path.home())),
                        "STATE_FILE does not start with home directory")

    def test_restart_app_uses_subprocess_not_shell(self):
        """restart_app should use list args (not shell=True) to prevent injection."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            restart_app("TestApp", "TestApp")
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        self.assertIsInstance(cmd, list, "restart_app must pass list not string to subprocess")

    def test_no_credentials_in_restart_commands(self):
        """Infra restart commands should not contain passwords."""
        for _, name, cmd in INFRA_SERVICES:
            self.assertNotIn("password", cmd.lower())
            self.assertNotIn("secret", cmd.lower())


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_count_recent_restarts_fast(self):
        state = {"restarts": [{"ts": time.time() - i * 10, "app": "x"} for i in range(200)]}
        start = time.perf_counter()
        count_recent_restarts(state)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.05, f"count_recent_restarts took {elapsed:.3f}s")

    def test_check_port_respects_timeout(self):
        """check_port must not hang past its timeout."""
        start = time.perf_counter()
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            alive, info, elapsed = check_port(19999, timeout=1)
        wall = time.perf_counter() - start
        self.assertFalse(alive)
        self.assertLess(wall, 3.0, "check_port ran past timeout bound")

    def test_monitored_apps_bounded(self):
        """Should have a reasonable number of monitored items."""
        self.assertLessEqual(len(MONITORED_APPS) + len(INFRA_SERVICES), 50,
                             "Too many monitored services — could cause slow sweeps")

    def test_alert_cooldown_prevents_spam(self):
        """ALERT_COOLDOWN must be at least 5 minutes."""
        self.assertGreaterEqual(ALERT_COOLDOWN, 300,
                                "ALERT_COOLDOWN should be ≥ 300s to prevent alert spam")

    def test_max_restarts_bounded(self):
        """MAX_RESTARTS_PER_HOUR must be reasonable."""
        self.assertLessEqual(MAX_RESTARTS_PER_HOUR, 10,
                             "MAX_RESTARTS_PER_HOUR too high — could cause restart storms")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_check_port_returns_alive_on_http_error_fallback(self):
        """If /api/status raises a non-URLError, check_port falls back to root and detects alive."""
        import urllib.error
        call_count = [0]

        # Simulate: /api/status returns a response that json.loads fails on,
        # then / returns an HTTPError (404 means port is up)
        def side_effect(req, timeout=None):
            call_count[0] += 1
            url = getattr(req, "full_url", str(req))
            if "/api/status" in url:
                # Return a mock context manager that yields invalid JSON
                mock_resp = MagicMock()
                mock_resp.__enter__ = lambda s: s
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_resp.read.return_value = b"not json"
                return mock_resp
            # Fallback call to root URL — HTTPError means alive
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            alive, info, elapsed = check_port(37400, timeout=3)

        # The /api/status path reads and then json.loads fails → non-URLError → fallback
        # But json.loads failure happens inside `with urlopen() as r:` which returns False
        # Actually check_port does json.loads inside the with block, which raises
        # a non-URLError, so the outer except catches it and tries fallback.
        # The fallback raises HTTPError(404) → returns True.
        self.assertTrue(alive, "check_port should return alive on 404 at root URL")
        self.assertGreaterEqual(call_count[0], 2)

    def test_vector_remember_silently_fails(self):
        """vector_remember must not propagate exceptions."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            try:
                _mod.vector_remember("test text", {})
            except Exception as e:
                self.fail(f"vector_remember raised unexpectedly: {e}")

    def test_restart_app_returns_false_on_exception(self):
        """restart_app must return False (not raise) on subprocess exception."""
        with patch("subprocess.run", side_effect=Exception("crash")):
            result = restart_app("TestApp", "TestApp")
        self.assertFalse(result)

    def test_restart_infra_returns_false_on_exception(self):
        """restart_infra must return False (not raise) on exception."""
        with patch("subprocess.run", side_effect=Exception("crash")):
            result = restart_infra("Memory Server", "brew services restart postgresql@17")
        self.assertFalse(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_count_recent_restarts_empty(self):
        state = {"restarts": []}
        self.assertEqual(count_recent_restarts(state), 0)

    def test_count_recent_restarts_filters_old(self):
        old_ts = time.time() - 7200  # 2 hours ago
        recent_ts = time.time() - 60
        state = {"restarts": [
            {"ts": old_ts, "app": "x"},
            {"ts": recent_ts, "app": "y"},
        ]}
        self.assertEqual(count_recent_restarts(state), 1)

    def test_count_recent_restarts_prunes_state(self):
        old_ts = time.time() - 7200
        state = {"restarts": [{"ts": old_ts, "app": "x"}]}
        count_recent_restarts(state)
        self.assertEqual(len(state["restarts"]), 0, "Old restarts should be pruned")

    def test_check_infra_port_down_on_exception(self):
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            alive, info = check_infra_port(18790)
        self.assertFalse(alive)
        self.assertEqual(info, "down")

    def test_check_infra_port_alive_on_http_error(self):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError("http://x", 404, "not found", {}, None)):
            alive, info = check_infra_port(18790)
        self.assertTrue(alive)
        self.assertEqual(info, "responding")

    def test_load_state_returns_defaults_when_missing(self):
        with patch("pathlib.Path.exists", return_value=False):
            state = load_state()
        self.assertIn("apps", state)
        self.assertIn("restarts", state)
        self.assertIsInstance(state["restarts"], list)

    def test_load_state_handles_corrupt_json(self):
        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value="{INVALID JSON"):
                state = load_state()
        self.assertIn("apps", state)

    def test_restart_app_sends_open_command(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = restart_app("TestApp", "My Test App")
        cmd = mock_run.call_args[0][0]
        self.assertIn("open", cmd)
        self.assertIn("-a", cmd)
        self.assertIn("My Test App", cmd)

    def test_monitored_apps_have_required_fields(self):
        for item in MONITORED_APPS:
            port, app_name, bundle_name, critical = item
            self.assertIsInstance(port, int)
            self.assertIsInstance(app_name, str)
            self.assertIsInstance(critical, bool)

    def test_infra_services_have_required_fields(self):
        for item in INFRA_SERVICES:
            port, name, restart_cmd = item
            self.assertIsInstance(port, int)
            self.assertIsInstance(restart_cmd, str)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_save_and_load_state_roundtrip(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            with patch.object(_mod, "STATE_FILE", tmp_path):
                state = {"apps": {"37400": {"alive": True}}, "restarts": []}
                save_state(state)
                loaded = load_state()
            self.assertEqual(loaded["apps"]["37400"]["alive"], True)
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_main_posts_alert_when_app_down(self):
        """When a critical app is down (was previously up), main() should alert."""
        prev_state = {
            "apps": {"37400": {"alive": True, "last_seen": time.time() - 60, "last_alert": 0}},
            "restarts": [],
        }

        def fake_check_port(port, timeout=3):
            if port == 37400:
                return False, "connection refused", 0.1
            return True, "ok", 0.1

        def fake_check_infra(port, timeout=3):
            return True, "ok"

        with patch.object(_mod, "load_state", return_value=prev_state):
            with patch.object(_mod, "save_state"):
                with patch.object(_mod, "check_port", side_effect=fake_check_port):
                    with patch.object(_mod, "check_infra_port", side_effect=fake_check_infra):
                        with patch.object(_mod, "restart_app", return_value=False):
                            with patch.object(_mod, "capture_diagnostics", return_value="/tmp/x"):
                                with patch.object(_mod, "slack_post") as mock_slack:
                                    _mod.main()

        mock_slack.assert_called_once()
        msg = mock_slack.call_args[0][0]
        self.assertIn("DOWN", msg)

    def test_recovery_posts_recovery_message(self):
        """When app was down and is now up, main() should post recovery."""
        prev_state = {
            "apps": {"37400": {"alive": False, "last_seen": time.time() - 300, "last_alert": time.time() - 700}},
            "restarts": [],
        }

        def fake_check_port(port, timeout=3):
            return True, "ok", 0.1

        def fake_check_infra(port, timeout=3):
            return True, "ok"

        with patch.object(_mod, "load_state", return_value=prev_state):
            with patch.object(_mod, "save_state"):
                with patch.object(_mod, "check_port", side_effect=fake_check_port):
                    with patch.object(_mod, "check_infra_port", side_effect=fake_check_infra):
                        with patch.object(_mod, "slack_post") as mock_slack:
                            with patch.object(_mod, "vector_remember"):
                                _mod.main()

        mock_slack.assert_called_once()
        msg = mock_slack.call_args[0][0]
        self.assertIn("back up", msg.lower())

    def test_all_clear_no_slack_post(self):
        """When all services are up, no Slack alert should be sent."""
        prev_state = {"apps": {}, "restarts": []}

        with patch.object(_mod, "load_state", return_value=prev_state):
            with patch.object(_mod, "save_state"):
                with patch.object(_mod, "check_port", return_value=(True, "ok", 0.1)):
                    with patch.object(_mod, "check_infra_port", return_value=(True, "ok")):
                        with patch.object(_mod, "slack_post") as mock_slack:
                            _mod.main()

        mock_slack.assert_not_called()


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_restart_budget_respected(self):
        """When MAX_RESTARTS_PER_HOUR is reached, no more restarts should occur."""
        # Fill restart budget
        state = {"restarts": [{"ts": time.time() - i, "app": "x"}
                               for i in range(MAX_RESTARTS_PER_HOUR)]}
        count = count_recent_restarts(state)
        self.assertEqual(count, MAX_RESTARTS_PER_HOUR)

        restart_calls = []
        prev_state = {
            "apps": {"37400": {"alive": True, "last_seen": time.time() - 60, "last_alert": 0}},
            "restarts": state["restarts"],
        }

        def fake_check_port(port, timeout=3):
            if port == 37400:
                return False, "refused", 0.1
            return True, "ok", 0.1

        with patch.object(_mod, "load_state", return_value=prev_state):
            with patch.object(_mod, "save_state"):
                with patch.object(_mod, "check_port", side_effect=fake_check_port):
                    with patch.object(_mod, "check_infra_port", return_value=(True, "ok")):
                        with patch.object(_mod, "restart_app",
                                          side_effect=lambda *a, **kw: restart_calls.append(a) or False):
                            with patch.object(_mod, "capture_diagnostics", return_value="/tmp/x"):
                                with patch.object(_mod, "slack_post"):
                                    with patch.object(_mod, "vector_remember"):
                                        _mod.main()

        self.assertEqual(len(restart_calls), 0,
                         "restart_app should not be called when restart budget exhausted")

    def test_infra_confirm_checks_prevents_false_alert(self):
        """Infrastructure services require INFRA_CONFIRM_CHECKS consecutive failures."""
        # First check — should not alert
        prev_state = {"apps": {}, "restarts": []}

        def fake_check_port(port, timeout=3):
            return True, "ok", 0.1

        def fake_check_infra(port, timeout=3):
            return False, "down"

        with patch.object(_mod, "load_state", return_value=prev_state):
            with patch.object(_mod, "save_state"):
                with patch.object(_mod, "check_port", side_effect=fake_check_port):
                    with patch.object(_mod, "check_infra_port", side_effect=fake_check_infra):
                        with patch.object(_mod, "slack_post") as mock_slack:
                            with patch.object(_mod, "restart_infra", return_value=False):
                                with patch.object(_mod, "vector_remember"):
                                    _mod.main()

        # With INFRA_CONFIRM_CHECKS=2, first failure should NOT alert
        if _mod.INFRA_CONFIRM_CHECKS >= 2:
            mock_slack.assert_not_called()

    def test_slow_response_triggers_preemptive_restart(self):
        """Port 37400 responding > 2s should trigger preemptive restart."""
        prev_state = {"apps": {}, "restarts": []}

        def fake_check_port(port, timeout=3):
            if port == 37400:
                return True, "ok", 2.5  # Slow
            return True, "ok", 0.1

        restart_calls = []
        with patch.object(_mod, "load_state", return_value=prev_state):
            with patch.object(_mod, "save_state"):
                with patch.object(_mod, "check_port", side_effect=fake_check_port):
                    with patch.object(_mod, "check_infra_port", return_value=(True, "ok")):
                        with patch.object(_mod, "restart_app",
                                          side_effect=lambda *a, **kw: restart_calls.append(a) or True):
                            with patch.object(_mod, "capture_diagnostics", return_value="/tmp/x"):
                                with patch.object(_mod, "slack_post"):
                                    with patch.object(_mod, "vector_remember"):
                                        _mod.main()

        self.assertGreater(len(restart_calls), 0, "Slow response should trigger preemptive restart")


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
        self.assertTrue(os.access(_SCRIPT, os.X_OK), f"{_SCRIPT} is not executable")

    def test_module_constants_present(self):
        self.assertIsInstance(MONITORED_APPS, list)
        self.assertGreater(len(MONITORED_APPS), 0)
        self.assertIsInstance(INFRA_SERVICES, list)
        self.assertGreater(len(INFRA_SERVICES), 0)
        self.assertIsInstance(ALERT_COOLDOWN, int)
        self.assertIsInstance(MAX_RESTARTS_PER_HOUR, int)

    def test_novacontrol_is_critical(self):
        """NovaControl (port 37400) must be marked critical=True."""
        nc = next((a for a in MONITORED_APPS if a[0] == 37400), None)
        self.assertIsNotNone(nc, "NovaControl not found in MONITORED_APPS")
        self.assertTrue(nc[3], "NovaControl must be marked critical=True")

    def test_state_file_path_is_json(self):
        self.assertTrue(str(_mod.STATE_FILE).endswith(".json"))

    def test_main_does_not_crash_with_no_state(self):
        """main() must not raise even when state file is missing and all ports are down."""
        with patch("pathlib.Path.exists", return_value=False):
            with patch.object(_mod, "save_state"):
                with patch.object(_mod, "check_port", return_value=(False, "down", 0.1)):
                    with patch.object(_mod, "check_infra_port", return_value=(False, "down")):
                        with patch.object(_mod, "restart_app", return_value=False):
                            with patch.object(_mod, "restart_infra", return_value=False):
                                with patch.object(_mod, "capture_diagnostics", return_value="/tmp/x"):
                                    with patch.object(_mod, "slack_post"):
                                        with patch.object(_mod, "vector_remember"):
                                            try:
                                                _mod.main()
                                            except Exception as e:
                                                self.fail(f"main() raised: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
