"""
test_nova_status_update.py — All 7 test categories for nova_status_update.py
Written by Jordan Koch.
"""
import importlib.util, json, sys, time, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_status_update.py"
_spec = importlib.util.spec_from_file_location("nova_status_update", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

check = _mod.check
app_status = _mod.app_status


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
    def test_status_file_in_workspace(self):
        self.assertIn(str(Path.home()), str(_mod.STATUS_FILE))
    def test_endpoints_are_local(self):
        src = _SCRIPT.read_text()
        self.assertIn("127.0.0.1", src)


class TestPerformance(unittest.TestCase):
    def test_check_returns_quickly_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            start = time.perf_counter()
            result = check("http://127.0.0.1:18790/health", timeout=1)
            elapsed = time.perf_counter() - start
        self.assertEqual(result, {})
        self.assertLess(elapsed, 2.0)
    def test_check_has_default_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=3", src)
    def test_app_status_returns_string(self):
        with patch.object(_mod, "check", return_value={}):
            result = app_status(37400, "TestApp")
        self.assertIsInstance(result, str)


class TestRetry(unittest.TestCase):
    def test_check_returns_empty_on_network_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = check("http://127.0.0.1:18790/health")
        self.assertEqual(result, {})
    def test_check_returns_empty_on_invalid_json(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = check("http://127.0.0.1:18790/health")
        self.assertEqual(result, {})
    def test_main_handles_all_services_down(self):
        """main() must complete even if all services are unreachable."""
        with patch.object(_mod, "check", return_value={}):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout="")
                with tempfile.TemporaryDirectory() as tmpdir:
                    with patch.object(_mod, "STATUS_FILE", Path(tmpdir) / "STATUS.md"):
                        _mod.main()
                self.assertTrue((Path(tmpdir) / "STATUS.md").exists())


class TestUnit(unittest.TestCase):
    def test_check_parses_json_response(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"status": "ok", "count": 42}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = check("http://127.0.0.1:18790/health")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["count"], 42)
    def test_app_status_running(self):
        with patch.object(_mod, "check", return_value={"status": "running"}):
            result = app_status(37400, "NovaControl")
        self.assertIn("NovaControl", result)
        self.assertIn("running", result)
    def test_app_status_not_running(self):
        with patch.object(_mod, "check", return_value={}):
            result = app_status(9999, "DeadApp")
        self.assertIn("DeadApp", result)
        self.assertIn("not running", result)
    def test_status_file_contains_required_sections(self):
        with patch.object(_mod, "check", return_value={}):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout="")
                with tempfile.TemporaryDirectory() as tmpdir:
                    with patch.object(_mod, "STATUS_FILE", Path(tmpdir) / "STATUS.md"):
                        _mod.main()
                        content = (Path(tmpdir) / "STATUS.md").read_text()
        self.assertIn("Memory System", content)
        self.assertIn("Ollama", content)
        self.assertIn("OpenClaw", content)


class TestIntegration(unittest.TestCase):
    def test_main_writes_status_file(self):
        with patch.object(_mod, "check", return_value={"status": "ok", "count": 1000, "model": "qwen3"}):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps({"enabled": True, "jobs": 42}))
                with tempfile.TemporaryDirectory() as tmpdir:
                    with patch.object(_mod, "STATUS_FILE", Path(tmpdir) / "STATUS.md"):
                        _mod.main()
                        content = (Path(tmpdir) / "STATUS.md").read_text()
        self.assertIn("Nova System Status", content)
        self.assertIn("Updated:", content)


class TestFunctional(unittest.TestCase):
    def test_main_marks_memory_online(self):
        with patch.object(_mod, "check", side_effect=lambda url, **kw:
            {"status": "ok", "count": 500, "model": "qwen3"} if "18790" in url else {}):
            with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
                with tempfile.TemporaryDirectory() as tmpdir:
                    with patch.object(_mod, "STATUS_FILE", Path(tmpdir) / "STATUS.md"):
                        _mod.main()
                        content = (Path(tmpdir) / "STATUS.md").read_text()
        self.assertIn("ONLINE", content)
    def test_main_marks_memory_down_when_no_health(self):
        with patch.object(_mod, "check", return_value={}):
            with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
                with tempfile.TemporaryDirectory() as tmpdir:
                    with patch.object(_mod, "STATUS_FILE", Path(tmpdir) / "STATUS.md"):
                        _mod.main()
                        content = (Path(tmpdir) / "STATUS.md").read_text()
        self.assertIn("DOWN", content)


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.WORKSPACE, Path)
        self.assertIsInstance(_mod.STATUS_FILE, Path)
    def test_functions_exist(self):
        for fn in ("check", "app_status", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
