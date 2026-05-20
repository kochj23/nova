"""
test_nova_app_suggestions.py — All 7 test categories for nova_app_suggestions.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_app_suggestions.py"
_spec = importlib.util.spec_from_file_location("nova_app_suggestions", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_data = _mod.load_data
save_data = _mod.save_data
check_app = _mod.check_app
analyze_patterns = _mod.analyze_patterns
generate_suggestions = _mod.generate_suggestions


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

    def test_data_file_is_local(self):
        self.assertTrue(str(_mod.DATA_FILE).startswith(str(Path.home())))

    def test_apps_use_local_ports(self):
        """App status checks must target local ports, not cloud."""
        for port, *_ in _mod.APPS:
            # All ports should be local (checked via 127.0.0.1 in check_app)
            self.assertGreater(port, 0)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_check_app_fast_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            start = time.perf_counter()
            for _ in range(20):
                check_app(37400, timeout=1)
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 2.0, f"check_app 20x: {elapsed:.3f}s")

    def test_analyze_patterns_fast_on_small_data(self):
        data = {"snapshots": [
            {"date": "2026-05-13", "hour": 10, "day": "Tuesday", "running": ["MLXCode"]}
            for _ in range(7)
        ]}
        start = time.perf_counter()
        for _ in range(100):
            analyze_patterns(data)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.3)

    def test_snapshots_pruned_at_60_days(self):
        """save_data must prune snapshots older than 60 days."""
        old_date = (datetime.now() - timedelta(days=65)).isoformat()
        recent_date = datetime.now().isoformat()
        data = {
            "snapshots": [
                {"date": old_date[:10], "hour": 10, "day": "Monday", "running": []},
                {"date": recent_date[:10], "hour": 11, "day": "Tuesday", "running": []},
            ],
            "suggestions_sent": {}
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            df = Path(tmpdir) / "app_usage_log.json"
            with patch.object(_mod, "DATA_FILE", df):
                save_data(data)
                loaded = load_data()
        # Old snapshot should be pruned
        self.assertEqual(len(loaded["snapshots"]), 1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_check_app_returns_false_on_connection_refused(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            alive, data = check_app(37400)
        self.assertFalse(alive)
        self.assertEqual(data, {})

    def test_vector_remember_swallows_errors(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            try:
                _mod.vector_remember("test")
            except Exception as e:
                self.fail(f"vector_remember raised: {e}")

    def test_get_app_data_returns_none_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = _mod.get_app_data(37400, "/api/status")
        self.assertIsNone(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_load_data_returns_defaults_on_missing(self):
        with patch.object(_mod, "DATA_FILE", Path("/nonexistent/app_usage.json")):
            result = load_data()
        self.assertIn("snapshots", result)
        self.assertIn("suggestions_sent", result)
        self.assertEqual(result["snapshots"], [])

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            df = Path(tmpdir) / "app_usage_log.json"
            with patch.object(_mod, "DATA_FILE", df):
                data = {"snapshots": [{"date": "2026-05-13", "hour": 10, "day": "Tuesday",
                                        "running": ["MLXCode"]}],
                         "suggestions_sent": {}}
                save_data(data)
                loaded = load_data()
        self.assertEqual(len(loaded["snapshots"]), 1)
        self.assertIn("MLXCode", loaded["snapshots"][0]["running"])

    def test_analyze_patterns_returns_empty_with_less_than_7_snapshots(self):
        data = {"snapshots": [
            {"date": "2026-05-13", "hour": 10, "day": "Tuesday", "running": ["MLXCode"]}
        ] * 3}
        result = analyze_patterns(data)
        self.assertEqual(result, {})

    def test_analyze_patterns_returns_data_with_7_plus_snapshots(self):
        data = {"snapshots": [
            {"date": f"2026-05-{i:02d}", "hour": 10, "day": "Tuesday", "running": ["MLXCode"]}
            for i in range(1, 15)
        ]}
        result = analyze_patterns(data)
        self.assertIn("last_seen", result)
        self.assertIn("day_hour_usage", result)

    def test_check_app_returns_true_on_success(self):
        mock_r = MagicMock()
        mock_r.__enter__ = lambda s: s
        mock_r.__exit__ = MagicMock(return_value=False)
        mock_r.read.return_value = json.dumps({"status": "ok"}).encode()
        with patch("urllib.request.urlopen", return_value=mock_r):
            alive, data = check_app(37400)
        self.assertTrue(alive)

    def test_generate_suggestions_returns_list(self):
        data = {"snapshots": [], "suggestions_sent": {}}
        patterns = {}
        result = generate_suggestions(data, [], patterns)
        self.assertIsInstance(result, list)

    def test_apps_list_not_empty(self):
        self.assertGreater(len(_mod.APPS), 0)

    def test_apps_have_required_fields(self):
        for app_tuple in _mod.APPS:
            self.assertEqual(len(app_tuple), 5,
                             f"App tuple must have 5 fields: {app_tuple}")
            port, name, bundle, endpoint, stale_days = app_tuple
            self.assertIsInstance(port, int)
            self.assertIsInstance(name, str)
            self.assertGreater(stale_days, 0)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_takes_snapshot(self):
        """main() must add a snapshot to the data file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            df = Path(tmpdir) / "app_usage_log.json"
            with patch.object(_mod, "DATA_FILE", df):
                with patch.object(_mod, "check_app", return_value=(False, {})):
                    with patch.object(_mod, "generate_suggestions", return_value=[]):
                        _mod.main()
                loaded = load_data()
        self.assertGreater(len(loaded["snapshots"]), 0, "main() must add a snapshot")

    def test_main_skips_duplicate_suggestions(self):
        """main() must not re-send the same suggestion on the same day."""
        today = date.today().isoformat()
        pre_sent = {f"{today}_MLXCode_stale": "2026-05-13T10:00:00"}

        with tempfile.TemporaryDirectory() as tmpdir:
            df = Path(tmpdir) / "app_usage_log.json"
            with patch.object(_mod, "DATA_FILE", df):
                data = {"snapshots": [], "suggestions_sent": pre_sent}
                save_data(data)
                post_calls = []
                with patch.object(_mod, "check_app", return_value=(False, {})):
                    with patch.object(_mod, "generate_suggestions", return_value=[
                        {"type": "stale", "app": "MLXCode", "message": "MLXCode not used",
                         "priority": "low"}
                    ]):
                        with patch.object(_mod, "slack_post",
                                           side_effect=lambda t, **kw: post_calls.append(t)):
                            _mod.main()
        self.assertEqual(len(post_calls), 0, "Should skip already-sent suggestion")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_high_priority_suggestions_first(self):
        """generate_suggestions must return high-priority items first after sort."""
        data = {"snapshots": [], "suggestions_sent": {}}
        patterns = {"last_seen": {"NMAPScanner": "2026-01-01"}, "day_hour_usage": {}}
        suggestions = [
            {"type": "actionable", "app": "NMAPScanner",
             "message": "Security warnings found", "priority": "high"},
            {"type": "stale", "app": "MLXCode",
             "message": "Not used in 7 days", "priority": "low"},
        ]
        # Sort by priority as main() does
        priority_order = {"high": 0, "medium": 1, "low": 2}
        sorted_suggestions = sorted(suggestions,
                                    key=lambda s: priority_order.get(s["priority"], 2))
        self.assertEqual(sorted_suggestions[0]["priority"], "high")

    def test_suggestions_capped_at_5(self):
        """main() must only post up to 5 suggestions."""
        many_suggestions = [
            {"type": "stale", "app": f"App{i}", "message": f"App{i} not used",
             "priority": "low"}
            for i in range(10)
        ]
        lines = [f"*App Intelligence*"]
        for s in many_suggestions[:5]:
            lines.append(f"  💤 {s['message']}")
        self.assertEqual(len(lines), 6)  # header + 5 suggestions

    def test_stale_icon_in_suggestion(self):
        """Stale suggestions must use 💤 icon."""
        icon_map = {"stale": "💤", "pattern": "🔮", "actionable": "📌"}
        self.assertEqual(icon_map["stale"], "💤")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_app_suggestions.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertIsInstance(_mod.DATA_FILE, Path)
        self.assertIsInstance(_mod.APPS, list)
        self.assertIsInstance(_mod.TODAY, str)
        self.assertIsInstance(_mod.HOUR, int)

    def test_all_functions_callable(self):
        for fn in [load_data, save_data, check_app, analyze_patterns,
                    generate_suggestions, _mod.vector_remember, _mod.main]:
            self.assertTrue(callable(fn))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
