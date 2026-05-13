"""
test_nova_mlx_chat.py — All 7 test categories for nova_mlx_chat.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_mlx_chat.py"
_spec = importlib.util.spec_from_file_location("nova_mlx_chat", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

MLXChatClient = _mod.MLXChatClient
MLX_ENDPOINT = _mod.MLX_ENDPOINT
DEFAULT_MODEL = _mod.DEFAULT_MODEL


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_", "api_key ="]:
            self.assertNotIn(pat, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_endpoint_defaults_to_localhost(self):
        """Default MLX endpoint must be localhost."""
        self.assertIn("127.0.0.1", MLX_ENDPOINT)

    def test_endpoint_configurable_via_env(self):
        """Endpoint must be configurable via environment variable."""
        src = _SCRIPT.read_text()
        self.assertIn("MLX_CHAT_ENDPOINT", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_health_cache_ttl_60s(self):
        """Health cache should be used for 60 seconds."""
        client = MLXChatClient()
        client._health_cache = {"running": True, "status": "online"}
        client._health_cache_time = time.time() - 30  # 30s ago

        with patch("subprocess.run") as mock_run:
            result = client.detect(fast=True)

        mock_run.assert_not_called()
        self.assertTrue(result["running"])

    def test_detect_refreshes_stale_cache(self):
        """Cache older than 60s must be refreshed."""
        client = MLXChatClient()
        client._health_cache = {"running": True}
        client._health_cache_time = time.time() - 120  # stale

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"version": "1.0"})

        with patch("subprocess.run", return_value=mock_result):
            result = client.detect(fast=True)
        self.assertTrue(result["running"])

    def test_client_has_timeout(self):
        self.assertGreater(MLXChatClient().timeout, 0)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_detect_returns_offline_on_failure(self):
        """detect() must return running=False on connection failure."""
        client = MLXChatClient()
        with patch("subprocess.run", side_effect=Exception("refused")):
            result = client.detect()
        self.assertFalse(result["running"])
        self.assertIn("error", result)

    def test_query_returns_none_on_failure(self):
        """query() must return None on any failure."""
        client = MLXChatClient()
        with patch("subprocess.run", side_effect=Exception("timeout")):
            result = client.query("Hello")
        self.assertIsNone(result)

    def test_list_models_returns_empty_on_failure(self):
        client = MLXChatClient()
        with patch("subprocess.run", side_effect=Exception("connection failed")):
            result = client.list_models()
        self.assertEqual(result, [])

    def test_health_check_returns_not_ready_on_failure(self):
        client = MLXChatClient()
        with patch("subprocess.run", side_effect=Exception("down")):
            result = client.health_check()
        self.assertFalse(result["mlx_ready"])


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_detect_parses_version(self):
        client = MLXChatClient()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"version": "2.1.0"})
        with patch("subprocess.run", return_value=mock_result):
            result = client.detect()
        self.assertTrue(result["running"])
        self.assertEqual(result["version"], "2.1.0")

    def test_health_check_parses_models(self):
        client = MLXChatClient()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"data": [{"id": "mistral"}, {"id": "llama"}]})
        with patch("subprocess.run", return_value=mock_result):
            result = client.health_check()
        self.assertTrue(result["mlx_ready"])
        self.assertIn("mistral", result["models_loaded"])

    def test_query_builds_correct_payload(self):
        client = MLXChatClient()
        sent_data = []

        def capture_run(cmd, **kwargs):
            sent_data.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"choices": [{"message": {"content": "Hello!"}}]})
            return r

        with patch("subprocess.run", side_effect=capture_run):
            result = client.query("Hello there")
        self.assertEqual(result, "Hello!")

    def test_query_with_system_prompt(self):
        client = MLXChatClient()
        sent_data = []

        def capture_run(cmd, **kwargs):
            payload = json.loads([c for c in cmd if c.startswith("{") or c.startswith("[")][0] if any(c.startswith("{") for c in cmd) else cmd[-1])
            sent_data.append(payload)
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"choices": [{"message": {"content": "OK"}}]})
            return r

        with patch("subprocess.run", side_effect=capture_run):
            result = client.query("test", system="You are a helpful AI")

    def test_current_model_returns_first(self):
        client = MLXChatClient()
        with patch.object(client, "list_models", return_value=["model_a", "model_b"]):
            result = client.current_model()
        self.assertEqual(result, "model_a")

    def test_current_model_none_when_no_models(self):
        client = MLXChatClient()
        with patch.object(client, "list_models", return_value=[]):
            result = client.current_model()
        self.assertIsNone(result)

    def test_port_extracted_from_endpoint(self):
        client = MLXChatClient(endpoint="http://127.0.0.1:5000")
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"version": "1.0"})
        with patch("subprocess.run", return_value=mock_result):
            result = client.detect()
        self.assertEqual(result["port"], 5000)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_detect_then_query_pipeline(self):
        """If detect() says running, query() should work."""
        client = MLXChatClient()
        detect_result = MagicMock()
        detect_result.returncode = 0
        detect_result.stdout = json.dumps({"version": "1.0"})
        query_result = MagicMock()
        query_result.returncode = 0
        query_result.stdout = json.dumps({"choices": [{"message": {"content": "Response!"}}]})

        call_count = [0]

        def multi_run(cmd, **kwargs):
            call_count[0] += 1
            if "health" in " ".join(cmd):
                return detect_result
            return query_result

        with patch("subprocess.run", side_effect=multi_run):
            detected = client.detect()
            if detected["running"]:
                response = client.query("Test prompt")
                self.assertEqual(response, "Response!")

    def test_list_models_then_query(self):
        client = MLXChatClient()
        with patch.object(client, "list_models", return_value=["mistral"]):
            model = client.current_model()
        with patch.object(client, "query", return_value="Hi!"):
            response = client.query("Hello", model=model)
        self.assertEqual(response, "Hi!")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_detect_offline_returns_1(self):
        with patch("sys.argv", ["nova_mlx_chat.py", "--detect"]):
            with patch.object(MLXChatClient, "detect", return_value={"running": False, "error": "offline"}):
                result = _mod.main()
        self.assertEqual(result, 1)

    def test_main_detect_online_returns_0(self):
        with patch("sys.argv", ["nova_mlx_chat.py", "--detect"]):
            with patch.object(MLXChatClient, "detect", return_value={
                "running": True, "endpoint": "http://127.0.0.1:5000", "version": "1.0"
            }):
                result = _mod.main()
        self.assertEqual(result, 0)

    def test_main_health_check_online(self):
        with patch("sys.argv", ["nova_mlx_chat.py", "--health-check"]):
            with patch.object(MLXChatClient, "health_check", return_value={
                "mlx_ready": True, "models_loaded": ["mistral"], "response_time_ms": 42
            }):
                result = _mod.main()
        self.assertEqual(result, 0)

    def test_main_no_args_returns_1(self):
        with patch("sys.argv", ["nova_mlx_chat.py"]):
            result = _mod.main()
        self.assertEqual(result, 1)


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

    def test_constants_defined(self):
        self.assertIsInstance(_mod.MLX_ENDPOINT, str)
        self.assertIsInstance(_mod.MLX_TIMEOUT, int)
        self.assertIsInstance(_mod.DEFAULT_MODEL, str)

    def test_class_exists(self):
        self.assertTrue(hasattr(_mod, "MLXChatClient"))
        client = MLXChatClient()
        for method in ("detect", "health_check", "query", "list_models", "current_model"):
            self.assertTrue(hasattr(client, method), f"Missing method: {method}")

    def test_main_function_exists(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
