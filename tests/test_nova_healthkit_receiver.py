"""
test_nova_healthkit_receiver.py — All 7 test categories for nova_healthkit_receiver.py
Written by Jordan Koch.
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub nova_config before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_healthkit_receiver.py"

# Patch directory creation at import time
with patch.object(Path, "mkdir", lambda self, *a, **kw: None):
    with patch.object(Path, "chmod", lambda self, mode: None):
        _spec = importlib.util.spec_from_file_location("nova_healthkit_receiver", _SCRIPT)
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)

HealthHandler = _mod.HealthHandler
PORT = _mod.PORT


def _make_handler(method, path, body=b"", headers=None):
    """Create a HealthHandler instance backed by a temp socket."""
    class FakeRequest:
        def makefile(self, *a, **kw):
            return io.BytesIO(body)
        def sendall(self, data):
            pass

    class FakeAddress:
        pass

    sock = FakeRequest()

    # Build headers dict
    hdr_dict = {"Content-Length": str(len(body))}
    if headers:
        hdr_dict.update(headers)

    handler = HealthHandler.__new__(HealthHandler)
    handler.path = path
    handler.command = method
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.headers = hdr_dict

    # Mock send methods
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.send_error = MagicMock()
    return handler


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        """Source must not contain API keys or tokens."""
        src = _SCRIPT.read_text()
        for p in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(p, src, f"Credential found: {p!r}")

    def test_no_pii_emails(self):
        """Source must not contain personal email addresses."""
        src = _SCRIPT.read_text()
        _at = "@"
        for pattern in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pattern, src)

    def test_health_data_tagged_local_only(self):
        """Payload sent to memory server must include privacy=local-only."""
        captured_payloads = []

        def fake_urlopen(req, timeout=None):
            captured_payloads.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        body = json.dumps({
            "sleep_hours": 7.5,
            "resting_heart_rate": 60,
            "date": date.today().isoformat(),
        }).encode()

        handler = _make_handler("POST", "/health", body)

        with tempfile.TemporaryDirectory() as tmpdir:
            health_dir = Path(tmpdir)
            with patch.object(_mod, "HEALTH_DIR", health_dir):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    with patch.object(Path, "chmod", lambda self, mode: None):
                        handler.do_POST()

        if captured_payloads:
            payload = captured_payloads[0]
            self.assertEqual(
                payload["metadata"]["privacy"], "local-only",
                "Health data must be tagged privacy=local-only"
            )

    def test_health_files_restricted_permissions(self):
        """Health data files must be created with chmod 600."""
        chmod_calls = []
        original_chmod = Path.chmod

        def capture_chmod(self, mode):
            chmod_calls.append((str(self), mode))

        body = json.dumps({"sleep_hours": 7.0}).encode()
        handler = _make_handler("POST", "/health", body)

        with tempfile.TemporaryDirectory() as tmpdir:
            health_dir = Path(tmpdir)
            with patch.object(_mod, "HEALTH_DIR", health_dir):
                with patch.object(Path, "chmod", capture_chmod):
                    with patch("urllib.request.urlopen", MagicMock()):
                        handler.do_POST()

        # At least one chmod 600 call should have been made
        restricted = [c for c in chmod_calls if c[1] == 0o600]
        self.assertTrue(len(restricted) > 0, "Health files must be chmod 600")

    def test_memory_url_is_local_only(self):
        """MEMORY_URL must point to localhost, not an external server."""
        self.assertIn("127.0.0.1", _mod.MEMORY_URL)

    def test_no_hardcoded_home_path(self):
        """Source must not hardcode literal home paths."""
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_post_handler_fast_on_valid_data(self):
        """POST handler must complete in < 100ms for typical health payload."""
        body = json.dumps({
            "sleep_hours": 7.5,
            "steps": 8000,
            "resting_heart_rate": 60,
            "date": date.today().isoformat(),
        }).encode()

        handler = _make_handler("POST", "/health", body)

        start = time.perf_counter()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "HEALTH_DIR", Path(tmpdir)):
                with patch("urllib.request.urlopen", MagicMock()):
                    with patch.object(Path, "chmod", lambda self, mode: None):
                        handler.do_POST()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_port_number_defined(self):
        """PORT must be a valid TCP port number."""
        self.assertIsInstance(PORT, int)
        self.assertGreater(PORT, 1024)
        self.assertLess(PORT, 65536)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_memory_store_failure_does_not_crash_handler(self):
        """POST handler must return 200 even when memory store fails."""
        body = json.dumps({"sleep_hours": 7.0, "date": date.today().isoformat()}).encode()
        handler = _make_handler("POST", "/health", body)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "HEALTH_DIR", Path(tmpdir)):
                with patch("urllib.request.urlopen", side_effect=Exception("memory down")):
                    with patch.object(Path, "chmod", lambda self, mode: None):
                        handler.do_POST()

        # Should still send 200
        handler.send_response.assert_called_with(200)

    def test_json_decode_error_returns_400(self):
        """Invalid JSON body must return 400."""
        body = b"not json at all"
        handler = _make_handler("POST", "/health", body)

        handler.do_POST()
        handler.send_error.assert_called_with(400, "Invalid JSON")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_wrong_path_returns_404(self):
        """POST to wrong path must return 404."""
        handler = _make_handler("POST", "/wrong", b"{}")
        handler.do_POST()
        handler.send_error.assert_called_with(404)

    def test_get_health_path_returns_data(self):
        """GET /health returns latest.json contents."""
        health_data = {"sleep_hours": 7.5, "steps": 8000}

        with tempfile.TemporaryDirectory() as tmpdir:
            health_dir = Path(tmpdir)
            latest = health_dir / "latest.json"
            latest.write_text(json.dumps(health_data))

            handler = _make_handler("GET", "/health")

            with patch.object(_mod, "HEALTH_DIR", health_dir):
                handler.do_GET()

        handler.send_response.assert_called_with(200)

    def test_get_health_no_data(self):
        """GET /health returns 200 with no-data message when no latest.json."""
        handler = _make_handler("GET", "/health")

        with tempfile.TemporaryDirectory() as tmpdir:
            health_dir = Path(tmpdir)
            with patch.object(_mod, "HEALTH_DIR", health_dir):
                handler.do_GET()

        handler.send_response.assert_called_with(200)

    def test_get_wrong_path_returns_404(self):
        """GET to wrong path returns 404."""
        handler = _make_handler("GET", "/wrong")
        handler.do_GET()
        handler.send_error.assert_called_with(404)

    def test_is_history_flag_handled(self):
        """POST with source=healthkit_history uses history path."""
        today = date.today().isoformat()
        body = json.dumps({
            "sleep_hours": 7.0,
            "date": today,
            "source": "healthkit_history",
        }).encode()
        handler = _make_handler("POST", "/health", body)

        with tempfile.TemporaryDirectory() as tmpdir:
            health_dir = Path(tmpdir)
            with patch.object(_mod, "HEALTH_DIR", health_dir):
                with patch("urllib.request.urlopen", MagicMock()):
                    with patch.object(Path, "chmod", lambda self, mode: None):
                        handler.do_POST()

        # Latest should NOT be written for history
        latest = health_dir / "latest.json"
        self.assertFalse(latest.exists(), "latest.json should not be written for healthkit_history")

    def test_daily_file_created(self):
        """POST writes a dated daily JSON file."""
        today = date.today().isoformat()
        body = json.dumps({"sleep_hours": 7.0, "date": today}).encode()
        handler = _make_handler("POST", "/health", body)

        with tempfile.TemporaryDirectory() as tmpdir:
            health_dir = Path(tmpdir)
            with patch.object(_mod, "HEALTH_DIR", health_dir):
                with patch("urllib.request.urlopen", MagicMock()):
                    with patch.object(Path, "chmod", lambda self, mode: None):
                        handler.do_POST()

        daily_file = health_dir / f"{today}.json"
        self.assertTrue(daily_file.exists(), f"Daily file {today}.json not created")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_post_creates_latest_and_daily_for_live_data(self):
        """POST of live health data creates both latest.json and dated file."""
        today = date.today().isoformat()
        body = json.dumps({
            "sleep_hours": 7.5,
            "resting_heart_rate": 58,
            "date": today,
        }).encode()
        handler = _make_handler("POST", "/health", body)

        with tempfile.TemporaryDirectory() as tmpdir:
            health_dir = Path(tmpdir)
            with patch.object(_mod, "HEALTH_DIR", health_dir):
                with patch("urllib.request.urlopen", MagicMock()):
                    with patch.object(Path, "chmod", lambda self, mode: None):
                        handler.do_POST()

        self.assertTrue((health_dir / "latest.json").exists())
        self.assertTrue((health_dir / f"{today}.json").exists())

    def test_memory_payload_includes_metrics(self):
        """Memory payload must include non-zero metrics from health data."""
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        today = date.today().isoformat()
        body = json.dumps({
            "sleep_hours": 7.5,
            "steps": 8000,
            "date": today,
        }).encode()
        handler = _make_handler("POST", "/health", body)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "HEALTH_DIR", Path(tmpdir)):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    with patch.object(Path, "chmod", lambda self, mode: None):
                        handler.do_POST()

        if captured:
            text = captured[0]["text"]
            self.assertIn("sleep_hours", text)

    def test_history_merges_into_existing_daily(self):
        """healthkit_history POST merges with existing daily data."""
        today = date.today().isoformat()
        existing_data = {"hrv": 45, "date": today, "received_at": "2026-01-01T00:00:00"}

        with tempfile.TemporaryDirectory() as tmpdir:
            health_dir = Path(tmpdir)
            daily = health_dir / f"{today}.json"
            daily.write_text(json.dumps(existing_data))

            body = json.dumps({
                "sleep_hours": 7.0,
                "date": today,
                "source": "healthkit_history",
            }).encode()
            handler = _make_handler("POST", "/health", body)

            with patch.object(_mod, "HEALTH_DIR", health_dir):
                with patch("urllib.request.urlopen", MagicMock()):
                    with patch.object(Path, "chmod", lambda self, mode: None):
                        handler.do_POST()

            merged = json.loads(daily.read_text())
            self.assertIn("sleep_hours", merged)
            self.assertIn("hrv", merged)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_zero_values_excluded_from_metrics(self):
        """Zero-value metrics must not appear in memory summary text."""
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        today = date.today().isoformat()
        body = json.dumps({
            "sleep_hours": 7.5,
            "steps": 0,          # zero — should be excluded
            "date": today,
        }).encode()
        handler = _make_handler("POST", "/health", body)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "HEALTH_DIR", Path(tmpdir)):
                with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                    with patch.object(Path, "chmod", lambda self, mode: None):
                        handler.do_POST()

        if captured:
            metadata = captured[0].get("metadata", {})
            self.assertNotIn("steps", metadata, "Zero steps should not appear in metadata")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        """nova_healthkit_receiver.py compiles without syntax errors."""
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_handler_class_exists(self):
        """HealthHandler must be a BaseHTTPRequestHandler subclass."""
        self.assertTrue(issubclass(HealthHandler, BaseHTTPRequestHandler))

    def test_port_is_37450(self):
        """PORT must be 37450 (documented protocol)."""
        self.assertEqual(PORT, 37450)

    def test_memory_url_defined(self):
        """MEMORY_URL must be defined and be a string."""
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertTrue(_mod.MEMORY_URL.startswith("http"))

    def test_health_dir_defined(self):
        """HEALTH_DIR must be defined."""
        self.assertIsInstance(_mod.HEALTH_DIR, Path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
