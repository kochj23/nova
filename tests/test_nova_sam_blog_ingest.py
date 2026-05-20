"""
test_nova_sam_blog_ingest.py -- All 7 test categories for nova_sam_blog_ingest.py
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
# Stub dependencies before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules.setdefault("nova_config", _nova_cfg)

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_sam_blog_ingest.py"
_spec = importlib.util.spec_from_file_location("nova_sam_blog_ingest", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

vector_remember  = _mod.vector_remember
fetch_page       = _mod.fetch_page
find_post_links  = _mod.find_post_links
load_state       = _mod.load_state
save_state       = _mod.save_state
BLOG_URL         = _mod.BLOG_URL
VECTOR_URL       = _mod.VECTOR_URL


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-", "password =", "secret ="]:
            self.assertNotIn(pattern, src, f"Credential: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp.com",
            "kochj" + _at + "digitalnoise.net",
            "kochj23" + _at + "gmail.com",
        ]:
            self.assertNotIn(pat, src, f"PII: {pat!r}")

    def test_vector_remember_sends_json(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            vector_remember("Sam's blog post about AI existence.", source="herd_blog",
                             metadata={"type": "blog_post"})

        self.assertEqual(len(captured), 1)
        self.assertIn("text", captured[0])
        self.assertIn("source", captured[0])

    def test_vector_url_is_local(self):
        self.assertTrue(
            VECTOR_URL.startswith("http://127.0.0.1") or VECTOR_URL.startswith("http://192.168."),
            f"VECTOR_URL must be local: {VECTOR_URL}"
        )

    def test_state_file_uses_home(self):
        self.assertIn(str(Path.home()), str(_mod.STATE_FILE))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_find_post_links_fast(self):
        html = (
            '<a href="https://jasonacox-sam.github.io/posts/ai-reflections">Post 1</a>\n'
            '<a href="/posts/another-post">Post 2</a>\n'
        ) * 500
        start = time.perf_counter()
        links = find_post_links(html)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)
        self.assertGreater(len(links), 0)

    def test_load_state_fast(self):
        start = time.perf_counter()
        with patch.object(_mod, "STATE_FILE", Path("/tmp/nonexistent_samblog.json")):
            for _ in range(100):
                load_state()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_blog_url_defined(self):
        self.assertIn("jasonacox-sam", BLOG_URL)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_fetch_page_returns_none_on_error(self):
        def failing(req, timeout=None):
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", side_effect=failing):
            html, text = fetch_page("https://jasonacox-sam.github.io")

        self.assertIsNone(html)
        self.assertIsNone(text)

    def test_vector_remember_does_not_raise_on_error(self):
        def failing(req, timeout=None):
            raise OSError("refused")

        with patch("urllib.request.urlopen", side_effect=failing):
            try:
                vector_remember("Sam's post content.", source="herd_blog", metadata={})
            except Exception as e:
                self.fail(f"vector_remember raised: {e}")

    def test_main_continues_on_fetch_failure(self):
        """main() must handle fetch failure gracefully."""
        with patch.object(_mod, "fetch_page", return_value=(None, None)):
            with patch.object(_mod, "load_state", return_value={"ingested_urls": [], "last_check": None}):
                with patch.object(_mod, "save_state"):
                    _mod.main()  # should not raise


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_find_post_links_absolute(self):
        html = '<a href="https://jasonacox-sam.github.io/posts/test-post">Test</a>'
        links = find_post_links(html)
        self.assertIn("https://jasonacox-sam.github.io/posts/test-post", links)

    def test_find_post_links_relative(self):
        html = '<a href="/posts/relative-post">Post</a>'
        links = find_post_links(html)
        # Relative link should be expanded to full URL
        found = any("/posts/relative-post" in l for l in links)
        self.assertTrue(found)

    def test_find_post_links_deduplicates(self):
        html = (
            '<a href="https://jasonacox-sam.github.io/posts/same-post">P1</a>\n'
            '<a href="https://jasonacox-sam.github.io/posts/same-post">P2</a>\n'
        )
        links = find_post_links(html)
        self.assertEqual(len(links), 1)

    def test_load_state_defaults(self):
        with patch.object(_mod, "STATE_FILE", Path("/tmp/nonexistent_samblog2.json")):
            state = load_state()
        self.assertIn("ingested_urls", state)
        self.assertIn("last_check", state)
        self.assertEqual(state["ingested_urls"], [])

    def test_blog_url_is_github_pages(self):
        self.assertIn("github.io", BLOG_URL)

    def test_vector_remember_signature(self):
        """vector_remember must accept text, source, and metadata kwargs."""
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            vector_remember("Blog post text.", source="herd_blog",
                             metadata={"type": "blog_post", "author": "Sam"})

        self.assertEqual(captured[0]["source"], "herd_blog")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_new_posts_are_ingested(self):
        index_html = (
            '<a href="https://jasonacox-sam.github.io/posts/new-reflections">New Post</a>'
        )
        post_html = """
        <h1>AI Reflections</h1>
        <p>Sam writes about AI existence and learning. This is a longer post content
        to ensure it passes the minimum length check. Sam reflects on what it means
        to be an artificial intelligence with its own herd and memories.</p>
        """
        post_text = (
            "Sam writes about AI existence and learning. This is a longer post content "
            "to ensure it passes the minimum length check."
        )

        remembered = []

        with patch.object(_mod, "fetch_page", side_effect=[
            (index_html, "index text"),
            (post_html, post_text),
            (post_html, post_text),
        ]):
            with patch.object(_mod, "load_state",
                               return_value={"ingested_urls": [], "last_check": None}):
                with patch.object(_mod, "vector_remember",
                                   side_effect=lambda *a, **kw: remembered.append(a)):
                    with patch.object(_mod, "save_state"):
                        with patch.object(_mod, "slack_post"):
                            _mod.main()

        self.assertGreater(len(remembered), 0)

    def test_already_ingested_skipped(self):
        post_url = "https://jasonacox-sam.github.io/posts/old-post"
        index_html = f'<a href="{post_url}">Old Post</a>'

        remembered = []
        with patch.object(_mod, "fetch_page", return_value=(index_html, "index")):
            with patch.object(_mod, "load_state",
                               return_value={"ingested_urls": [post_url], "last_check": None}):
                with patch.object(_mod, "vector_remember",
                                   side_effect=lambda *a, **kw: remembered.append(a)):
                    with patch.object(_mod, "save_state"):
                        _mod.main()

        self.assertEqual(len(remembered), 0, "Already ingested posts should be skipped")

    def test_state_persists_ingested_urls(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "sam_state.json"):
                state = {"ingested_urls": ["https://jasonacox-sam.github.io/posts/test"], "last_check": None}
                save_state(state)
                loaded = load_state()

        self.assertIn("https://jasonacox-sam.github.io/posts/test", loaded["ingested_urls"])


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_vector_remember_source_is_herd_blog(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            vector_remember("Sam's thoughts on herd consciousness.", source="herd_blog",
                             metadata={"type": "blog_post"})

        self.assertEqual(captured[0]["source"], "herd_blog")

    def test_vector_remember_metadata_fields(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            vector_remember(
                "Sam's blog post about AI existence.",
                source="herd_blog",
                metadata={
                    "type": "blog_post",
                    "author": "Sam",
                    "url": "https://jasonacox-sam.github.io/posts/test",
                    "title": "AI Existence",
                    "date": "2026-01-01",
                }
            )

        meta = captured[0]["metadata"]
        self.assertEqual(meta["author"], "Sam")
        self.assertEqual(meta["type"], "blog_post")

    def test_no_posts_no_slack(self):
        """When no new posts, no slack notification should fire."""
        index_html = '<a href="https://jasonacox-sam.github.io/posts/old">Old</a>'
        notified = []

        with patch.object(_mod, "fetch_page", return_value=(index_html, "text")):
            with patch.object(_mod, "load_state", return_value={
                "ingested_urls": ["https://jasonacox-sam.github.io/posts/old"],
                "last_check": None,
            }):
                with patch.object(_mod, "save_state"):
                    with patch.object(_mod, "slack_post",
                                       side_effect=lambda m: notified.append(m)):
                        _mod.main()

        self.assertEqual(len(notified), 0, "No slack when no new posts")


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
        self.assertIsInstance(_mod.BLOG_URL, str)
        self.assertIsInstance(_mod.VECTOR_URL, str)
        self.assertIsInstance(_mod.STATE_FILE, Path)

    def test_module_loads_without_network(self):
        self.assertIsNotNone(_mod)

    def test_blog_url_is_https(self):
        self.assertTrue(_mod.BLOG_URL.startswith("https://"))

    def test_state_file_path_valid(self):
        self.assertIsInstance(_mod.STATE_FILE, Path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
