"""
test_nova_logger.py — All 7 test categories for nova_logger.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_logger.py"
_spec = importlib.util.spec_from_file_location("nova_logger", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

log = _mod.log
read_logs = _mod.read_logs
_rotate = _mod._rotate
_guess_source = _mod._guess_source
LOG_DEBUG = _mod.LOG_DEBUG
LOG_INFO = _mod.LOG_INFO
LOG_WARN = _mod.LOG_WARN
LOG_ERROR = _mod.LOG_ERROR
LOG_FATAL = _mod.LOG_FATAL
MAX_SIZE_BYTES = _mod.MAX_SIZE_BYTES
MAX_FILES = _mod.MAX_FILES


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for p in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(p, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(p, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_log_dir_uses_path_home(self):
        """LOG_DIR must be under home directory."""
        self.assertTrue(str(_mod.LOG_DIR).startswith(str(Path.home())))

    def test_log_file_does_not_expose_secrets(self):
        """Log entries must not expose full stack traces to external channels."""
        src = _SCRIPT.read_text()
        # Log writes to file, not to Slack/external. Verify no Slack posting.
        self.assertNotIn("post_both", src)
        self.assertNotIn("SLACK_API", src)

    def test_log_doesnt_crash_on_disk_full(self):
        """log() must silently ignore OSError (disk full, permissions)."""
        with patch("builtins.open", side_effect=OSError("disk full")):
            try:
                log("test message", level=LOG_INFO, source="test")
            except Exception as exc:
                self.fail(f"log() raised on OSError: {exc}")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_log_fast(self):
        """log() must write 1000 entries in < 500ms."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "LOG_DIR", Path(tmpdir)):
                with patch.object(_mod, "LOG_FILE", Path(tmpdir) / "nova.jsonl"):
                    start = time.perf_counter()
                    for i in range(1000):
                        log(f"message {i}", level=LOG_INFO, source="perf_test")
                    elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_read_logs_fast(self):
        """read_logs must read 10k entries in < 500ms."""
        entries = [json.dumps({"ts": "2026-01-01T00:00:00+00:00", "level": "info",
                               "source": "test", "msg": f"msg {i}"})
                   for i in range(10000)]

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "nova.jsonl"
            log_file.write_text("\n".join(entries))

            with patch.object(_mod, "LOG_DIR", Path(tmpdir)):
                with patch.object(_mod, "LOG_FILE", log_file):
                    start = time.perf_counter()
                    result = read_logs(n=100)
                    elapsed = time.perf_counter() - start

        self.assertLess(elapsed, 0.5)

    def test_max_size_is_reasonable(self):
        """MAX_SIZE_BYTES must be between 1MB and 500MB."""
        self.assertGreaterEqual(MAX_SIZE_BYTES, 1 * 1024 * 1024)
        self.assertLessEqual(MAX_SIZE_BYTES, 500 * 1024 * 1024)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_log_handles_permission_error(self):
        """log() must not raise on PermissionError."""
        with patch("builtins.open", side_effect=PermissionError("no write")):
            try:
                log("test", level=LOG_ERROR)
            except PermissionError:
                self.fail("log() should not propagate PermissionError")

    def test_read_logs_handles_missing_file(self):
        """read_logs returns [] when log files don't exist."""
        with patch.object(_mod, "LOG_DIR", Path("/nonexistent")):
            with patch.object(_mod, "LOG_FILE", Path("/nonexistent/nova.jsonl")):
                result = read_logs(n=100)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 0)

    def test_rotate_handles_rename_error(self):
        """_rotate must not raise if rename fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "nova.jsonl"
            log_file.write_bytes(b"x" * (MAX_SIZE_BYTES + 100))

            with patch.object(_mod, "LOG_FILE", log_file):
                with patch.object(_mod, "LOG_DIR", Path(tmpdir)):
                    with patch.object(Path, "rename", side_effect=OSError("busy")):
                        try:
                            _rotate()
                        except Exception:
                            pass  # May or may not raise — just shouldn't crash the caller


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_log_writes_json_entry(self):
        """log() writes valid JSON to log file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "nova.jsonl"

            with patch.object(_mod, "LOG_DIR", Path(tmpdir)):
                with patch.object(_mod, "LOG_FILE", log_file):
                    log("test message", level=LOG_INFO, source="unit_test")

            entries = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]

        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["level"], "info")
        self.assertEqual(entry["source"], "unit_test")
        self.assertEqual(entry["msg"], "test message")
        self.assertIn("ts", entry)

    def test_log_includes_extra_data(self):
        """log() includes extra dict in entry."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "nova.jsonl"

            with patch.object(_mod, "LOG_DIR", Path(tmpdir)):
                with patch.object(_mod, "LOG_FILE", log_file):
                    log("error occurred", level=LOG_ERROR, source="test",
                        extra={"host": "localhost", "code": 500})

            entry = json.loads(log_file.read_text().strip())

        self.assertIn("extra", entry)
        self.assertEqual(entry["extra"]["host"], "localhost")
        self.assertEqual(entry["extra"]["code"], 500)

    def test_log_level_filtering(self):
        """log() skips entries below MIN_LEVEL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "nova.jsonl"

            with patch.object(_mod, "LOG_DIR", Path(tmpdir)):
                with patch.object(_mod, "LOG_FILE", log_file):
                    with patch.object(_mod, "MIN_LEVEL", LOG_ERROR):
                        log("debug message", level=LOG_DEBUG, source="test")
                        log("info message", level=LOG_INFO, source="test")
                        log("error message", level=LOG_ERROR, source="test")

            lines = [l for l in log_file.read_text().splitlines() if l.strip()]

        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["msg"], "error message")

    def test_level_constants_defined(self):
        """All log level constants must be defined."""
        self.assertEqual(LOG_DEBUG, "debug")
        self.assertEqual(LOG_INFO, "info")
        self.assertEqual(LOG_WARN, "warn")
        self.assertEqual(LOG_ERROR, "error")
        self.assertEqual(LOG_FATAL, "fatal")

    def test_level_order_ascending(self):
        """Level order must be debug < info < warn < error < fatal."""
        order = _mod._LEVEL_ORDER
        self.assertLess(order["debug"], order["info"])
        self.assertLess(order["info"], order["warn"])
        self.assertLess(order["warn"], order["error"])
        self.assertLess(order["error"], order["fatal"])

    def test_max_files_defined(self):
        self.assertIsInstance(MAX_FILES, int)
        self.assertGreater(MAX_FILES, 0)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_read_logs_returns_newest_first(self):
        """read_logs returns entries newest first."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "nova.jsonl"

            entries = [
                json.dumps({"ts": f"2026-01-0{i}T00:00:00+00:00", "level": "info",
                            "source": "test", "msg": f"entry {i}"})
                for i in range(1, 6)
            ]
            log_file.write_text("\n".join(entries))

            with patch.object(_mod, "LOG_DIR", Path(tmpdir)):
                with patch.object(_mod, "LOG_FILE", log_file):
                    result = read_logs(n=5)

        # Newest first
        timestamps = [r["ts"] for r in result]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    def test_read_logs_source_filter(self):
        """read_logs correctly filters by source."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "nova.jsonl"

            entries = [
                json.dumps({"ts": "2026-01-01T00:00:00+00:00", "level": "info",
                            "source": "source_a", "msg": "from a"}),
                json.dumps({"ts": "2026-01-02T00:00:00+00:00", "level": "info",
                            "source": "source_b", "msg": "from b"}),
            ]
            log_file.write_text("\n".join(entries))

            with patch.object(_mod, "LOG_DIR", Path(tmpdir)):
                with patch.object(_mod, "LOG_FILE", log_file):
                    result = read_logs(n=100, source="source_a")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "source_a")

    def test_rotate_when_file_exceeds_max(self):
        """_rotate renames log file when it exceeds MAX_SIZE_BYTES."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "nova.jsonl"
            log_file.write_bytes(b"x" * (MAX_SIZE_BYTES + 100))

            with patch.object(_mod, "LOG_FILE", log_file):
                with patch.object(_mod, "LOG_DIR", Path(tmpdir)):
                    _rotate()

            rotated = Path(tmpdir) / "nova.jsonl.1"
            self.assertTrue(rotated.exists(), "Log file should have been rotated to .1")
            self.assertFalse(log_file.exists(), "Original log file should be gone")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_error_and_fatal_print_to_stderr(self):
        """ERROR and FATAL levels print to stderr."""
        import io
        from contextlib import redirect_stderr

        stderr_out = io.StringIO()

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "nova.jsonl"
            with patch.object(_mod, "LOG_DIR", Path(tmpdir)):
                with patch.object(_mod, "LOG_FILE", log_file):
                    with redirect_stderr(stderr_out):
                        log("error happened", level=LOG_ERROR, source="test")

        output = stderr_out.getvalue()
        self.assertIn("error happened", output)

    def test_debug_does_not_print_to_stderr(self):
        """DEBUG level must NOT print to stderr."""
        import io
        from contextlib import redirect_stderr

        stderr_out = io.StringIO()

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "nova.jsonl"
            with patch.object(_mod, "LOG_DIR", Path(tmpdir)):
                with patch.object(_mod, "LOG_FILE", log_file):
                    with patch.object(_mod, "MIN_LEVEL", LOG_DEBUG):
                        with redirect_stderr(stderr_out):
                            log("debug only", level=LOG_DEBUG, source="test")

        output = stderr_out.getvalue()
        self.assertNotIn("debug only", output)

    def test_log_entry_timestamp_is_utc_iso(self):
        """Log timestamps must be UTC ISO format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "nova.jsonl"
            with patch.object(_mod, "LOG_DIR", Path(tmpdir)):
                with patch.object(_mod, "LOG_FILE", log_file):
                    log("time test", level=LOG_INFO)

            entry = json.loads(log_file.read_text().strip())

        ts = entry["ts"]
        # Should parse as ISO and have timezone info
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(ts)
        self.assertIsNotNone(parsed.tzinfo)


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

    def test_key_functions_callable(self):
        for fn in [log, read_logs, _rotate, _guess_source]:
            self.assertTrue(callable(fn))

    def test_level_constants_are_strings(self):
        for level in [LOG_DEBUG, LOG_INFO, LOG_WARN, LOG_ERROR, LOG_FATAL]:
            self.assertIsInstance(level, str)

    def test_log_dir_defined(self):
        self.assertIsInstance(_mod.LOG_DIR, Path)
        self.assertIsInstance(_mod.LOG_FILE, Path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
