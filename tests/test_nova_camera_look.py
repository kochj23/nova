"""
test_nova_camera_look.py — All 7 test categories for nova_camera_look.py
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

# Stub heavy / unavailable deps before loading
_nova_protect = MagicMock()
sys.modules["nova_protect_monitor"] = _nova_protect
sys.modules["nova_protect_monitor"].ProtectClient = MagicMock

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_camera_look.py"
_spec = importlib.util.spec_from_file_location("nova_camera_look", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

get_cameras = _mod.get_cameras
fuzzy_match = _mod.fuzzy_match
take_snapshot = _mod.take_snapshot
describe_image = _mod.describe_image


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials_in_source(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "Bearer ", "token ="]:
            self.assertNotIn(pat, src, f"Potential credential: {pat!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src, "Hardcoded home path found")

    def test_no_pii_emails_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com",
                    "user" + _at + "example-corp.com"]:
            self.assertNotIn(pat, src, f"PII email found: {pat!r}")

    def test_interior_cameras_filtered_out(self):
        """get_cameras must exclude Interior-prefix cameras."""
        mock_client = MagicMock()
        mock_client.get_bootstrap.return_value = {
            "cameras": [
                {"name": "Interior - Living Room", "state": "CONNECTED", "id": "1"},
                {"name": "Front Door", "state": "CONNECTED", "id": "2"},
                {"name": "Interior - Bedroom", "state": "CONNECTED", "id": "3"},
            ]
        }
        result = get_cameras(mock_client)
        names = [c["name"] for c in result]
        self.assertNotIn("Interior - Living Room", names)
        self.assertNotIn("Interior - Bedroom", names)
        self.assertIn("Front Door", names)

    def test_snapshot_cleanup_on_describe(self):
        """Snapshot images must be deleted after description."""
        src = _SCRIPT.read_text()
        self.assertIn("os.unlink", src, "Snapshot cleanup not found in source")

    def test_ollama_url_is_localhost(self):
        """Vision model must use local Ollama, not cloud."""
        self.assertTrue(
            _mod.OLLAMA_URL.startswith("http://127.0.0.1") or
            _mod.OLLAMA_URL.startswith("http://localhost"),
            "OLLAMA_URL must point to localhost, not cloud"
        )


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_fuzzy_match_fast_on_many_cameras(self):
        """fuzzy_match must complete in <10ms for 50 cameras."""
        cameras = [{"name": f"Camera {i}", "id": str(i)} for i in range(50)]
        cameras.append({"name": "Front Door Patio", "id": "99"})
        start = time.perf_counter()
        result = fuzzy_match("patio", cameras)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.01, f"fuzzy_match took {elapsed:.4f}s (limit 10ms)")
        self.assertIsNotNone(result)

    def test_get_cameras_handles_empty_bootstrap(self):
        """get_cameras must return [] immediately if bootstrap is empty."""
        mock_client = MagicMock()
        mock_client.get_bootstrap.return_value = {}
        start = time.perf_counter()
        result = get_cameras(mock_client)
        elapsed = time.perf_counter() - start
        self.assertEqual(result, [])
        self.assertLess(elapsed, 0.01)

    def test_snapshot_dir_constant_defined(self):
        """SNAPSHOT_DIR must be a Path under home."""
        sd = _mod.SNAPSHOT_DIR
        self.assertIsInstance(sd, Path)
        self.assertTrue(str(sd).startswith(str(Path.home())))


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_describe_image_returns_error_string_on_failure(self):
        """describe_image must not raise — return error string instead."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"FAKEJPEG")
            tmp = f.name
        try:
            with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
                result = describe_image(tmp, "Test Cam")
            self.assertIn("Vision analysis failed", result)
        finally:
            os.unlink(tmp)

    def test_take_snapshot_returns_none_on_failure(self):
        """take_snapshot must return None when client fails."""
        mock_client = MagicMock()
        mock_client.get_snapshot.return_value = False
        with patch.object(_mod, "SNAPSHOT_DIR", Path("/tmp/test_snapshots")):
            result = take_snapshot(mock_client, "abc12345", "Test Camera")
        self.assertIsNone(result)

    def test_get_cameras_returns_empty_on_bootstrap_failure(self):
        """get_cameras must return [] if get_bootstrap returns None."""
        mock_client = MagicMock()
        mock_client.get_bootstrap.return_value = None
        result = get_cameras(mock_client)
        self.assertEqual(result, [])


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_fuzzy_match_exact_substring(self):
        cameras = [{"name": "Front Door", "id": "1"}, {"name": "Back Patio", "id": "2"}]
        result = fuzzy_match("patio", cameras)
        self.assertEqual(result["name"], "Back Patio")

    def test_fuzzy_match_word_match(self):
        cameras = [{"name": "Alley North", "id": "1"}, {"name": "Side Yard", "id": "2"}]
        result = fuzzy_match("alley", cameras)
        self.assertEqual(result["id"], "1")

    def test_fuzzy_match_no_match_returns_none(self):
        cameras = [{"name": "Front Door", "id": "1"}]
        result = fuzzy_match("rooftop", cameras)
        self.assertIsNone(result)

    def test_fuzzy_match_case_insensitive(self):
        cameras = [{"name": "Carport Camera", "id": "5"}]
        result = fuzzy_match("CARPORT", cameras)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "5")

    def test_get_cameras_excludes_disconnected(self):
        mock_client = MagicMock()
        mock_client.get_bootstrap.return_value = {
            "cameras": [
                {"name": "Front Door", "state": "DISCONNECTED", "id": "1"},
                {"name": "Back Patio", "state": "CONNECTED", "id": "2"},
            ]
        }
        result = get_cameras(mock_client)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Back Patio")

    def test_interior_prefix_constant(self):
        self.assertEqual(_mod.INTERIOR_PREFIX, "Interior")

    def test_vision_model_constant_defined(self):
        self.assertIsInstance(_mod.VISION_MODEL, str)
        self.assertGreater(len(_mod.VISION_MODEL), 0)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_get_cameras_then_fuzzy_match_pipeline(self):
        """Full pipeline: get cameras then fuzzy match."""
        mock_client = MagicMock()
        mock_client.get_bootstrap.return_value = {
            "cameras": [
                {"name": "Front Yard", "state": "CONNECTED", "id": "10"},
                {"name": "Interior - Garage", "state": "CONNECTED", "id": "11"},
            ]
        }
        cameras = get_cameras(mock_client)
        self.assertEqual(len(cameras), 1)  # Interior filtered out
        result = fuzzy_match("front", cameras)
        self.assertEqual(result["id"], "10")

    def test_describe_image_calls_ollama(self):
        """describe_image must call urlopen with Ollama URL."""
        import tempfile
        calls = []

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"\xff\xd8\xff" + b"\x00" * 100)  # minimal JPEG header
            tmp = f.name
        try:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"response": "A person is visible."}).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)

            def capture_urlopen(req, timeout=None):
                calls.append(req.full_url)
                return mock_resp

            with patch("urllib.request.urlopen", side_effect=capture_urlopen):
                result = describe_image(tmp, "Front Door")
            self.assertGreater(len(calls), 0)
            self.assertIn("11434", calls[0])
            self.assertIn("person", result.lower())
        finally:
            os.unlink(tmp)

    def test_snapshot_creates_file_in_snapshot_dir(self):
        """take_snapshot should write to SNAPSHOT_DIR."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_client = MagicMock()
            mock_client.get_snapshot.return_value = True

            with patch.object(_mod, "SNAPSHOT_DIR", Path(tmpdir)):
                result = take_snapshot(mock_client, "abc12345", "Front Yard")

            self.assertIsNotNone(result)
            self.assertIn("abc12345"[:8], result)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_list_mode_prints_camera_names(self):
        """--list should print camera names without crashing."""
        mock_client = MagicMock()
        mock_client.login.return_value = True
        mock_client.get_bootstrap.return_value = {
            "cameras": [{"name": "Front Door", "state": "CONNECTED", "id": "1"}]
        }
        with patch("sys.argv", ["nova_camera_look.py", "--list"]):
            with patch.object(_mod, "ProtectClient", return_value=mock_client):
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    _mod.main()
                output = buf.getvalue()
        self.assertIn("Front Door", output)

    def test_main_exits_if_no_camera_match(self):
        """main should sys.exit(1) if no camera matches query."""
        mock_client = MagicMock()
        mock_client.login.return_value = True
        mock_client.get_bootstrap.return_value = {
            "cameras": [{"name": "Front Door", "state": "CONNECTED", "id": "1"}]
        }
        with patch("sys.argv", ["nova_camera_look.py", "rooftop_nonexistent"]):
            with patch.object(_mod, "ProtectClient", return_value=mock_client):
                with self.assertRaises(SystemExit) as ctx:
                    _mod.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_main_exits_if_login_fails(self):
        """main must exit if ProtectClient.login() fails."""
        mock_client = MagicMock()
        mock_client.login.return_value = False
        with patch("sys.argv", ["nova_camera_look.py", "front"]):
            with patch.object(_mod, "ProtectClient", return_value=mock_client):
                with self.assertRaises(SystemExit):
                    _mod.main()


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

    def test_module_constants_defined(self):
        self.assertIsInstance(_mod.INTERIOR_PREFIX, str)
        self.assertIsInstance(_mod.OLLAMA_URL, str)
        self.assertIsInstance(_mod.VISION_MODEL, str)
        self.assertIsInstance(_mod.SNAPSHOT_DIR, Path)

    def test_functions_exist(self):
        for fn in ("get_cameras", "fuzzy_match", "take_snapshot", "describe_image", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing function: {fn}")

    def test_snapshot_dir_under_home(self):
        self.assertTrue(str(_mod.SNAPSHOT_DIR).startswith(str(Path.home())))


if __name__ == "__main__":
    unittest.main(verbosity=2)
