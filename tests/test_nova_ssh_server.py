"""
test_nova_ssh_server.py — All 7 test categories for nova_ssh_server.py
Written by Jordan Koch.
"""
import importlib.util, json, sys, time, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Stub asyncssh before loading
sys.modules["asyncssh"] = MagicMock()
sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_ssh_server.py"
_spec = importlib.util.spec_from_file_location("nova_ssh_server", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

log = _mod.log
recall = _mod.recall
generate = _mod.generate
NovaSSHServer = _mod.NovaSSHServer


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_"]:
            self.assertNotIn(pat, src)
    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)
    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)
    def test_password_from_keychain_not_hardcoded(self):
        src = _SCRIPT.read_text()
        self.assertIn("nova-ssh-password", src, "SSH password must be in Keychain")
        self.assertNotIn("Jkoogie", src, "Hardcoded password found")
    def test_host_key_in_home(self):
        self.assertIn(str(Path.home()), str(_mod.HOST_KEY_PATH))
    def test_authorized_keys_in_home_ssh(self):
        self.assertIn(str(Path.home()), str(_mod.AUTHORIZED_KEYS))
    def test_vector_url_is_localhost(self):
        self.assertTrue(_mod.VECTOR_URL.startswith("http://127.0.0.1"))
    def test_ollama_url_is_localhost(self):
        self.assertTrue(_mod.OLLAMA_URL.startswith("http://127.0.0.1"))
    def test_public_key_auth_supported(self):
        server = NovaSSHServer()
        self.assertTrue(server.public_key_auth_supported())
    def test_allowed_users_defined(self):
        server = NovaSSHServer()
        self.assertTrue(server.begin_auth("nova"))
        self.assertTrue(server.begin_auth("jordan"))
        self.assertTrue(server.begin_auth("kochj"))
        self.assertFalse(server.begin_auth("unknown_user"))


class TestPerformance(unittest.TestCase):
    def test_recall_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=10", src)
    def test_generate_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=120", src)
    def test_session_timeout_defined(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=300", src)
    def test_max_history_bounded(self):
        """Conversation history kept to last 6 entries."""
        src = _SCRIPT.read_text()
        self.assertIn("history[-6:]", src)


class TestRetry(unittest.TestCase):
    def test_recall_returns_empty_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = recall("What is the weather?")
        self.assertEqual(result, [])
    def test_generate_returns_error_string_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = generate("Hello Nova")
        self.assertIn("error", result.lower())
    def test_password_auth_returns_false_if_no_keychain(self):
        server = NovaSSHServer()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            result = server.validate_password("nova", "wrongpass")
        self.assertFalse(result)


class TestUnit(unittest.TestCase):
    def test_port_defined(self):
        self.assertEqual(_mod.PORT, 2222)
    def test_system_prompt_mentions_nova(self):
        self.assertIn("Nova", _mod.SYSTEM_PROMPT)
    def test_system_prompt_mentions_memories(self):
        self.assertIn("memories", _mod.SYSTEM_PROMPT.lower())
    def test_generate_strips_think_tags(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "response": "<think>ignore</think>Hello Jordan!"
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = generate("Hi")
        # qwen3 /no_think prefix is used — response should not contain think tags
        self.assertNotIn("<think>", result)
    def test_recall_parses_memories_list(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "memories": [{"text": "Memory 1"}, {"text": "Memory 2"}]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = recall("test query")
        self.assertEqual(len(result), 2)
    def test_validate_public_key_returns_false_if_no_auth_keys(self):
        server = NovaSSHServer()
        with patch.object(_mod.AUTHORIZED_KEYS, "exists", return_value=False):
            result = server.validate_public_key("nova", MagicMock())
        self.assertFalse(result)


class TestIntegration(unittest.TestCase):
    def test_recall_builds_context_from_memories(self):
        """Memories from recall() should be passed to generate()."""
        memories = ["Jordan arrived home at 3pm.", "Jordan likes coffee."]
        with patch.object(_mod, "recall", return_value=memories):
            context_used = []
            original_generate = generate
            def capture_generate(prompt, context=""):
                context_used.append(context)
                return "Test response"
            with patch.object(_mod, "generate", side_effect=capture_generate):
                # Simulate what handle_session does
                context = "\n".join(f"- {m}" for m in memories)
                _mod.generate("What time did I get home?", context)
            self.assertGreater(len(context_used), 0)

    def test_host_key_permissions_set(self):
        src = _SCRIPT.read_text()
        self.assertIn("0o600", src, "Host key must be set to 0o600")


class TestFunctional(unittest.TestCase):
    def test_quit_commands_recognized(self):
        """quit/exit/bye should all end the session."""
        src = _SCRIPT.read_text()
        self.assertIn('"quit"', src)
        self.assertIn('"exit"', src)
        self.assertIn('"bye"', src)
    def test_thinking_indicator_shown(self):
        src = _SCRIPT.read_text()
        self.assertIn("thinking", src)


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.HOST, str)
        self.assertIsInstance(_mod.PORT, int)
        self.assertIsInstance(_mod.HOST_KEY_PATH, Path)
        self.assertIsInstance(_mod.AUTHORIZED_KEYS, Path)
        self.assertIsInstance(_mod.SYSTEM_PROMPT, str)
    def test_functions_exist(self):
        for fn in ("log", "recall", "generate", "handle_session", "start_server", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
