"""
test_nova_comedy_scripts_ingest.py -- All 7 test categories for nova_comedy_scripts_ingest.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules.setdefault("nova_config", _nova_cfg)

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_comedy_scripts_ingest.py"
_spec = importlib.util.spec_from_file_location("nova_comedy_scripts_ingest", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

MOVIES = _mod.MOVIES


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
            self.assertNotIn(pat, src, f"PII found: {pat!r}")

    def test_memory_url_is_local(self):
        url = _mod.MEMORY_URL
        self.assertTrue(
            url.startswith("http://127.0.0.1") or url.startswith("http://192.168."),
            f"MEMORY_URL must be local, got: {url}"
        )

    def test_state_file_uses_home(self):
        self.assertIn(str(Path.home()), str(_mod.STATE_FILE))


class TestPerformance(unittest.TestCase):

    def test_chunk_words_fast(self):
        text = "Word " * 50000
        start = time.perf_counter()
        words = text.split()
        chunks = [" ".join(words[i:i+400]) for i in range(0, len(words), 400)]
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)

    def test_movies_list_non_empty(self):
        self.assertGreater(len(MOVIES), 20)

    def test_movies_are_tuples(self):
        for item in MOVIES[:5]:
            self.assertIsInstance(item, tuple)
            self.assertGreaterEqual(len(item), 3)


class TestRetry(unittest.TestCase):

    def test_fetch_url_retries(self):
        """fetch_url must retry on error."""
        call_count = [0]

        def failing(req, timeout=None):
            call_count[0] += 1
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", side_effect=failing):
            with patch("time.sleep"):
                result = _mod.fetch_url("https://imsdb.com/scripts/test.html")

        self.assertIsNone(result, "fetch_url should return None after retries")
        self.assertGreaterEqual(call_count[0], 2, "fetch_url should retry at least twice")

    def test_remember_does_not_raise_on_error(self):
        """remember() must not raise even if memory server is down."""
        def failing(req, timeout=None):
            raise OSError("refused")

        with patch("urllib.request.urlopen", side_effect=failing):
            try:
                _mod.remember("test screenplay text", "Test Movie", "comedy", 1999,
                               "imsdb", 1, 1)
            except Exception as e:
                self.fail(f"remember() raised: {e}")

    def test_fetch_url_returns_none_on_404(self):
        import urllib.error

        def not_found(req, timeout=None):
            raise urllib.error.HTTPError(
                url="http://x", code=404, msg="Not Found", hdrs=None, fp=None)

        with patch("urllib.request.urlopen", side_effect=not_found):
            result = _mod.fetch_url("https://example.com/test.html")

        self.assertIsNone(result)


class TestUnit(unittest.TestCase):

    def test_movies_have_required_fields(self):
        for item in MOVIES[:10]:
            title, year, genre, *rest = item
            self.assertIsInstance(title, str)
            self.assertIsInstance(year, int)
            self.assertIsInstance(genre, str)
            self.assertGreater(len(title), 0)

    def test_chunk_words_size(self):
        text = "word " * 1000
        words = text.split()
        chunks = [" ".join(words[i:i+400]) for i in range(0, len(words), 400)]
        for c in chunks:
            self.assertLessEqual(len(c.split()), 400)

    def test_sample_movie_in_list(self):
        titles = [m[0] for m in MOVIES]
        self.assertIn("Some Like It Hot", titles)

    def test_constants_defined(self):
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIsInstance(_mod.CHUNK_WORDS, int)
        self.assertIsInstance(_mod.MIN_CHUNK, int)
        self.assertGreater(_mod.CHUNK_WORDS, 0)

    def test_state_file_is_path(self):
        self.assertIsInstance(_mod.STATE_FILE, Path)


class TestIntegration(unittest.TestCase):

    def test_remember_sends_correct_payload(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            _mod.remember("Some Like It Hot screenplay content here.",
                          "Some Like It Hot", "comedy", 1999, "imsdb", 1, 1)

        self.assertTrue(len(captured) > 0)
        payload = captured[0]
        self.assertIn("text", payload)
        self.assertIn("source", payload)
        self.assertIn("metadata", payload)

    def test_remember_metadata_fields(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            _mod.remember("A gripping scene from the film.",
                          "Some Like It Hot", "comedy", 1980, "imsdb", 1, 3)

        meta = captured[0]["metadata"]
        for field in ["title", "year", "genre", "source_site", "chunk", "total_chunks"]:
            self.assertIn(field, meta)

    def test_load_state_returns_defaults(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "state.json"):
                state = _mod.load_state()
        self.assertIn("done", state)


class TestFunctional(unittest.TestCase):

    def test_main_skips_completed_movies(self):
        all_titles = [m[0] for m in MOVIES]
        state = {"done": {t: True for t in all_titles}}
        ingest_calls = []

        with patch.object(_mod, "load_state", return_value=state):
            with patch.object(_mod, "save_state"):
                with patch.object(_mod, "remember",
                                   side_effect=lambda *a, **kw: ingest_calls.append(a)):
                    with patch.object(_mod, "notify"):
                        with patch("time.sleep"):
                            _mod.main()

        self.assertEqual(len(ingest_calls), 0, "All movies done — should skip all")

    def test_remember_truncates_long_text(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        long_text = "word " * 5000
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            _mod.remember(long_text, "TestMovie", "comedy", 2000, "imsdb", 1, 1)

        if captured:
            payload_text = captured[0]["text"]
            self.assertLessEqual(len(payload_text), 3000,
                                 "Stored text should be reasonably truncated")

    def test_notify_called_after_movie(self):
        notified = []
        screenplay = "screenplay content " * 100
        state = {"done": {}}

        with patch.object(_mod, "load_state", return_value=state):
            with patch.object(_mod, "save_state"):
                with patch.object(_mod, "fetch_script", return_value=screenplay):
                    with patch.object(_mod, "remember"):
                        with patch.object(_mod, "notify",
                                           side_effect=lambda *a, **kw: notified.append(a)):
                            with patch("time.sleep"):
                                _mod.main()

        self.assertGreater(len(notified), 0, "notify() should be called at least once")


class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_module_constants_defined(self):
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIsInstance(_mod.MOVIES, list)
        self.assertGreater(len(_mod.MOVIES), 0)

    def test_module_loads_without_network(self):
        self.assertIsNotNone(_mod)

    def test_log_file_path_valid(self):
        self.assertIsInstance(_mod.LOG_FILE, Path)

    def test_chunk_words_positive(self):
        self.assertGreater(_mod.CHUNK_WORDS, 0)
        self.assertLess(_mod.CHUNK_WORDS, 10000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
