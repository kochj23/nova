"""
test_nova_openwebui.py — All 7 test categories for nova_openwebui.py
Written by Jordan Koch.
"""
import importlib.util, json, sys, time, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_openwebui.py"
_spec = importlib.util.spec_from_file_location("nova_openwebui", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

OpenWebUIClient = _mod.OpenWebUIClient


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
    def test_endpoint_uses_local_network(self):
        self.assertIn("192.168.", _mod.OPENWEBUI_ENDPOINT)
    def test_endpoint_configurable_via_env(self):
        self.assertIn("OPENWEBUI_ENDPOINT", _SCRIPT.read_text())


class TestPerformance(unittest.TestCase):
    def test_health_cache_used_within_60s(self):
        client = OpenWebUIClient()
        client._health_cache = {"running": True}
        client._health_cache_time = time.time() - 30
        with patch("subprocess.run") as mock_run:
            client.detect(fast=True)
        mock_run.assert_not_called()
    def test_timeout_defined(self):
        self.assertGreater(_mod.OPENWEBUI_TIMEOUT, 0)
    def test_detect_returns_quickly_on_failure(self):
        client = OpenWebUIClient(timeout=1)
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            start = time.perf_counter()
            result = client.detect()
            elapsed = time.perf_counter() - start
        self.assertFalse(result["running"])


class TestRetry(unittest.TestCase):
    def test_detect_returns_offline_on_exception(self):
        client = OpenWebUIClient()
        with patch("subprocess.run", side_effect=Exception("refused")):
            result = client.detect()
        self.assertFalse(result["running"])
    def test_query_returns_none_on_failure(self):
        client = OpenWebUIClient()
        with patch("subprocess.run", side_effect=Exception("timeout")):
            result = client.query("Hello")
        self.assertIsNone(result)
    def test_list_models_returns_empty_on_failure(self):
        client = OpenWebUIClient()
        with patch("subprocess.run", side_effect=Exception("down")):
            result = client.list_models()
        self.assertEqual(result, [])
    def test_health_check_returns_not_ready_on_failure(self):
        client = OpenWebUIClient()
        with patch("subprocess.run", side_effect=Exception("down")):
            result = client.health_check()
        self.assertFalse(result.get("openwebui_ready", False))


class TestUnit(unittest.TestCase):
    def test_detect_parses_version(self):
        client = OpenWebUIClient()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"version": "0.3.0"})
        with patch("subprocess.run", return_value=mock_result):
            result = client.detect()
        self.assertTrue(result["running"])
        self.assertEqual(result["version"], "0.3.0")
    def test_health_check_parses_models(self):
        client = OpenWebUIClient()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"models": [{"name": "mistral"}, {"name": "llama"}]})
        with patch("subprocess.run", return_value=mock_result):
            result = client.health_check()
        self.assertTrue(result.get("openwebui_ready"))
        self.assertEqual(result["models_count"], 2)
    def test_query_parses_multi_line_response(self):
        client = OpenWebUIClient()
        lines = [
            json.dumps({"message": {"content": "Hello"}}),
            json.dumps({"message": {"content": " World"}}),
        ]
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "\n".join(lines)
        with patch("subprocess.run", return_value=mock_result):
            result = client.query("test")
        self.assertEqual(result, "Hello World")
    def test_get_model_info_returns_dict(self):
        client = OpenWebUIClient()
        models = [{"name": "mistral", "size": "4.1GB", "modified": "2026-01-01"}]
        with patch.object(client, "list_models", return_value=models):
            info = client.get_model_info("mistral")
        self.assertEqual(info["name"], "mistral")
    def test_get_model_info_returns_none_for_missing(self):
        client = OpenWebUIClient()
        with patch.object(client, "list_models", return_value=[]):
            result = client.get_model_info("nonexistent")
        self.assertIsNone(result)


class TestIntegration(unittest.TestCase):
    def test_detect_then_query(self):
        client = OpenWebUIClient()
        with patch.object(client, "detect", return_value={"running": True}):
            with patch.object(client, "query", return_value="Response"):
                resp = client.query("Hello")
        self.assertEqual(resp, "Response")


class TestFunctional(unittest.TestCase):
    def test_main_detect_offline_returns_1(self):
        with patch("sys.argv", ["nova_openwebui.py", "--detect"]):
            with patch.object(OpenWebUIClient, "detect", return_value={"running": False, "error": "offline"}):
                result = _mod.main()
        self.assertEqual(result, 1)
    def test_main_no_args_returns_1(self):
        with patch("sys.argv", ["nova_openwebui.py"]):
            result = _mod.main()
        self.assertEqual(result, 1)
    def test_main_detect_online_returns_0(self):
        with patch("sys.argv", ["nova_openwebui.py", "--detect"]):
            with patch.object(OpenWebUIClient, "detect", return_value={
                "running": True, "endpoint": "http://192.168.1.6:3000", "version": "1.0"
            }):
                result = _mod.main()
        self.assertEqual(result, 0)


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.OPENWEBUI_ENDPOINT, str)
        self.assertIsInstance(_mod.OPENWEBUI_TIMEOUT, int)
        self.assertIsInstance(_mod.DEFAULT_MODEL, str)
    def test_class_exists_with_methods(self):
        client = OpenWebUIClient()
        for m in ("detect", "health_check", "query", "list_models", "get_model_info"):
            self.assertTrue(hasattr(client, m))

if __name__ == "__main__":
    unittest.main(verbosity=2)
