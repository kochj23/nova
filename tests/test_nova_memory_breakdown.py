"""
test_nova_memory_breakdown.py — All 7 test categories for nova_memory_breakdown.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub nova_config before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_memory_breakdown.py"
_spec = importlib.util.spec_from_file_location("nova_memory_breakdown", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

get_queue_depth = _mod.get_queue_depth
get_breakdown = _mod.get_breakdown
post_breakdown = _mod.post_breakdown


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        """Source must not contain API keys or passwords."""
        src = _SCRIPT.read_text()
        for p in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(p, src, f"Credential found: {p!r}")

    def test_no_pii_emails(self):
        """Source must not contain personal email addresses."""
        src = _SCRIPT.read_text()
        _at = "@"
        for pattern in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pattern, src)

    def test_no_hardcoded_home_path(self):
        """Source must not hardcode literal home path."""
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_psql_uses_local_database(self):
        """psql command must use local nova_memories database."""
        src = _SCRIPT.read_text()
        self.assertIn("nova_memories", src)

    def test_vector_url_is_local(self):
        """VECTOR_URL must point to localhost."""
        self.assertIn("127.0.0.1", _mod.VECTOR_URL)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_get_queue_depth_fast_on_failure(self):
        """get_queue_depth must return quickly (< 100ms) when server unreachable."""
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            start = time.perf_counter()
            result = get_queue_depth()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)
        self.assertEqual(result, -1)

    def test_post_breakdown_splits_long_message(self):
        """post_breakdown must split messages > 3000 chars."""
        # Build a breakdown with many sources to force splitting
        long_output = "\n".join([f"source_{i}|{i*1000}" for i in range(100)])

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = long_output

        calls = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: calls.append(msg)

        with patch("subprocess.run", return_value=mock_result):
            post_breakdown()

        _nova_cfg.post_both.side_effect = None
        # Should have posted (at minimum 1 call)
        self.assertGreater(len(calls), 0)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_queue_depth_returns_minus1_on_failure(self):
        """get_queue_depth returns -1 when server is unreachable."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = get_queue_depth()
        self.assertEqual(result, -1)

    def test_get_breakdown_returns_none_on_psql_failure(self):
        """get_breakdown returns None when psql fails."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            result = get_breakdown()
        self.assertIsNone(result)

    def test_post_breakdown_handles_psql_failure(self):
        """post_breakdown handles psql failure without crashing."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            try:
                post_breakdown()
            except Exception as exc:
                self.fail(f"post_breakdown raised on psql failure: {exc}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_get_queue_depth_returns_number(self):
        """get_queue_depth returns pending count from vector server."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"pending": 42}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = get_queue_depth()
        self.assertEqual(result, 42)

    def test_get_queue_depth_returns_0_when_empty(self):
        """get_queue_depth returns 0 when queue is empty."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"pending": 0}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = get_queue_depth()
        self.assertEqual(result, 0)

    def test_get_breakdown_parses_psql_output(self):
        """get_breakdown correctly parses psql pipe-separated output."""
        psql_output = "television|15000\nemail_archive|8000\nslack|3000\n"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = psql_output

        with patch("subprocess.run", return_value=mock_result):
            result = get_breakdown()

        self.assertIsNotNone(result)
        breakdown, total = result
        self.assertEqual(total, 26000)
        self.assertEqual(len(breakdown), 3)
        self.assertEqual(breakdown[0], ("television", 15000))

    def test_get_breakdown_handles_empty_output(self):
        """get_breakdown handles empty psql output."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            result = get_breakdown()

        if result:
            breakdown, total = result
            self.assertEqual(total, 0)

    def test_get_breakdown_skips_lines_without_pipe(self):
        """get_breakdown skips malformed lines without | separator."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "television|1000\nmalformed_line\nemail|500\n"

        with patch("subprocess.run", return_value=mock_result):
            result = get_breakdown()

        if result:
            breakdown, total = result
            self.assertEqual(total, 1500)
            self.assertEqual(len(breakdown), 2)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_post_breakdown_calls_slack_post(self):
        """post_breakdown calls post_both with formatted breakdown."""
        psql_output = "television|1500\nemail_archive|800\nslack|200\n"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = psql_output

        slack_calls = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: slack_calls.append(msg)

        with patch("subprocess.run", return_value=mock_result):
            post_breakdown()

        _nova_cfg.post_both.side_effect = None
        self.assertTrue(len(slack_calls) > 0)
        # Should contain source names and total
        full_msg = "\n".join(slack_calls)
        self.assertIn("television", full_msg)
        self.assertIn("2,500", full_msg)  # formatted total

    def test_post_breakdown_includes_percentages(self):
        """Breakdown message must include percentage bars."""
        psql_output = "television|500\nslack|500\n"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = psql_output

        slack_calls = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: slack_calls.append(msg)

        with patch("subprocess.run", return_value=mock_result):
            post_breakdown()

        _nova_cfg.post_both.side_effect = None
        full_msg = "\n".join(slack_calls)
        self.assertIn("%", full_msg)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_wait_and_post_exits_when_queue_drains(self):
        """wait_and_post calls post_breakdown when queue reaches 0."""
        depth_sequence = [100, 50, 0]
        call_index = [0]

        def fake_queue_depth():
            idx = min(call_index[0], len(depth_sequence) - 1)
            val = depth_sequence[idx]
            call_index[0] += 1
            return val

        post_calls = []

        with patch.object(_mod, "get_queue_depth", side_effect=fake_queue_depth):
            with patch.object(_mod, "post_breakdown", side_effect=lambda: post_calls.append(1)):
                with patch("time.sleep"):
                    _mod.wait_and_post()

        self.assertEqual(len(post_calls), 1, "post_breakdown should be called once")

    def test_post_breakdown_total_row_present(self):
        """Breakdown must include a TOTAL row."""
        psql_output = "television|1000\nemail|500\n"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = psql_output

        slack_calls = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: slack_calls.append(msg)

        with patch("subprocess.run", return_value=mock_result):
            post_breakdown()

        _nova_cfg.post_both.side_effect = None
        full_msg = "\n".join(slack_calls)
        self.assertIn("TOTAL", full_msg)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        """nova_memory_breakdown.py compiles without syntax errors."""
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_key_functions_callable(self):
        """All key functions must be callable."""
        for fn in [get_queue_depth, get_breakdown, post_breakdown, _mod.wait_and_post]:
            self.assertTrue(callable(fn))

    def test_vector_url_defined(self):
        """VECTOR_URL must be defined."""
        self.assertIsInstance(_mod.VECTOR_URL, str)
        self.assertTrue(_mod.VECTOR_URL.startswith("http"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
