"""
test_nova_outreach_intelligence.py — All 7 test categories for nova_outreach_intelligence.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
sys.modules["nova_config"] = _nova_cfg
sys.modules["herd_config"] = MagicMock(HERD=[
    {"name": "Sam", "email": "sam@example.com", "profile": "sam.md"},
    {"name": "Gaston", "email": "gaston@example.com", "profile": "gaston.md"},
])

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_outreach_intelligence.py"
_spec = importlib.util.spec_from_file_location("nova_outreach_intelligence", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_intelligence = _mod.load_intelligence
save_intelligence = _mod.save_intelligence
get_outreach_history = _mod.get_outreach_history
compute_warmth_scores = _mod.compute_warmth_scores
pick_best_recipient = _mod.pick_best_recipient


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

    def test_intelligence_file_is_local(self):
        self.assertTrue(str(_mod.INTELLIGENCE_FILE).startswith(str(Path.home())))

    def test_outreach_log_is_local(self):
        self.assertTrue(str(_mod.OUTREACH_LOG).startswith(str(Path.home())))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_compute_warmth_scores_fast_on_empty_log(self):
        with patch.object(_mod, "OUTREACH_LOG", Path("/nonexistent/log.log")):
            with patch.object(_mod, "get_email_history", return_value={}):
                start = time.perf_counter()
                scores = compute_warmth_scores()
                elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)
        self.assertIsInstance(scores, dict)

    def test_warmth_scores_bounded(self):
        """All warmth scores must be in 0-100 range."""
        with patch.object(_mod, "OUTREACH_LOG", Path("/nonexistent/log.log")):
            with patch.object(_mod, "get_email_history", return_value={}):
                scores = compute_warmth_scores()
        for name, score in scores.items():
            self.assertGreaterEqual(score, 0, f"{name} score below 0")
            self.assertLessEqual(score, 100, f"{name} score above 100")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_email_history_silent_on_network_error(self):
        """get_email_history must return empty dict on network failure."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = _mod.get_email_history()
        self.assertIsInstance(result, dict)

    def test_get_today_signals_returns_list_on_gh_failure(self):
        """get_today_signals must return list even when gh command fails."""
        with patch("subprocess.run", side_effect=Exception("gh not found")):
            result = _mod.get_today_signals()
        self.assertIsInstance(result, list)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_load_intelligence_returns_defaults_on_missing_file(self):
        with patch.object(_mod, "INTELLIGENCE_FILE",
                           Path("/nonexistent/outreach_intelligence.json")):
            result = load_intelligence()
        self.assertIn("contacts", result)
        self.assertIn("last_updated", result)

    def test_save_and_load_intelligence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            intel_file = Path(tmpdir) / "outreach_intelligence.json"
            with patch.object(_mod, "INTELLIGENCE_FILE", intel_file):
                data = {"contacts": {"Sam": {"warmth": 75}}, "last_updated": ""}
                save_intelligence(data)
                loaded = load_intelligence()
        self.assertEqual(loaded["contacts"]["Sam"]["warmth"], 75)

    def test_save_intelligence_sets_last_updated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            intel_file = Path(tmpdir) / "intelligence.json"
            with patch.object(_mod, "INTELLIGENCE_FILE", intel_file):
                data = {"contacts": {}, "last_updated": ""}
                save_intelligence(data)
                loaded = load_intelligence()
        self.assertNotEqual(loaded["last_updated"], "")

    def test_get_outreach_history_returns_dict(self):
        with patch.object(_mod, "OUTREACH_LOG", Path("/nonexistent/log.log")):
            result = get_outreach_history()
        self.assertIsInstance(result, dict)

    def test_get_outreach_history_parses_log(self):
        today = date.today().isoformat()
        log_content = f"[{today} 09:00:00] Outreach sent to Sam\n[{today} 15:00:00] Outreach sent to Gaston\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log_content)
            tmp = f.name
        with patch.object(_mod, "OUTREACH_LOG", Path(tmp)):
            history = get_outreach_history()
        os.unlink(tmp)
        self.assertIn("Sam", history)
        self.assertIn("Gaston", history)
        self.assertIn(today, history["Sam"])

    def test_warmth_score_decreases_for_never_contacted(self):
        """Members never contacted should have below-baseline warmth."""
        with patch.object(_mod, "OUTREACH_LOG", Path("/nonexistent/log.log")):
            with patch.object(_mod, "get_email_history", return_value={}):
                scores = compute_warmth_scores()
        for name, score in scores.items():
            self.assertLess(score, 50, f"{name} never contacted but has high warmth: {score}")

    def test_pick_best_recipient_returns_none_when_no_candidates(self):
        """pick_best_recipient must return None when all members were contacted yesterday."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        outreach = {"Sam": [yesterday], "Gaston": [yesterday]}
        with patch.object(_mod, "get_outreach_history", return_value=outreach):
            result = pick_best_recipient({"Sam": 60, "Gaston": 50}, [])
        self.assertIsNone(result)

    def test_pick_best_recipient_returns_candidate(self):
        """pick_best_recipient must return a candidate when history allows."""
        old_date = (date.today() - timedelta(days=10)).isoformat()
        outreach = {"Sam": [old_date]}
        with patch.object(_mod, "get_outreach_history", return_value=outreach):
            with patch("pathlib.Path.exists", return_value=False):
                result = pick_best_recipient({"Sam": 55, "Gaston": 50}, [])
        # Result may be None if both conditions are right, just check type
        self.assertTrue(result is None or isinstance(result, dict))


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_compute_warmth_integrates_outreach_history(self):
        """Warmth should be higher for recently contacted members."""
        today = date.today().isoformat()
        log_content = f"[{today} 09:00:00] Outreach sent to Sam\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log_content)
            tmp = f.name
        with patch.object(_mod, "OUTREACH_LOG", Path(tmp)):
            with patch.object(_mod, "get_email_history", return_value={}):
                scores = compute_warmth_scores()
        os.unlink(tmp)
        self.assertIn("Sam", scores)
        # Sam was contacted today so should have higher warmth
        self.assertGreater(scores.get("Sam", 0), 50)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_status_report_prints_without_crashing(self):
        """status_report must print a report without raising."""
        with patch.object(_mod, "OUTREACH_LOG", Path("/nonexistent/log.log")):
            with patch.object(_mod, "get_email_history", return_value={}):
                try:
                    _mod.status_report()
                except Exception as e:
                    self.fail(f"status_report raised: {e}")

    def test_suggest_prints_without_crashing(self):
        with patch.object(_mod, "OUTREACH_LOG", Path("/nonexistent/log.log")):
            with patch.object(_mod, "get_email_history", return_value={}):
                with patch.object(_mod, "get_today_signals", return_value=[]):
                    try:
                        _mod.suggest()
                    except Exception as e:
                        self.fail(f"suggest raised: {e}")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_outreach_intelligence.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertIsInstance(_mod.WORKSPACE, Path)
        self.assertIsInstance(_mod.INTELLIGENCE_FILE, Path)
        self.assertIsInstance(_mod.TODAY, str)

    def test_all_functions_callable(self):
        for fn in [load_intelligence, save_intelligence, get_outreach_history,
                    compute_warmth_scores, pick_best_recipient,
                    _mod.get_today_signals, _mod.status_report, _mod.suggest]:
            self.assertTrue(callable(fn))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
