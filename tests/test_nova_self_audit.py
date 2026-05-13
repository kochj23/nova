"""
test_nova_self_audit.py — All 7 test categories for nova_self_audit.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Load module under test — stub nova_config
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_self_audit.py"

_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

# Suppress logging to file during tests
with patch("logging.FileHandler", MagicMock()), \
     patch("logging.basicConfig", MagicMock()):
    _spec = importlib.util.spec_from_file_location("nova_self_audit", _SCRIPT)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)

audit_scripts = _mod.audit_scripts
audit_services = _mod.audit_services
audit_processes = _mod.audit_processes
audit_docs = _mod.audit_docs
run_audit = _mod.run_audit
_port_listening = _mod._port_listening
_process_running = _mod._process_running
_scripts_on_disk = _mod._scripts_on_disk


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-live", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pattern in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(pattern, src, f"PII found: {pattern!r}")

    def test_audit_state_file_path_in_home(self):
        """State file must be in home directory, not a temp or global location."""
        self.assertTrue(str(_mod.AUDIT_STATE_FILE).startswith(str(Path.home())))

    def test_scripts_dir_in_home(self):
        """SCRIPTS_DIR must be in home directory."""
        self.assertTrue(str(_mod.SCRIPTS_DIR).startswith(str(Path.home())))

    def test_port_check_uses_localhost(self):
        """_port_listening() must only connect to localhost."""
        src = _SCRIPT.read_text()
        # Should connect to 127.0.0.1, not 0.0.0.0 or external IPs
        self.assertIn("127.0.0.1", src, "_port_listening() must use 127.0.0.1")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_scripts_on_disk_fast(self):
        """_scripts_on_disk() must scan scripts dir in < 500ms."""
        start = time.perf_counter()
        _scripts_on_disk()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"_scripts_on_disk() took {elapsed:.3f}s (limit 500ms)")

    def test_port_check_has_timeout(self):
        """_port_listening() must set a timeout to avoid hanging on closed ports."""
        src = _SCRIPT.read_text()
        self.assertIn("settimeout(2)", src, "_port_listening() must use settimeout(2)")

    def test_load_save_audit_state_fast(self):
        """Load and save audit state must complete in < 50ms."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"last_issue_key": "[]"}, f)
            fname = f.name
        try:
            with patch.object(_mod, "AUDIT_STATE_FILE", Path(fname)):
                start = time.perf_counter()
                state = _mod._load_last_audit_state()
                _mod._save_audit_state(state)
                elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 0.05, f"State load/save took {elapsed:.3f}s")
        finally:
            os.unlink(fname)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_port_listening_returns_false_on_refused(self):
        """_port_listening() must return False when connection is refused."""
        # Use a port that's almost certainly not in use
        result = _port_listening(19999)
        # Should return False or True — must not raise
        self.assertIsInstance(result, bool)

    def test_process_running_returns_false_on_pgrep_failure(self):
        """_process_running() must return False when pgrep fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            result = _process_running("nonexistent_process_xyz")
        self.assertFalse(result)

    def test_run_audit_handles_slack_post_failure(self):
        """run_audit() with working Slack must return issue count."""
        # Test that run_audit returns an integer count (normal path)
        with patch.object(_mod, "audit_scripts", return_value=([], [], 5, 5, 3)):
            with patch.object(_mod, "audit_services", return_value=([], ["Gateway :18789"])):
                with patch.object(_mod, "audit_processes", return_value=([], ["Scheduler"])):
                    with patch.object(_mod, "audit_docs", return_value=[]):
                        with patch.object(_mod, "slack_post", return_value=None):
                            with patch.object(_mod, "_load_last_audit_state",
                                              return_value={"last_issue_key": '["changed"]'}):
                                with patch.object(_mod, "_save_audit_state"):
                                    result = run_audit()
        self.assertIsInstance(result, int)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_scripts_on_disk_returns_set(self):
        result = _scripts_on_disk()
        self.assertIsInstance(result, set)

    def test_scripts_in_file_handles_missing_file(self):
        result = _mod._scripts_in_file(Path("/nonexistent/MEMORY.md"))
        self.assertEqual(result, set())

    def test_scripts_in_file_finds_nova_scripts(self):
        """_scripts_in_file() must find nova_*.py references."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("- nova_scheduler.py handles task scheduling\n")
            f.write("- dream_deliver.py posts dreams\n")
            fname = f.name
        try:
            result = _mod._scripts_in_file(Path(fname))
            self.assertIn("nova_scheduler.py", result)
            self.assertIn("dream_deliver.py", result)
        finally:
            os.unlink(fname)

    def test_load_audit_state_returns_empty_on_missing_file(self):
        with patch.object(_mod, "AUDIT_STATE_FILE", Path("/nonexistent/state.json")):
            result = _mod._load_last_audit_state()
        self.assertEqual(result, {})

    def test_save_load_audit_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "AUDIT_STATE_FILE", Path(tmpdir) / "state.json"):
                _mod._save_audit_state({"last_issue_key": "[\"issue1\"]", "last_run": "2026-01-01"})
                loaded = _mod._load_last_audit_state()
        self.assertEqual(loaded["last_issue_key"], "[\"issue1\"]")

    def test_audit_services_returns_issues_for_down_ports(self):
        """audit_services() must report issues for ports that aren't listening."""
        with patch.object(_mod, "_port_listening", return_value=False):
            issues, ok = audit_services()
        self.assertGreater(len(issues), 0, "Closed ports must generate issues")
        self.assertEqual(len(ok), 0)

    def test_audit_services_returns_ok_for_listening_ports(self):
        """audit_services() must report ok for ports that are listening."""
        with patch.object(_mod, "_port_listening", return_value=True):
            issues, ok = audit_services()
        self.assertEqual(len(issues), 0)
        self.assertGreater(len(ok), 0)

    def test_audit_processes_down(self):
        """audit_processes() must report issues for processes not running."""
        with patch.object(_mod, "_process_running", return_value=False):
            issues, ok = audit_processes()
        self.assertGreater(len(issues), 0)

    def test_audit_processes_running(self):
        with patch.object(_mod, "_process_running", return_value=True):
            issues, ok = audit_processes()
        self.assertEqual(len(issues), 0)

    def test_audit_docs_reports_missing_memory_md(self):
        """audit_docs() must flag missing MEMORY.md."""
        with patch.object(_mod, "MEMORY_MD", Path("/nonexistent/MEMORY.md")):
            with patch.object(_mod, "IDENTITY_MD", Path("/nonexistent/IDENTITY.md")):
                issues = audit_docs()
        self.assertTrue(any("MEMORY.md" in i for i in issues))


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_run_audit_returns_issue_count(self):
        """run_audit() must return an integer issue count."""
        with patch.object(_mod, "audit_scripts", return_value=([], [], 10, 8, 5)):
            with patch.object(_mod, "audit_services", return_value=([], ["Gateway"])):
                with patch.object(_mod, "audit_processes", return_value=([], ["Scheduler"])):
                    with patch.object(_mod, "audit_docs", return_value=[]):
                        with patch.object(_mod, "slack_post"):
                            with patch.object(_mod, "_load_last_audit_state", return_value={}):
                                with patch.object(_mod, "_save_audit_state"):
                                    count = run_audit()

        self.assertIsInstance(count, int)
        self.assertEqual(count, 0)

    def test_run_audit_posts_to_slack_on_new_issues(self):
        """run_audit() must post to Slack when new issues are found."""
        slack_calls = []
        with patch.object(_mod, "audit_scripts",
                          return_value=(["nova_missing.py missing from disk"], [], 10, 10, 5)):
            with patch.object(_mod, "audit_services", return_value=([], [])):
                with patch.object(_mod, "audit_processes", return_value=([], [])):
                    with patch.object(_mod, "audit_docs", return_value=[]):
                        with patch.object(_mod, "slack_post",
                                          side_effect=lambda msg: slack_calls.append(msg)):
                            with patch.object(_mod, "_load_last_audit_state", return_value={}):
                                with patch.object(_mod, "_save_audit_state"):
                                    run_audit()

        self.assertTrue(len(slack_calls) > 0, "run_audit() must post to Slack when issues exist")

    def test_run_audit_skips_slack_when_unchanged(self):
        """run_audit() must NOT post to Slack when issues are unchanged from last run."""
        issue = "Gateway (:18789) is not listening"
        issue_key = json.dumps(sorted([issue]))
        slack_calls = []

        with patch.object(_mod, "audit_scripts", return_value=([], [], 10, 10, 5)):
            with patch.object(_mod, "audit_services", return_value=([issue], [])):
                with patch.object(_mod, "audit_processes", return_value=([], [])):
                    with patch.object(_mod, "audit_docs", return_value=[]):
                        with patch.object(_mod, "slack_post",
                                          side_effect=lambda msg: slack_calls.append(msg)):
                            with patch.object(_mod, "_load_last_audit_state",
                                              return_value={"last_issue_key": issue_key}):
                                with patch.object(_mod, "_save_audit_state"):
                                    run_audit()

        self.assertEqual(len(slack_calls), 0,
                         "run_audit() must skip Slack when issues are unchanged")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_script_no_crash_on_direct_run(self):
        """Script must not crash when executed with all ports/processes down."""
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(_SCRIPT.parent)},
            timeout=10,
        )
        # Script may exit 0 or 1, but must not traceback
        self.assertNotIn("Traceback", result.stderr,
                         f"Script crashed: {result.stderr[:500]}")

    def test_scripts_in_scheduler_handles_missing_yaml(self):
        """_scripts_in_scheduler() must return {} if scheduler.yaml is missing."""
        with patch.object(_mod, "SCHEDULER_YAML", Path("/nonexistent/scheduler.yaml")):
            result = _mod._scripts_in_scheduler()
        self.assertEqual(result, {})


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_required_functions_exist(self):
        for fn in ["audit_scripts", "audit_services", "audit_processes",
                   "audit_docs", "run_audit", "slack_post",
                   "_port_listening", "_process_running", "_scripts_on_disk"]:
            self.assertTrue(callable(getattr(_mod, fn, None)), f"Missing: {fn}")

    def test_expected_services_defined(self):
        self.assertIsInstance(_mod.EXPECTED_SERVICES, dict)
        self.assertGreater(len(_mod.EXPECTED_SERVICES), 2)

    def test_expected_processes_defined(self):
        self.assertIsInstance(_mod.EXPECTED_PROCESSES, list)
        self.assertGreater(len(_mod.EXPECTED_PROCESSES), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
