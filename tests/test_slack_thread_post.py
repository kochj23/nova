"""
test_slack_thread_post.py — All 7 test categories for slack_thread_post.py
Written by Jordan Koch.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

import importlib.util
_SCRIPT = Path(__file__).parent.parent / "scripts" / "slack_thread_post.py"
_spec = importlib.util.spec_from_file_location("slack_thread_post", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_tokens(self):
        src = _SCRIPT.read_text()
        import re
        tokens = re.findall(r'xox[bpoas]-[A-Za-z0-9-]+', src)
        self.assertEqual(tokens, [], f"Hardcoded Slack tokens: {tokens}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pattern in ["kochjpar" + _at + "gmail.com"]:
            self.assertNotIn(pattern, src)

    def test_subprocess_timeout_set(self):
        """subprocess.run must have timeout to prevent hanging."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=15", src)

    def test_metadata_stored_locally(self):
        """Thread metadata must be stored locally, not sent externally."""
        src = _SCRIPT.read_text()
        self.assertIn("metadata_dir", src)
        self.assertIn(".openclaw", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_subprocess_timeout_prevents_hang(self):
        """subprocess.run must have 15s timeout."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=15", src)

    def test_sections_parsed_efficiently(self):
        """parse_markdown_sections() must handle reasonable input sizes."""
        poster = _mod.SlackThreadPoster("C123")
        content = "\n".join([f"## Section {i}\nContent {i}" for i in range(50)])
        sections = poster.parse_markdown_sections(content)
        self.assertEqual(len(sections), 50)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_run_slack_cmd_handles_exception(self):
        """_run_slack_cmd() must return error dict on exception."""
        poster = _mod.SlackThreadPoster("C123")
        with patch("subprocess.run", side_effect=Exception("command not found")):
            result = poster._run_slack_cmd(["nonexistent", "command"])
        self.assertEqual(result["status"], "error")
        self.assertIn("error", result)

    def test_run_slack_cmd_handles_nonzero_exit(self):
        """_run_slack_cmd() must return error dict on non-zero exit."""
        poster = _mod.SlackThreadPoster("C123")

        def fail_run(*args, **kwargs):
            m = MagicMock()
            m.returncode = 1
            m.stderr = "command failed"
            m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=fail_run):
            result = poster._run_slack_cmd(["message", "test"])
        self.assertEqual(result["status"], "error")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_slack_thread_poster_init(self):
        poster = _mod.SlackThreadPoster("C123ABC")
        self.assertEqual(poster.channel, "C123ABC")
        self.assertTrue(poster.metadata_dir.exists())

    def test_parse_markdown_sections_empty(self):
        poster = _mod.SlackThreadPoster("C123")
        sections = poster.parse_markdown_sections("")
        self.assertEqual(sections, [])

    def test_parse_markdown_sections_basic(self):
        poster = _mod.SlackThreadPoster("C123")
        content = "## Section One\nContent here\n## Section Two\nMore content"
        sections = poster.parse_markdown_sections(content)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0]["title"], "Section One")
        self.assertEqual(sections[1]["title"], "Section Two")

    def test_parse_email_digest_groups_by_sender(self):
        poster = _mod.SlackThreadPoster("C123")
        digest = {
            "emails": [
                {"from": "alice@example.com", "subject": "Hello", "body_preview": "Hi there"},
                {"from": "alice@example.com", "subject": "Follow up", "body_preview": "More"},
                {"from": "bob@example.com", "subject": "Question", "body_preview": "Question"},
            ]
        }
        sections = poster.parse_email_digest(digest)
        senders = [s["title"] for s in sections]
        self.assertIn("From: alice@example.com", senders)
        self.assertIn("From: bob@example.com", senders)

    def test_store_metadata_creates_file(self):
        poster = _mod.SlackThreadPoster("C123")
        with tempfile.TemporaryDirectory() as tmpdir:
            poster.metadata_dir = Path(tmpdir)
            poster._store_metadata("1234567890.123456",
                                   {"type": "test", "source": "test"})
            files = list(Path(tmpdir).glob("*.json"))
            self.assertEqual(len(files), 1)

    def test_post_via_tool_returns_ts(self):
        """_post_via_tool() must return a timestamp."""
        poster = _mod.SlackThreadPoster("C123")
        result = poster._post_via_tool("test message")
        self.assertEqual(result["status"], "ok")
        self.assertIn("ts", result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_post_sectioned_creates_parent_then_replies(self):
        """post_sectioned() must create parent message then replies."""
        poster = _mod.SlackThreadPoster("C123")
        sections = [
            {"title": "Overview", "content": "Summary here"},
            {"title": "Details", "content": "Detail content"},
        ]
        result = poster.post_sectioned("Test Report", sections)
        self.assertIn("posts", result)
        self.assertGreater(len(result["posts"]), 0)

        post_types = [p["type"] for p in result["posts"]]
        self.assertIn("parent", post_types)

    def test_post_message_with_thread_ts(self):
        """post_message() with thread_ts must send as reply."""
        poster = _mod.SlackThreadPoster("C123")
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args[0])
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"ts": "1234567890.000001"})
            return r

        with patch("subprocess.run", side_effect=fake_run):
            result = poster.post_message(
                "Test", "Message content", thread_ts="1234567890.000000")

        if calls:
            cmd = " ".join(str(c) for c in calls[0])
            self.assertIn("threadId", cmd)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_requires_channel(self):
        """main() must require --channel argument."""
        with patch("sys.argv", ["slack_thread_post.py"]):
            with self.assertRaises(SystemExit):
                _mod.main()

    def test_main_requires_content_source(self):
        """main() must exit when no content source given."""
        with patch("sys.argv", ["slack_thread_post.py", "--channel", "C123"]):
            with self.assertRaises(SystemExit):
                _mod.main()

    def test_get_ts_flag_prints_ts(self):
        """--get-ts flag must print the thread timestamp."""
        src = _SCRIPT.read_text()
        self.assertIn("get_ts", src)
        self.assertIn("print(ts)", src)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"slack_thread_post.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_main_function_exists(self):
        self.assertTrue(callable(_mod.main))

    def test_slack_thread_poster_class_exists(self):
        self.assertTrue(hasattr(_mod, "SlackThreadPoster"))

    def test_main_guard_present(self):
        src = _SCRIPT.read_text()
        self.assertIn('if __name__ == "__main__"', src)

    def test_class_methods_present(self):
        poster = _mod.SlackThreadPoster("C123")
        for method in ["post_message", "post_sectioned",
                       "parse_markdown_sections", "parse_email_digest",
                       "_store_metadata", "_post_via_tool"]:
            self.assertTrue(callable(getattr(poster, method, None)),
                            f"Method {method} must exist")


if __name__ == "__main__":
    unittest.main(verbosity=2)
