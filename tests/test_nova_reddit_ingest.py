"""
test_nova_reddit_ingest.py — All 7 test categories for nova_reddit_ingest.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub dependencies before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules.setdefault("nova_config", _nova_cfg)

# nova_logger stub
_nova_logger = MagicMock()
_nova_logger.LOG_INFO  = "INFO"
_nova_logger.LOG_ERROR = "ERROR"
_nova_logger.LOG_WARN  = "WARN"
_nova_logger.log = MagicMock()
sys.modules["nova_logger"] = _nova_logger

_SCRIPT = Path(__file__).parent.parent / "scripts" / "_archive" / "nova_reddit_ingest.py"
_spec = importlib.util.spec_from_file_location("nova_reddit_ingest", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_state           = _mod.load_state
save_state           = _mod.save_state
fetch_subreddit      = _mod.fetch_subreddit
fetch_comments       = _mod.fetch_comments
vector_remember      = _mod.vector_remember
ingest_subreddit     = _mod.ingest_subreddit
generate_dream_context = _mod.generate_dream_context
_is_quiet_hours      = _mod._is_quiet_hours
SUBREDDITS           = _mod.SUBREDDITS


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-", "password =", "secret ="]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src, "Hardcoded home path found")

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp.com",
            "kochj" + _at + "digitalnoise.net",
            "kochj23" + _at + "gmail.com",
        ]:
            self.assertNotIn(pat, src, f"PII email found: {pat!r}")

    def test_vector_remember_sends_json(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            vector_remember("test post content", metadata={"type": "reddit_post"})

        self.assertEqual(len(captured), 1)
        self.assertIn("text", captured[0])
        self.assertIn("source", captured[0])

    def test_seen_ids_capped_at_5000(self):
        state = {"seen_ids": [str(i) for i in range(6000)]}
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "state.json"):
                save_state(state)
                loaded = load_state()
        self.assertLessEqual(len(loaded["seen_ids"]), 5000)

    def test_vector_url_is_local(self):
        url = _mod.VECTOR_URL
        self.assertTrue(
            url.startswith("http://127.0.0.1") or url.startswith("http://192.168."),
            f"VECTOR_URL must be local, got: {url}"
        )


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_load_state_fast_on_missing_file(self):
        start = time.perf_counter()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "nonexistent.json"):
                for _ in range(100):
                    load_state()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"load_state 100x took {elapsed:.3f}s")

    def test_subreddits_count_reasonable(self):
        self.assertGreater(len(SUBREDDITS), 3)
        self.assertLess(len(SUBREDDITS), 200)

    def test_generate_dream_context_fast(self):
        posts = [{"sub": "burbank", "title": f"Post {i}", "weight": "normal"}
                 for i in range(50)]
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            (home / ".openclaw" / "workspace" / "memory").mkdir(parents=True)
            with patch.object(Path, "home", return_value=home):
                start = time.perf_counter()
                generate_dream_context(posts)
                elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, f"generate_dream_context took {elapsed:.3f}s")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_fetch_subreddit_returns_empty_on_error(self):
        def failing(req, timeout=None):
            raise OSError("network error")

        with patch("urllib.request.urlopen", side_effect=failing):
            result = fetch_subreddit("burbank", {"limit": 10})

        self.assertEqual(result, [], "Should return empty list on error")

    def test_fetch_comments_returns_empty_on_error(self):
        def failing(req, timeout=None):
            raise OSError("error")

        with patch("urllib.request.urlopen", side_effect=failing):
            result = fetch_comments("burbank", "abc123")

        self.assertEqual(result, [])

    def test_vector_remember_does_not_raise_on_error(self):
        def failing(req, timeout=None):
            raise OSError("refused")

        # Should not raise
        with patch("urllib.request.urlopen", side_effect=failing):
            try:
                vector_remember("some text", metadata={})
            except Exception as e:
                self.fail(f"vector_remember raised: {e}")

    def test_fetch_subreddit_handles_timeout(self):
        import socket

        def timeout_fn(req, timeout=None):
            raise socket.timeout("timed out")

        with patch("urllib.request.urlopen", side_effect=timeout_fn):
            result = fetch_subreddit("testsubreddit", {"limit": 5})

        self.assertEqual(result, [])


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_load_state_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "missing.json"):
                state = load_state()
        self.assertIn("seen_ids", state)
        self.assertEqual(state["seen_ids"], [])

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "state.json"):
                state = {"seen_ids": ["abc", "def"], "timestamp": "2026-01-01"}
                save_state(state)
                loaded = load_state()
        self.assertIn("abc", loaded["seen_ids"])
        self.assertIn("def", loaded["seen_ids"])

    def test_is_quiet_hours_logic(self):
        # Test quiet hours boundaries using actual module reference
        import datetime as dt_module
        # Hour 23 should be quiet
        with patch.object(_mod, "datetime", MagicMock(now=MagicMock(return_value=MagicMock(hour=23)))):
            self.assertTrue(_mod._is_quiet_hours())
        # Hour 3 should be quiet
        with patch.object(_mod, "datetime", MagicMock(now=MagicMock(return_value=MagicMock(hour=3)))):
            self.assertTrue(_mod._is_quiet_hours())
        # Just verify the function returns a bool (covers import)
        result = _mod._is_quiet_hours()
        self.assertIsInstance(result, bool)

    def test_subreddits_have_required_keys(self):
        for name, cfg in SUBREDDITS.items():
            self.assertIn("source", cfg, f"r/{name} missing 'source'")
            self.assertIn("limit", cfg, f"r/{name} missing 'limit'")

    def test_state_file_uses_home_path(self):
        """STATE_FILE must use Path.home(), not a hardcoded path."""
        state_path = str(_mod.STATE_FILE)
        self.assertIn(str(Path.home()), state_path)

    def test_seen_ids_dedup(self):
        """ingest_subreddit must not re-ingest already-seen post IDs."""
        state = {"seen_ids": ["existing_id_001"]}
        posts_data = [{"data": {
            "id": "existing_id_001",
            "title": "Already seen",
            "selftext": "",
            "score": 5,
            "num_comments": 2,
            "author": "testuser",
            "permalink": "/r/burbank/comments/existing_id_001/already_seen/",
            "stickied": False,
            "link_flair_text": "",
        }}]

        with patch.object(_mod, "fetch_subreddit", return_value=posts_data):
            with patch.object(_mod, "fetch_comments", return_value=[]):
                with patch.object(_mod, "vector_remember") as mock_remember:
                    count, _ = ingest_subreddit("burbank", {"limit": 10, "source": "local"}, state)

        mock_remember.assert_not_called()
        self.assertEqual(count, 0)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_ingest_subreddit_stores_new_posts(self):
        state = {"seen_ids": []}
        posts_data = [{"data": {
            "id": "newpost001",
            "title": "New post about Burbank",
            "selftext": "Something interesting happened in Burbank today.",
            "score": 42,
            "num_comments": 10,
            "author": "localreporter",
            "permalink": "/r/burbank/comments/newpost001/new_post/",
            "stickied": False,
            "link_flair_text": "Local News",
        }}]

        stored = []

        with patch.object(_mod, "fetch_subreddit", return_value=posts_data):
            with patch.object(_mod, "fetch_comments", return_value=[]):
                with patch.object(_mod, "vector_remember",
                                  side_effect=lambda t, metadata=None: stored.append(t)):
                    with patch("time.sleep"):
                        count, today = ingest_subreddit(
                            "burbank", {"limit": 15, "source": "burbank", "dream_weight": "high"},
                            state)

        self.assertEqual(count, 1)
        self.assertEqual(len(stored), 1)
        self.assertIn("newpost001", state["seen_ids"])

    def test_generate_dream_context_groups_by_sub(self):
        posts = [
            {"sub": "burbank", "title": "Burbank fire", "weight": "high"},
            {"sub": "burbank", "title": "Burbank parade", "weight": "high"},
            {"sub": "ClaudeCode", "title": "Claude tip", "weight": "normal"},
        ]
        written_content = []

        # Patch Path.write_text to capture the written content
        original_write_text = Path.write_text

        def capturing_write_text(self, text, *args, **kwargs):
            if ".reddit.md" in str(self):
                written_content.append(text)
            else:
                original_write_text(self, text, *args, **kwargs)

        with patch.object(Path, "write_text", capturing_write_text):
            with patch.object(Path, "mkdir"):
                generate_dream_context(posts)

        self.assertGreater(len(written_content), 0, "No dream context file was written")
        combined = "\n".join(written_content)
        self.assertIn("burbank", combined.lower())

    def test_state_ids_persist_after_ingest(self):
        state = {"seen_ids": []}
        posts_data = [{"data": {
            "id": "persist001",
            "title": "Test Post",
            "selftext": "",
            "score": 1,
            "num_comments": 0,
            "author": "user",
            "permalink": "/r/test/comments/persist001/",
            "stickied": False,
            "link_flair_text": "",
        }}]

        with patch.object(_mod, "fetch_subreddit", return_value=posts_data):
            with patch.object(_mod, "fetch_comments", return_value=[]):
                with patch.object(_mod, "vector_remember"):
                    with patch("time.sleep"):
                        ingest_subreddit("test", {"limit": 5, "source": "reddit"}, state)

        self.assertIn("persist001", state["seen_ids"])


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_full_ingest_cycle(self):
        """main() iterates all subreddits and saves state."""
        state = {"seen_ids": []}

        with patch.object(_mod, "load_state", return_value=state):
            with patch.object(_mod, "ingest_subreddit", return_value=(0, [])):
                with patch.object(_mod, "save_state") as mock_save:
                    with patch.object(_mod, "generate_dream_context"):
                        with patch.object(_nova_cfg, "post_both"):
                            _mod.main()

        mock_save.assert_called()

    def test_no_slack_notification_during_quiet_hours(self):
        state = {"seen_ids": []}
        notified = []

        with patch.object(_mod, "load_state", return_value=state):
            with patch.object(_mod, "ingest_subreddit", return_value=(3, [{"sub": "burbank", "title": "x", "weight": "high"}])):
                with patch.object(_mod, "save_state"):
                    with patch.object(_mod, "generate_dream_context"):
                        with patch.object(_mod, "_is_quiet_hours", return_value=True):
                            with patch.object(_nova_cfg, "post_both",
                                              side_effect=lambda *a, **kw: notified.append(a)):
                                _mod.main()

        self.assertEqual(len(notified), 0, "Should not notify during quiet hours")

    def test_vector_remember_includes_metadata(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            vector_remember("Reddit post text", metadata={
                "type": "reddit_post",
                "subreddit": "burbank",
                "post_id": "abc123",
            })

        self.assertEqual(len(captured), 1)
        self.assertIn("metadata", captured[0])
        self.assertEqual(captured[0]["metadata"]["subreddit"], "burbank")

    def test_stickied_posts_skipped(self):
        state = {"seen_ids": []}
        posts_data = [{"data": {
            "id": "sticky001",
            "title": "Mod announcement",
            "selftext": "",
            "score": 100,
            "num_comments": 5,
            "author": "mod",
            "permalink": "/r/test/",
            "stickied": True,
            "link_flair_text": "",
        }}]

        remembered = []
        with patch.object(_mod, "fetch_subreddit", return_value=posts_data):
            with patch.object(_mod, "fetch_comments", return_value=[]):
                with patch.object(_mod, "vector_remember",
                                  side_effect=lambda *a, **kw: remembered.append(a)):
                    with patch("time.sleep"):
                        count, _ = ingest_subreddit("test", {"limit": 5, "source": "reddit"}, state)

        self.assertEqual(count, 0, "Stickied posts must be skipped")
        self.assertEqual(len(remembered), 0)


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
        self.assertIsInstance(_mod.VECTOR_URL, str)
        self.assertIsInstance(_mod.USER_AGENT, str)
        self.assertIsInstance(_mod.SUBREDDITS, dict)
        self.assertGreater(len(_mod.SUBREDDITS), 0)

    def test_subreddits_include_local_sources(self):
        sources = {cfg.get("source") for cfg in SUBREDDITS.values()}
        self.assertIn("burbank", sources, "burbank subreddit source expected")

    def test_module_loads_without_network(self):
        self.assertIsNotNone(_mod)

    def test_user_agent_identifies_nova(self):
        self.assertIn("Nova", _mod.USER_AGENT)

    def test_state_file_path_valid(self):
        self.assertIsInstance(_mod.STATE_FILE, Path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
