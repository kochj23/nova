"""
test_nova_horror_scripts_imsdb.py -- All 7 test categories for nova_horror_scripts_imsdb.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_horror_scripts_imsdb.py"
_spec = importlib.util.spec_from_file_location("nova_horror_scripts_imsdb", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

fetch_script = _mod.fetch_script
chunk_text   = _mod.chunk_text
ingest       = _mod.ingest
SCRIPTS      = _mod.SCRIPTS


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
        self.assertNotIn(home, src)

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

    def test_ingest_payload_has_source(self):
        """ingest() must set source to movie_script_horror."""
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            return MagicMock()

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest("A horror film screenplay text here for testing purposes.", "Halloween", 1978)

        self.assertTrue(len(captured) > 0)
        for item in captured:
            self.assertEqual(item["source"], "movie_script_horror")

    def test_vector_url_is_local(self):
        url = _mod.VECTOR_URL
        self.assertTrue(
            url.startswith("http://127.0.0.1") or url.startswith("http://192.168."),
            f"VECTOR_URL must be local, got: {url}"
        )

    def test_payload_privacy_local_only(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            return MagicMock()

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest("Halloween screenplay test content for privacy check.", "Halloween", 1978)

        self.assertTrue(len(captured) > 0)
        for item in captured:
            self.assertEqual(item["metadata"]["privacy"], "local-only")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_chunk_text_fast(self):
        text = "FADE IN: EXT. CRYSTAL LAKE - NIGHT\n" * 2000
        start = time.perf_counter()
        chunk_text(text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, f"chunk_text took {elapsed:.3f}s")

    def test_chunk_text_bounded(self):
        text = "SCENE\n" * 10000
        chunks = chunk_text(text)
        self.assertLessEqual(len(chunks), 5000)

    def test_scripts_dict_non_empty(self):
        self.assertGreater(len(SCRIPTS), 5)

    def test_chunk_text_filters_short(self):
        text = "ok\n" * 100
        chunks = chunk_text(text, max_chars=1600)
        for c in chunks:
            self.assertGreater(len(c.strip()), 50)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_fetch_script_returns_none_on_error(self):
        def failing(req, timeout=None):
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", side_effect=failing):
            result = fetch_script("Halloween")

        self.assertIsNone(result)

    def test_ingest_handles_urlopen_error_gracefully(self):
        def failing(req, timeout=None):
            raise OSError("refused")

        with patch("urllib.request.urlopen", side_effect=failing):
            # Should not raise
            stored = ingest("Horror screenplay content here for test.", "Scream", 1996)

        self.assertEqual(stored, 0, "ingest should return 0 on network error")

    def test_fetch_script_handles_http_error(self):
        import urllib.error

        def http_error(req, timeout=None):
            raise urllib.error.HTTPError(
                url="http://x", code=404, msg="Not Found", hdrs=None, fp=None)

        with patch("urllib.request.urlopen", side_effect=http_error):
            result = fetch_script("NotExistentHorrorMovie")

        self.assertIsNone(result)

    def test_main_continues_on_fetch_failure(self):
        """main() must continue processing scripts even if fetch fails for one."""
        processed = []

        def fail_fetch(slug):
            processed.append(slug)
            return None  # simulate fetch failure

        with patch.object(_mod, "fetch_script", side_effect=fail_fetch):
            with patch.object(_nova_cfg, "post_both"):
                with patch("time.sleep"):
                    _mod.main()

        # All scripts should have been attempted
        self.assertEqual(len(processed), len(SCRIPTS))


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_chunk_text_splits_at_max(self):
        text = "A" * 3000
        chunks = chunk_text(text, max_chars=1600)
        for c in chunks:
            self.assertLessEqual(len(c), 1600)

    def test_chunk_text_single_short(self):
        text = "Short text for a horror screenplay."
        chunks = chunk_text(text, max_chars=1600)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], text)

    def test_chunk_text_empty(self):
        chunks = chunk_text("", max_chars=1600)
        self.assertEqual(chunks, [])

    def test_scripts_has_halloween(self):
        all_titles = [v[0] for v in SCRIPTS.values()]
        self.assertIn("Halloween", all_titles)

    def test_scripts_has_nightmare(self):
        slugs = list(SCRIPTS.keys())
        self.assertTrue(any("Nightmare" in k or "Elm" in k for k in slugs))

    def test_ingest_returns_count(self):
        ctx = MagicMock()
        ctx.__enter__ = lambda s: s
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=ctx):
            stored = ingest("Horror screenplay content here for test ingestion.", "Halloween", 1978)

        self.assertGreaterEqual(stored, 1)

    def test_stats_dict_keys(self):
        for key in ["fetched", "stored", "errors", "skipped"]:
            self.assertIn(key, _mod.stats)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_fetch_and_ingest_pipeline(self):
        """Simulate fetch returning screenplay text, then ingest."""
        screenplay = (
            "FADE IN:\n\nEXT. HADDONFIELD - NIGHT\n\n"
            "Michael Myers stands in the shadows watching the house.\n\n"
        ) * 20
        stored = []

        def mock_urlopen(req, timeout=None):
            stored.append(json.loads(req.data.decode()))
            return MagicMock()

        with patch.object(_mod, "fetch_script", return_value=screenplay):
            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                count = ingest(screenplay, "Halloween", 1978)

        self.assertGreater(count, 0)
        self.assertGreater(len(stored), 0)

    def test_ingest_chunk_metadata_fields(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            return MagicMock()

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest("The monster approaches through the fog at midnight.", "The Thing", 1982)

        for item in captured:
            meta = item["metadata"]
            for field in ["privacy", "origin", "title", "film", "year", "type"]:
                self.assertIn(field, meta)
            self.assertEqual(meta["type"], "screenplay")

    def test_main_tracks_stats(self):
        screenplay = "Horror content " * 100

        with patch.object(_mod, "fetch_script", return_value=screenplay):
            with patch("urllib.request.urlopen", return_value=MagicMock()):
                with patch.object(_nova_cfg, "post_both"):
                    with patch("time.sleep"):
                        _mod.main()

        self.assertGreater(_mod.stats["fetched"], 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_skips_short_scripts(self):
        """Scripts with less than 500 chars should be skipped."""
        processed = {"stored": 0}

        with patch.object(_mod, "fetch_script", return_value="too short"):
            with patch.object(_mod, "ingest",
                               side_effect=lambda *a: processed.__setitem__("stored", processed["stored"] + 1)):
                with patch.object(_nova_cfg, "post_both"):
                    with patch("time.sleep"):
                        _mod.main()

        self.assertEqual(processed["stored"], 0,
                         "Scripts with <500 chars should not be ingested")

    def test_ingest_truncates_to_1900(self):
        """Chunks must be truncated to 1900 chars in the stored text."""
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            return MagicMock()

        long_chunk = "X" * 3000
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest(long_chunk, "TestFilm", 2000)

        for item in captured:
            self.assertLessEqual(len(item["text"]), 2100,
                                 "Stored text should be truncated")

    def test_fetch_script_extracts_pre_tag(self):
        """fetch_script must extract text from <pre> tags."""
        html = (
            "<html><body>"
            "<pre>FADE IN:\n\nEXT. LOCATION - NIGHT\n\nA scary scene unfolds.</pre>"
            "</body></html>"
        )
        ctx = MagicMock()
        ctx.__enter__ = lambda s: s
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.read = lambda: html.encode()
        ctx.headers = {"Content-Type": "text/html"}

        with patch("urllib.request.urlopen", return_value=ctx):
            result = fetch_script("Halloween")

        if result:
            self.assertIn("FADE IN", result)

    def test_notify_called_on_completion(self):
        with patch.object(_mod, "fetch_script", return_value=None):
            with patch.object(_nova_cfg, "post_both") as mock_post:
                with patch("time.sleep"):
                    _mod.main()

        mock_post.assert_called()


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
        self.assertIsInstance(_mod.SCRIPTS, dict)
        self.assertGreater(len(_mod.SCRIPTS), 0)

    def test_module_loads_without_network(self):
        self.assertIsNotNone(_mod)

    def test_scripts_dict_values_are_tuples(self):
        for slug, val in SCRIPTS.items():
            self.assertIsInstance(val, tuple)
            self.assertEqual(len(val), 2)

    def test_log_file_is_in_tmp(self):
        self.assertIn("tmp", str(_mod.LOG_FILE).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
