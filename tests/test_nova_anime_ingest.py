"""
test_nova_anime_ingest.py — All 7 test categories for nova_anime_ingest.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_anime_ingest.py"
_spec = importlib.util.spec_from_file_location("nova_anime_ingest", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

extract_text_from_html = _mod.extract_text_from_html
is_japanese            = _mod.is_japanese
chunk_text             = _mod.chunk_text
ingest_chunk           = _mod.ingest_chunk
search                 = _mod.search
load_state             = _mod.load_state
save_state             = _mod.save_state
ANIME_FILMS            = _mod.ANIME_FILMS
CHUNK_SIZE             = _mod.CHUNK_SIZE
SOURCE                 = _mod.SOURCE


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-", "password =", "secret ="]:
            self.assertNotIn(pattern, src, f"Credential pattern found: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src, "Hardcoded home path found in source")

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney.com",
            "kochj" + _at + "digitalnoise.net",
            "kochj23" + _at + "gmail.com",
        ]:
            self.assertNotIn(pat, src, f"PII email found: {pat!r}")

    def test_ingest_payload_is_json(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            return MagicMock()

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("Spirited Away is a Studio Ghibli film by Hayao Miyazaki.",
                         "Spirited Away", "https://example.com")

        self.assertEqual(len(captured), 1)
        self.assertIn("text", captured[0])
        self.assertIn("metadata", captured[0])

    def test_memory_url_is_local(self):
        url = _mod.MEMORY_URL
        self.assertTrue(
            url.startswith("http://127.0.0.1") or url.startswith("http://192.168."),
            f"MEMORY_URL must be local, got: {url}"
        )

    def test_user_agent_is_bot(self):
        src = _SCRIPT.read_text()
        self.assertIn("Nova/1.0", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_is_japanese_fast(self):
        text = "This is English text about anime films and Studio Ghibli." * 100
        start = time.perf_counter()
        for _ in range(10000):
            is_japanese(text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, f"is_japanese 10000x took {elapsed:.3f}s")

    def test_chunk_text_bounded(self):
        text = ("Spirited Away is a 2001 Japanese animated film.\n\n") * 100
        chunks = chunk_text(text)
        self.assertLessEqual(len(chunks), 200)

    def test_extract_text_fast(self):
        html = "<p>Spirited Away analysis paragraph.</p>" * 500
        start = time.perf_counter()
        extract_text_from_html(html)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, f"extract_text_from_html took {elapsed:.3f}s")

    def test_anime_films_list_not_empty(self):
        self.assertGreater(len(ANIME_FILMS), 50)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_ingest_chunk_returns_false_on_error(self):
        def failing(req, timeout=None):
            raise OSError("refused")

        with patch("urllib.request.urlopen", side_effect=failing):
            result = ingest_chunk("test", "Title", "https://example.com")

        self.assertFalse(result)

    def test_search_returns_empty_on_error(self):
        def failing(req, timeout=None):
            raise OSError("SearXNG down")

        with patch("urllib.request.urlopen", side_effect=failing):
            result = search("Spirited Away")

        self.assertEqual(result, [])

    def test_translate_to_english_returns_original_on_error(self):
        def failing(req, timeout=None):
            raise OSError("ollama down")

        with patch("urllib.request.urlopen", side_effect=failing):
            original = "テスト"
            result = _mod.translate_to_english(original)

        self.assertEqual(result, original)

    def test_fetch_url_returns_none_on_error(self):
        def failing(req, timeout=None):
            raise OSError("fetch error")

        with patch("urllib.request.urlopen", side_effect=failing):
            result = _mod.fetch_url("https://example.com/anime-review")

        self.assertIsNone(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_is_japanese_detects_english(self):
        self.assertFalse(is_japanese("This is all English text with no Japanese characters at all."))

    def test_is_japanese_detects_jp_text(self):
        # CJK characters
        jp = "千と千尋の神隠し" * 20
        self.assertTrue(is_japanese(jp))

    def test_extract_text_strips_script_tags(self):
        html = "<html><body><p>Good content</p><script>bad()</script></body></html>"
        result = extract_text_from_html(html)
        self.assertIn("Good content", result)
        self.assertNotIn("bad()", result)

    def test_extract_text_strips_nav(self):
        html = "<nav>Nav garbage</nav><p>Real article content here yes</p>"
        result = extract_text_from_html(html)
        self.assertNotIn("Nav garbage", result)

    def test_chunk_text_respects_chunk_size(self):
        text = ("A" * 800 + "\n\n") * 10
        chunks = chunk_text(text)
        for c in chunks:
            self.assertLessEqual(len(c), CHUNK_SIZE * 2)

    def test_chunk_text_skips_short_paragraphs(self):
        text = "ok\n\nThis is a valid paragraph about anime films and their themes and storytelling.\n\n"
        chunks = chunk_text(text)
        for c in chunks:
            self.assertGreaterEqual(len(c), 60)

    def test_load_state_defaults(self):
        with patch.object(_mod, "STATE_FILE", Path("/tmp/nonexistent_anime_xyz.json")):
            state = load_state()
        self.assertIn("completed", state)
        self.assertIn("total_chunks", state)
        self.assertEqual(state["total_chunks"], 0)

    def test_anime_films_are_tuples(self):
        for item in ANIME_FILMS[:5]:
            self.assertIsInstance(item, tuple)
            self.assertEqual(len(item), 2)

    def test_source_constant(self):
        self.assertEqual(SOURCE, "anime_films")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_chunk_then_ingest_pipeline(self):
        stored = []

        def mock_urlopen(req, timeout=None):
            stored.append(json.loads(req.data.decode()))
            return MagicMock()

        text = ("Spirited Away follows a young girl named Chihiro who enters "
                "a world of spirits while moving with her parents.\n\n") * 4
        chunks = chunk_text(text)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            for chunk in chunks:
                ingest_chunk(chunk, "Spirited Away", "https://example.com")

        self.assertGreater(len(stored), 0)
        for item in stored:
            self.assertEqual(item["source"], SOURCE)

    def test_state_persists_completed_films(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "anime_state.json"
            with patch.object(_mod, "STATE_FILE", state_file):
                state = {"completed": ["Spirited Away"], "total_chunks": 42}
                save_state(state)
                loaded = load_state()
        self.assertIn("Spirited Away", loaded["completed"])
        self.assertEqual(loaded["total_chunks"], 42)

    def test_ingest_chunk_metadata_has_required_fields(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            return MagicMock()

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("Princess Mononoke explores themes of nature vs industry.",
                         "Princess Mononoke", "https://example.com/mononoke")

        meta = captured[0]["metadata"]
        for field in ["source", "title", "url", "type", "privacy", "ingested_at"]:
            self.assertIn(field, meta)
        self.assertEqual(meta["type"], "anime_analysis")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_skips_completed_films(self):
        all_titles = [t for t, _ in ANIME_FILMS]
        state = {"completed": all_titles, "total_chunks": 9999}

        ingest_calls = []
        with patch.object(_mod, "load_state", return_value=state):
            with patch.object(_mod, "notify"):
                with patch.object(_mod, "ingest_chunk",
                                  side_effect=lambda *a, **kw: ingest_calls.append(a)):
                    with patch("time.sleep"):
                        _mod.main()

        self.assertEqual(len(ingest_calls), 0,
                         "Should skip all films when all are completed")

    def test_ingest_chunk_returns_true_on_success(self):
        ctx = MagicMock()
        ctx.__enter__ = lambda s: s
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=ctx):
            result = ingest_chunk("Akira is a landmark sci-fi anime film from 1988.",
                                  "Akira", "https://example.com/akira")
        self.assertTrue(result)

    def test_post_film_status_calls_notify(self):
        notified = []
        with patch.object(_mod, "notify", side_effect=lambda m: notified.append(m)):
            _mod.post_film_status(
                "Ghost in the Shell", 1, len(ANIME_FILMS),
                chunks_this_film=10, total_chunks=100,
                urls_fetched=5,
                all_chunks=["Ghost in the Shell is a 1995 cyberpunk anime."]
            )
        self.assertEqual(len(notified), 1)
        self.assertIn("Ghost in the Shell", notified[0])

    def test_fetch_url_skips_blocked_domains(self):
        for domain in ["https://youtube.com/watch?v=abc", "https://twitter.com/x",
                       "https://instagram.com/p/abc"]:
            result = _mod.fetch_url(domain)
            self.assertIsNone(result, f"Should skip: {domain}")


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
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIsInstance(_mod.SEARXNG_URL, str)
        self.assertIsInstance(_mod.CHUNK_SIZE, int)
        self.assertIsInstance(_mod.ANIME_FILMS, list)

    def test_search_angles_have_placeholder(self):
        for angle in _mod.SEARCH_ANGLES:
            self.assertIn("{query}", angle,
                          f"Search angle missing {{query}} placeholder: {angle!r}")

    def test_module_loads_without_network(self):
        self.assertIsNotNone(_mod)

    def test_log_file_uses_home_path(self):
        log_path = str(_mod.LOG_FILE)
        self.assertIn(str(Path.home()), log_path)

    def test_anime_films_have_titles(self):
        for title, query in ANIME_FILMS[:10]:
            self.assertGreater(len(title), 0)
            self.assertGreater(len(query), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
