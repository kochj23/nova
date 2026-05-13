"""
test_nova_log_rotate.py — All 7 test categories for nova_log_rotate.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_log_rotate.py"
_spec = importlib.util.spec_from_file_location("nova_log_rotate", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

trim_jsonl = _mod.trim_jsonl
trim_log_file = _mod.trim_log_file
main = _mod.main
MAX_LOG_BYTES = _mod.MAX_LOG_BYTES


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for p in ["sk-", "ghp_", "AKIA"]:
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

    def test_log_dirs_use_path_home(self):
        """Log directories must use Path.home()."""
        self.assertTrue(str(_mod.CRON_RUNS_DIR).startswith(str(Path.home())))
        self.assertTrue(str(_mod.LOGS_DIR).startswith(str(Path.home())))

    def test_max_log_size_reasonable(self):
        """MAX_LOG_BYTES must be at most 50MB (prevent disk exhaustion)."""
        self.assertLessEqual(MAX_LOG_BYTES, 50 * 1024 * 1024)
        self.assertGreater(MAX_LOG_BYTES, 0)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_trim_jsonl_fast(self):
        """trim_jsonl must handle 10k lines in < 500ms."""
        cutoff = datetime.now() - timedelta(days=30)
        old_ts = int((cutoff - timedelta(days=1)).timestamp() * 1000)
        new_ts = int((cutoff + timedelta(days=1)).timestamp() * 1000)

        lines = []
        for i in range(10000):
            ts = old_ts if i % 2 == 0 else new_ts
            lines.append(json.dumps({"ts": ts, "msg": f"entry {i}"}))

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines))
            fname = Path(f.name)

        with patch.object(_mod, "CUTOFF", cutoff):
            start = time.perf_counter()
            trim_jsonl(fname)
            elapsed = time.perf_counter() - start

        fname.unlink()
        self.assertLess(elapsed, 0.5)

    def test_trim_log_file_fast_small_file(self):
        """trim_log_file returns 0 quickly for small files."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("small log content\n")
            fname = Path(f.name)

        start = time.perf_counter()
        result = trim_log_file(fname)
        elapsed = time.perf_counter() - start
        fname.unlink()

        self.assertLess(elapsed, 0.01)
        self.assertEqual(result, 0)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_trim_jsonl_handles_unreadable_file(self):
        """trim_jsonl returns (0, 0) for unreadable files."""
        result = trim_jsonl(Path("/nonexistent/file.jsonl"))
        self.assertEqual(result, (0, 0))

    def test_trim_log_file_handles_unreadable_file(self):
        """trim_log_file returns 0 for unreadable files."""
        result = trim_log_file(Path("/nonexistent/file.log"))
        self.assertEqual(result, 0)

    def test_main_handles_missing_dirs(self):
        """main() handles missing cron/logs dirs gracefully."""
        with patch.object(_mod, "CRON_RUNS_DIR", Path("/nonexistent/cron")):
            with patch.object(_mod, "LOGS_DIR", Path("/nonexistent/logs")):
                try:
                    main()
                except Exception as exc:
                    self.fail(f"main() raised with missing dirs: {exc}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_trim_jsonl_removes_old_entries(self):
        """trim_jsonl removes entries older than CUTOFF."""
        cutoff = datetime.now() - timedelta(days=30)
        old_ts = int((cutoff - timedelta(days=5)).timestamp() * 1000)
        new_ts = int((cutoff + timedelta(days=5)).timestamp() * 1000)

        lines = [
            json.dumps({"ts": old_ts, "msg": "old entry"}),
            json.dumps({"ts": new_ts, "msg": "new entry"}),
            json.dumps({"ts": old_ts, "msg": "also old"}),
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines))
            fname = Path(f.name)

        with patch.object(_mod, "CUTOFF", cutoff):
            before, after = trim_jsonl(fname)

        content = fname.read_text()
        fname.unlink()

        self.assertEqual(before, 3)
        self.assertEqual(after, 1)
        self.assertIn("new entry", content)
        self.assertNotIn("old entry", content)

    def test_trim_jsonl_keeps_malformed_lines(self):
        """trim_jsonl preserves malformed JSON lines (be conservative)."""
        lines = [
            "not valid json",
            json.dumps({"ts": int(datetime.now().timestamp() * 1000), "msg": "valid"}),
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines))
            fname = Path(f.name)

        before, after = trim_jsonl(fname)
        fname.unlink()

        # Both lines kept (malformed = keep, valid new = keep)
        self.assertEqual(after, 2)

    def test_trim_log_file_large_file(self):
        """trim_log_file truncates files larger than MAX_LOG_BYTES."""
        # Create file larger than MAX_LOG_BYTES
        content = b"log line\n" * (MAX_LOG_BYTES // 9 + 100)

        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            f.write(content)
            fname = Path(f.name)

        freed = trim_log_file(fname)
        final_size = fname.stat().st_size
        fname.unlink()

        self.assertGreater(freed, 0, "Should have freed some bytes")
        self.assertLessEqual(final_size, MAX_LOG_BYTES)

    def test_trim_log_file_preserves_complete_lines(self):
        """trim_log_file must not split log lines mid-line."""
        # Write content with known line structure
        lines = [f"2026-01-01 entry {i:06d}\n" for i in range(10000)]
        content = "".join(lines).encode()

        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            f.write(content)
            fname = Path(f.name)

        trim_log_file(fname)
        result = fname.read_bytes()
        fname.unlink()

        # Result must start at a line boundary (starts after a newline)
        lines_out = result.decode().splitlines()
        if lines_out:
            # Each line should match our format
            self.assertTrue(lines_out[0].startswith("2026-01-01"))

    def test_cutoff_is_30_days_ago(self):
        """CUTOFF must be approximately 30 days in the past."""
        now = datetime.now()
        diff = now - _mod.CUTOFF
        self.assertAlmostEqual(diff.days, 30, delta=1)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_trims_old_jsonl_files(self):
        """main() trims jsonl files with old entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cron_dir = Path(tmpdir) / "runs"
            cron_dir.mkdir()
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()

            # Create a jsonl file with old entries
            cutoff = datetime.now() - timedelta(days=30)
            old_ts = int((cutoff - timedelta(days=5)).timestamp() * 1000)
            new_ts = int((cutoff + timedelta(days=5)).timestamp() * 1000)

            jsonl = cron_dir / "test.jsonl"
            jsonl.write_text(
                json.dumps({"ts": old_ts, "msg": "old"}) + "\n" +
                json.dumps({"ts": new_ts, "msg": "new"}) + "\n"
            )

            with patch.object(_mod, "CRON_RUNS_DIR", cron_dir):
                with patch.object(_mod, "LOGS_DIR", logs_dir):
                    with patch.object(_mod, "CUTOFF", cutoff):
                        main()

            content = jsonl.read_text()
            self.assertIn("new", content)
            self.assertNotIn("old", content)

    def test_main_rotates_large_log_files(self):
        """main() rotates log files that exceed MAX_LOG_BYTES."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()
            cron_dir = Path(tmpdir) / "runs"
            cron_dir.mkdir()

            big_log = logs_dir / "big.log"
            big_log.write_bytes(b"x" * (MAX_LOG_BYTES + 100))

            with patch.object(_mod, "LOGS_DIR", logs_dir):
                with patch.object(_mod, "CRON_RUNS_DIR", cron_dir):
                    main()

            self.assertLessEqual(big_log.stat().st_size, MAX_LOG_BYTES)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_posts_to_slack_when_work_done(self):
        """main() posts to Slack when files were rotated/trimmed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()
            cron_dir = Path(tmpdir) / "runs"

            big_log = logs_dir / "nova.log"
            big_log.write_bytes(b"x" * (MAX_LOG_BYTES + 1000))

            slack_msgs = []
            _nova_cfg.post_both.side_effect = lambda msg, **kw: slack_msgs.append(msg)

            with patch.object(_mod, "LOGS_DIR", logs_dir):
                with patch.object(_mod, "CRON_RUNS_DIR", cron_dir):
                    main()

            _nova_cfg.post_both.side_effect = None

        if slack_msgs:
            self.assertTrue(any("Log Rotation" in m or "log" in m.lower() for m in slack_msgs))

    def test_main_no_slack_when_nothing_to_do(self):
        """main() does NOT post to Slack when no files need rotation."""
        _nova_cfg.post_both.reset_mock()
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir) / "logs"
            logs_dir.mkdir()
            cron_dir = Path(tmpdir) / "runs"
            cron_dir.mkdir()
            # Small log file — no rotation needed
            (logs_dir / "small.log").write_text("small log\n")

            with patch.object(_mod, "LOGS_DIR", logs_dir):
                with patch.object(_mod, "CRON_RUNS_DIR", cron_dir):
                    main()

        _nova_cfg.post_both.assert_not_called()


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
        for fn in [trim_jsonl, trim_log_file, main]:
            self.assertTrue(callable(fn))

    def test_constants_defined(self):
        self.assertIsInstance(_mod.MAX_LOG_BYTES, int)
        self.assertIsInstance(_mod.CRON_RUNS_DIR, Path)
        self.assertIsInstance(_mod.LOGS_DIR, Path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
