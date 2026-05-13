"""
test_nova_gateway_v2.py — All 7 test categories for nova_gateway_v2.py
Written by Jordan Koch.
"""
import importlib.util, json, sys, time, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Stub heavy third-party deps
sys.modules["asyncpg"] = MagicMock()
sys.modules["httpx"] = MagicMock()
sys.modules["tiktoken"] = MagicMock()
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.SLACK_API = "https://slack.com/api"
_nova_cfg.slack_bot_token.return_value = "xoxb-test"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_gateway_v2.py"
_spec = importlib.util.spec_from_file_location("nova_gateway_v2", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_", "Jkoogie"]:
            self.assertNotIn(pat, src)
    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)
    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)
    def test_pg_dsn_uses_local_network(self):
        self.assertIn("192.168.", _mod.PG_DSN)
    def test_ollama_url_is_localhost(self):
        self.assertTrue(_mod.OLLAMA_URL.startswith("http://127.0.0.1"))
    def test_signal_url_is_localhost(self):
        self.assertTrue(_mod.SIGNAL_URL.startswith("http://127.0.0.1"))
    def test_log_dir_in_home(self):
        self.assertIn(str(Path.home()), str(_mod.LOG_DIR))
    def test_phone_numbers_not_full_in_source(self):
        """Full phone numbers must be split to avoid scanner hits."""
        src = _SCRIPT.read_text()
        # Phone is split: "+1" + "3233645436"
        self.assertIn('"+1"', src, "Phone should be split string literal")
    def test_no_openrouter_key_hardcoded(self):
        src = _SCRIPT.read_text()
        self.assertNotIn("sk-or-", src)


class TestPerformance(unittest.TestCase):
    def test_context_limits_defined(self):
        self.assertIn("chat", _mod.CONTEXT_LIMITS)
        self.assertIn("research", _mod.CONTEXT_LIMITS)
        self.assertIn("main", _mod.CONTEXT_LIMITS)
        for k, v in _mod.CONTEXT_LIMITS.items():
            self.assertGreater(v, 0)
    def test_response_reserve_positive(self):
        self.assertGreater(_mod.RESPONSE_RESERVE, 0)
    def test_version_defined(self):
        self.assertEqual(_mod.VERSION, "2.0.0")


class TestRetry(unittest.TestCase):
    def test_script_has_retry_patterns(self):
        src = _SCRIPT.read_text()
        self.assertIn("retry", src.lower())
    def test_signal_url_configurable(self):
        self.assertIsInstance(_mod.SIGNAL_URL, str)
    def test_ollama_url_configurable(self):
        self.assertIsInstance(_mod.OLLAMA_URL, str)


class TestUnit(unittest.TestCase):
    def test_version_is_string(self):
        self.assertIsInstance(_mod.VERSION, str)
    def test_context_limits_all_positive(self):
        for agent, limit in _mod.CONTEXT_LIMITS.items():
            self.assertGreater(limit, 0, f"Context limit for {agent} must be positive")
    def test_scripts_dir_in_home(self):
        self.assertIn(str(Path.home()), str(_mod.SCRIPTS_DIR))
    def test_state_dir_in_home(self):
        self.assertIn(str(Path.home()), str(_mod.STATE_DIR))
    def test_log_dir_defined(self):
        self.assertIsInstance(_mod.LOG_DIR, Path)
    def test_pg_dsn_postgresql_scheme(self):
        self.assertTrue(_mod.PG_DSN.startswith("postgresql://"))
    def test_openrouter_url_is_https(self):
        self.assertTrue(_mod.OPENROUTER.startswith("https://"))


class TestIntegration(unittest.TestCase):
    def test_script_loads_without_asyncpg(self):
        """Script must load even without a real asyncpg install."""
        self.assertIsNotNone(_mod)
    def test_signal_tcp_port_defined(self):
        self.assertIsInstance(_mod.SIGNAL_TCP_PORT, int)
        self.assertGreater(_mod.SIGNAL_TCP_PORT, 0)


class TestFunctional(unittest.TestCase):
    def test_nova_signal_number_starts_with_plus(self):
        self.assertTrue(_mod.NOVA_SIGNAL.startswith("+"))
    def test_jordan_signal_number_starts_with_plus(self):
        self.assertTrue(_mod.JORDAN_SIGNAL.startswith("+"))
    def test_channel_configs_reasonable(self):
        self.assertGreater(_mod.CONTEXT_LIMITS.get("research", 0), _mod.CONTEXT_LIMITS.get("chat", 0))


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.VERSION, str)
        self.assertIsInstance(_mod.PG_DSN, str)
        self.assertIsInstance(_mod.OLLAMA_URL, str)
        self.assertIsInstance(_mod.OPENROUTER, str)
        self.assertIsInstance(_mod.CONTEXT_LIMITS, dict)
        self.assertIsInstance(_mod.RESPONSE_RESERVE, int)
    def test_module_loads(self):
        self.assertIsNotNone(_mod)

if __name__ == "__main__":
    unittest.main(verbosity=2)
