"""
test_nova_morning_brief.py — All 7 test categories for nova_morning_brief.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub dependencies before loading
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_morning_brief.py"

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()

# Stub nova_mail_deliver before the script imports it
_mail_deliver = MagicMock()
_mail_deliver.parse_accounts_from_file = MagicMock(return_value={})
_mail_deliver.is_noise = MagicMock(return_value=False)
_mail_deliver.is_important = MagicMock(return_value=False)

sys.modules["nova_config"] = _nova_cfg
sys.modules["nova_mail_deliver"] = _mail_deliver

_spec = importlib.util.spec_from_file_location("nova_morning_brief", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

get_weather = _mod.get_weather
get_email_priorities = _mod.get_email_priorities
get_github_overnight = _mod.get_github_overnight
get_system_health = _mod.get_system_health
get_mail_summary = _mod.get_mail_summary
vector_remember = _mod.vector_remember
slack_post = _mod.slack_post


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "password =", "secret ="]:
            self.assertNotIn(pat, src, f"Potential credential: {pat!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src, "Hardcoded home path found")

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney.com",
            "kochj" + _at + "digitalnoise.net",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII email in source: {p!r}")

    def test_vector_remember_does_not_log_secrets(self):
        """vector_remember must not include any auth tokens in its payload."""
        payloads = []
        def capture_urlopen(req, timeout=None):
            payloads.append(json.loads(req.data.decode()))
            return MagicMock(__enter__=lambda s: s, __exit__=MagicMock(return_value=False))
        with patch("urllib.request.urlopen", side_effect=capture_urlopen):
            vector_remember("Morning brief test summary", {"date": "2026-01-01"})
        if payloads:
            payload_str = json.dumps(payloads[0])
            self.assertNotIn("sk-", payload_str)
            self.assertNotIn("password", payload_str.lower())

    def test_github_api_uses_gh_cli_not_tokens(self):
        """GitHub data must use gh CLI, not raw HTTP with PATs."""
        src = _SCRIPT.read_text()
        _at = "@"
        self.assertNotIn("ghp_", src)
        # Uses subprocess gh call
        self.assertIn("gh", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_weather_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)

    def test_github_call_has_timeout(self):
        """gh CLI calls must have a timeout parameter."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)

    def test_system_health_check_fast_on_unavailable(self):
        start = time.perf_counter()
        with patch("urllib.request.urlopen", side_effect=OSError("no route")):
            issues, mem_count = get_system_health()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 5.0)
        self.assertIsInstance(issues, list)

    def test_get_mail_summary_respects_cap_of_5_important(self):
        """Mail summary must cap important list at 5 items."""
        fake_accounts = {
            "acct1": [
                {"unread": True, "sender": f"boss{i}@co.com", "subject": f"Urgent {i}"}
                for i in range(20)
            ]
        }
        _mail_deliver.parse_accounts_from_file.return_value = fake_accounts
        _mail_deliver.is_important.return_value = True
        _mail_deliver.is_noise.return_value = False

        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            with patch.object(_mod, "SUMMARY_FILE", MagicMock(exists=lambda: True,
                              read_text=lambda **kw: "MOCK_MAIL")):
                result = get_mail_summary()
        self.assertLessEqual(len(result.get("important", [])), 5)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_weather_tries_multiple_fallbacks(self):
        """get_weather must try at least 2 sources on failure."""
        call_count = [0]
        def count_urlopen(req, timeout=None):
            call_count[0] += 1
            raise OSError("fail")
        with patch("urllib.request.urlopen", side_effect=count_urlopen):
            result = get_weather()
        self.assertGreaterEqual(call_count[0], 2)
        self.assertIsInstance(result, str)

    def test_weather_returns_string_on_all_failures(self):
        with patch("urllib.request.urlopen", side_effect=OSError("no net")):
            result = get_weather()
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_vector_remember_silent_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            try:
                vector_remember("test text", {"date": "2026-01-01"})
            except Exception as e:
                self.fail(f"vector_remember raised: {e}")

    def test_get_email_priorities_returns_empty_on_missing_file(self):
        with patch.object(_mod, "MEMORY_DIR", Path("/nonexistent/path")):
            result = get_email_priorities()
        self.assertIsInstance(result, list)

    def test_get_github_overnight_silent_on_failure(self):
        with patch("subprocess.run", side_effect=Exception("gh not found")):
            result = get_github_overnight()
        self.assertIsInstance(result, list)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_weather_converts_celsius_to_fahrenheit(self):
        fake = MagicMock()
        fake.read.return_value = b"Sunny +20\xc2\xb0C feels +18\xc2\xb0C humidity 50%"
        fake.status = 200
        fake.__enter__ = lambda s: s
        fake.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=fake):
            result = get_weather()
        # Should have F conversion
        self.assertIn("°F", result)

    def test_weather_returns_fallback_string_on_all_sources_fail(self):
        with patch("urllib.request.urlopen", side_effect=OSError("all fail")):
            result = get_weather()
        self.assertIn("Weather", result)

    def test_get_email_priorities_returns_list(self):
        with patch.object(_mod, "MEMORY_DIR", Path("/nonexistent")):
            result = get_email_priorities()
        self.assertIsInstance(result, list)

    def test_get_mail_summary_returns_dict_with_keys(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            result = get_mail_summary()
        self.assertIn("total_unread", result)
        self.assertIn("important", result)
        self.assertIn("noise_count", result)
        self.assertIn("success", result)

    def test_get_mail_summary_fail_returns_success_false(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1)):
            result = get_mail_summary()
        self.assertFalse(result["success"])

    def test_get_mail_summary_no_mail_returns_success_true(self):
        _mail_deliver.parse_accounts_from_file.return_value = {}
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            with patch.object(_mod, "SUMMARY_FILE",
                              MagicMock(exists=lambda: True,
                                        read_text=lambda **kw: "NO_MAIL")):
                result = get_mail_summary()
        self.assertTrue(result["success"])
        self.assertEqual(result["total_unread"], 0)

    def test_github_overnight_parses_star_count(self):
        gh_out = json.dumps({"stargazerCount": 42, "openIssues": {"totalCount": 2}})
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=gh_out)):
            notes = get_github_overnight()
        self.assertTrue(len(notes) > 0)
        combined = " ".join(notes)
        self.assertIn("42", combined)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_builds_and_posts_to_slack(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        with patch.object(_mod, "get_weather", return_value="Sunny 72°F"):
            with patch.object(_mod, "get_email_priorities", return_value=[]):
                with patch.object(_mod, "get_calendar_events", return_value=[]):
                    with patch.object(_mod, "get_mail_summary",
                                      return_value={"success": True, "total_unread": 0,
                                                    "important": [], "noise_count": 0}):
                        with patch.object(_mod, "get_github_overnight", return_value=[]):
                            with patch.object(_mod, "get_system_health", return_value=([], 1000)):
                                with patch.object(_mod, "vector_remember"):
                                    _mod.main()
        self.assertTrue(len(posts) > 0)
        _nova_cfg.post_both.side_effect = None

    def test_main_includes_weather_in_slack(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        with patch.object(_mod, "get_weather", return_value="Clear 75°F"):
            with patch.object(_mod, "get_email_priorities", return_value=[]):
                with patch.object(_mod, "get_calendar_events", return_value=[]):
                    with patch.object(_mod, "get_mail_summary",
                                      return_value={"success": False, "total_unread": 0,
                                                    "important": [], "noise_count": 0}):
                        with patch.object(_mod, "get_github_overnight", return_value=[]):
                            with patch.object(_mod, "get_system_health", return_value=([], 0)):
                                with patch.object(_mod, "vector_remember"):
                                    _mod.main()
        self.assertTrue(any("75°F" in p for p in posts))
        _nova_cfg.post_both.side_effect = None

    def test_system_health_issues_appear_in_slack(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        with patch.object(_mod, "get_weather", return_value="Cloudy"):
            with patch.object(_mod, "get_email_priorities", return_value=[]):
                with patch.object(_mod, "get_calendar_events", return_value=[]):
                    with patch.object(_mod, "get_mail_summary",
                                      return_value={"success": False, "total_unread": 0,
                                                    "important": [], "noise_count": 0}):
                        with patch.object(_mod, "get_github_overnight", return_value=[]):
                            with patch.object(_mod, "get_system_health",
                                              return_value=(["NovaControl is down"], 0)):
                                with patch.object(_mod, "vector_remember"):
                                    _mod.main()
        combined = " ".join(posts)
        self.assertIn("NovaControl", combined)
        _nova_cfg.post_both.side_effect = None


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_slack_message_includes_jordan_greeting(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        with patch.object(_mod, "get_weather", return_value="Sunny"):
            with patch.object(_mod, "get_email_priorities", return_value=[]):
                with patch.object(_mod, "get_calendar_events", return_value=[]):
                    with patch.object(_mod, "get_mail_summary",
                                      return_value={"success": True, "total_unread": 0,
                                                    "important": [], "noise_count": 0}):
                        with patch.object(_mod, "get_github_overnight", return_value=[]):
                            with patch.object(_mod, "get_system_health", return_value=([], 0)):
                                with patch.object(_mod, "vector_remember"):
                                    _mod.main()
        combined = " ".join(posts)
        self.assertIn("Jordan", combined)
        _nova_cfg.post_both.side_effect = None

    def test_meetings_shown_when_present(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        with patch.object(_mod, "get_weather", return_value="Sunny"):
            with patch.object(_mod, "get_email_priorities", return_value=[]):
                with patch.object(_mod, "get_calendar_events",
                                  return_value=["10am 1:1 with Team"]):
                    with patch.object(_mod, "get_mail_summary",
                                      return_value={"success": True, "total_unread": 0,
                                                    "important": [], "noise_count": 0}):
                        with patch.object(_mod, "get_github_overnight", return_value=[]):
                            with patch.object(_mod, "get_system_health", return_value=([], 0)):
                                with patch.object(_mod, "vector_remember"):
                                    _mod.main()
        combined = " ".join(posts)
        self.assertIn("1:1", combined)
        _nova_cfg.post_both.side_effect = None

    def test_clean_overnight_message_shown_when_no_issues(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        with patch.object(_mod, "get_weather", return_value="Sunny"):
            with patch.object(_mod, "get_email_priorities", return_value=[]):
                with patch.object(_mod, "get_calendar_events", return_value=[]):
                    with patch.object(_mod, "get_mail_summary",
                                      return_value={"success": True, "total_unread": 0,
                                                    "important": [], "noise_count": 0}):
                        with patch.object(_mod, "get_github_overnight", return_value=[]):
                            with patch.object(_mod, "get_system_health", return_value=([], 1000)):
                                with patch.object(_mod, "vector_remember"):
                                    _mod.main()
        combined = " ".join(posts)
        self.assertIn("green", combined.lower())
        _nova_cfg.post_both.side_effect = None


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

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_key_functions_exist(self):
        for fn in ["main", "get_weather", "get_email_priorities", "get_system_health",
                   "get_mail_summary", "get_github_overnight", "slack_post", "vector_remember"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_module_constants_present(self):
        self.assertIsNotNone(_mod.VECTOR_URL)
        self.assertIsNotNone(_mod.SCRIPTS)
        self.assertIn("18790", _mod.VECTOR_URL)

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))

    def test_log_function_exists(self):
        self.assertTrue(hasattr(_mod, "log"))
        _mod.log("smoke test")  # should not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
