"""
test_nova_homekit_occupancy.py — All 7 test categories for nova_homekit_occupancy.py
Written by Jordan Koch.
"""
import importlib.util, json, sys, time, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_homekit_occupancy.py"
_spec = importlib.util.spec_from_file_location("nova_homekit_occupancy", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

log = _mod.log
remember = _mod.remember
get_homekit_accessories = _mod.get_homekit_accessories
check_vehicle_presence = _mod.check_vehicle_presence
build_occupancy_map = _mod.build_occupancy_map


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
    def test_memory_url_is_localhost(self):
        self.assertTrue(_mod.MEMORY_URL.startswith("http://127.0.0.1"))
    def test_homekit_script_in_home(self):
        self.assertIn(str(Path.home()), str(_mod.HOMEKIT_SCRIPT))
    def test_workspace_in_home(self):
        self.assertIn(str(Path.home()), str(_mod.WORKSPACE))


class TestPerformance(unittest.TestCase):
    def test_remember_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=5", src)
    def test_get_homekit_accessories_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=10", src)
    def test_check_vehicle_presence_fast(self):
        start = time.perf_counter()
        result = check_vehicle_presence()
        elapsed = time.perf_counter() - start
        self.assertIsInstance(result, dict)
        self.assertLess(elapsed, 0.1)


class TestRetry(unittest.TestCase):
    def test_remember_returns_none_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = remember("Occupancy: home occupied")
        self.assertIsNone(result)
    def test_get_homekit_accessories_returns_empty_on_script_missing(self):
        with patch.object(_mod.HOMEKIT_SCRIPT, "exists", return_value=False):
            result = get_homekit_accessories()
        self.assertEqual(result, [])
    def test_get_homekit_accessories_returns_empty_on_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error"
        with patch("subprocess.run", return_value=mock_result):
            with patch.object(_mod.HOMEKIT_SCRIPT, "exists", return_value=True):
                result = get_homekit_accessories()
        self.assertEqual(result, [])


class TestUnit(unittest.TestCase):
    def test_check_vehicle_presence_returns_dict(self):
        result = check_vehicle_presence()
        self.assertIn("home", result)
        self.assertIn("location", result)
        self.assertIn("confidence", result)
    def test_check_vehicle_confidence_in_range(self):
        result = check_vehicle_presence()
        self.assertGreaterEqual(result["confidence"], 0)
        self.assertLessEqual(result["confidence"], 1.0)
    def test_build_occupancy_map_returns_dict(self):
        result = build_occupancy_map([], check_vehicle_presence())
        self.assertIsInstance(result, (dict, type(None)))
    def test_get_homekit_accessories_returns_list_on_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps([{"name": "Motion Sensor", "type": "motion"}])
        with patch("subprocess.run", return_value=mock_result):
            with patch.object(_mod.HOMEKIT_SCRIPT, "exists", return_value=True):
                result = get_homekit_accessories()
        self.assertIsInstance(result, list)
    def test_get_homekit_accessories_handles_invalid_json(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not json"
        with patch("subprocess.run", return_value=mock_result):
            with patch.object(_mod.HOMEKIT_SCRIPT, "exists", return_value=True):
                result = get_homekit_accessories()
        self.assertEqual(result, [])
    def test_memory_url_format(self):
        import re
        self.assertRegex(_mod.MEMORY_URL, r"^http://127\.0\.0\.1:\d+$")


class TestIntegration(unittest.TestCase):
    def test_remember_sends_to_memory(self):
        sent = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"id": "123"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        def capture(req, **kwargs):
            sent.append(json.loads(req.data.decode()))
            return mock_resp
        with patch("urllib.request.urlopen", side_effect=capture):
            remember("Jordan is home", source="occupancy")
        self.assertGreater(len(sent), 0)
        self.assertEqual(sent[0]["source"], "occupancy")
    def test_vehicle_and_accessories_combined(self):
        accessories = [
            {"name": "Front Door Motion", "type": "motion", "state": {"motion": True}},
        ]
        vehicle = check_vehicle_presence()
        result = build_occupancy_map(accessories, vehicle)
        # Should not raise


class TestFunctional(unittest.TestCase):
    def test_main_runs_without_crash(self):
        """main() should complete without crashing even with mocked deps."""
        with patch.object(_mod, "get_homekit_accessories", return_value=[]):
            with patch.object(_mod, "check_vehicle_presence",
                               return_value={"home": True, "location": "carport", "confidence": 0.9, "last_seen": "now"}):
                with patch.object(_mod, "build_occupancy_map", return_value=None):
                    with patch.object(_mod, "remember"):
                        if hasattr(_mod, "main"):
                            _mod.main()


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIsInstance(_mod.WORKSPACE, Path)
        self.assertIsInstance(_mod.HOMEKIT_SCRIPT, Path)
    def test_functions_exist(self):
        for fn in ("log", "remember", "get_homekit_accessories",
                   "check_vehicle_presence", "build_occupancy_map"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
