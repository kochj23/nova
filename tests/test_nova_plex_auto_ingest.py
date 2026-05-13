"""
test_nova_plex_auto_ingest.py — All 7 test categories for nova_plex_auto_ingest.py
Written by Jordan Koch.
"""
import importlib.util, json, sys, time, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg
# Stub nova_plex import inside plex_auto_ingest
sys.modules["nova_plex"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_plex_auto_ingest.py"
_spec = importlib.util.spec_from_file_location("nova_plex_auto_ingest", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

classify_content = _mod.classify_content
chunk_text = _mod.chunk_text
load_state = _mod.load_state
save_state = _mod.save_state
ingest_chunks = _mod.ingest_chunks


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_"]:
            self.assertNotIn(pat, src)
    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)
    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)
    def test_plex_token_from_keychain(self):
        src = _SCRIPT.read_text()
        self.assertIn("nova-plex-token", src)
    def test_memory_url_local_network(self):
        self.assertIn("192.168.", _mod.MEMORY_URL)
    def test_work_dir_on_data_volume(self):
        self.assertIn("/Volumes/Data", str(_mod.WORK_DIR))
    def test_privacy_metadata_in_chunks(self):
        src = _SCRIPT.read_text()
        self.assertIn("local-only", src)


class TestPerformance(unittest.TestCase):
    def test_classify_content_fast(self):
        start = time.perf_counter()
        for _ in range(1000):
            classify_content("Jeopardy", "", ["Game Show"], "quiz contestant")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)
    def test_chunk_text_respects_chunk_size(self):
        long_text = "This is a sentence. " * 200
        chunks = chunk_text(long_text)
        for c in chunks:
            self.assertLessEqual(len(c), _mod.CHUNK_SIZE + 200)
    def test_chunk_text_filters_short_chunks(self):
        chunks = chunk_text("Short. Short. Short.")
        # Short chunks (<50 chars) should be filtered or merged
        for c in chunks:
            self.assertGreater(len(c), 50)


class TestRetry(unittest.TestCase):
    def test_ingest_chunks_handles_network_failure(self):
        chunks = ["Test chunk text " * 10]
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = ingest_chunks(chunks, "documentary", {"title": "Test"})
        self.assertEqual(result, 0)  # 0 ingested
    def test_save_state_creates_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "WORK_DIR", Path(tmpdir)):
                with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "state.json"):
                    save_state({"ingested": {}, "last_run": 0})
                    self.assertTrue((Path(tmpdir) / "state.json").exists())


class TestUnit(unittest.TestCase):
    def test_classify_documentary(self):
        result = classify_content("Planet Earth", "", ["Documentary"], "nature wildlife")
        self.assertEqual(result, "documentary")
    def test_classify_comedy(self):
        result = classify_content("Stand Up Show", "", ["Comedy"], "funny joke laugh")
        self.assertEqual(result, "comedy")
    def test_classify_default_documentary(self):
        result = classify_content("Unknown Show", "", [], "random text")
        self.assertEqual(result, "documentary")
    def test_chunk_text_splits_long_text(self):
        text = ("This is a test sentence. " * 100)
        chunks = chunk_text(text)
        self.assertGreater(len(chunks), 1)
    def test_chunk_text_empty(self):
        result = chunk_text("")
        self.assertEqual(result, [])
    def test_save_and_load_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "WORK_DIR", Path(tmpdir)):
                with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "state.json"):
                    state = {"ingested": {"key1": {"title": "Test"}}, "last_run": 12345}
                    save_state(state)
                    loaded = load_state()
        self.assertIn("key1", loaded["ingested"])
    def test_path_translation_external3(self):
        """Plex /external3/ paths must be translated to /Volumes/external/."""
        src = _SCRIPT.read_text()
        self.assertIn("/external3/", src)
        self.assertIn("/Volumes/external/", src)
    def test_max_duration_defined(self):
        self.assertGreater(_mod.MAX_DURATION_MIN, 0)
        self.assertLessEqual(_mod.MAX_DURATION_MIN, 360)


class TestIntegration(unittest.TestCase):
    def test_ingest_chunks_returns_count(self):
        chunks = ["This is a test chunk that is long enough to pass. " * 3]
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = ingest_chunks(chunks, "documentary", {"title": "Test Doc"})
        self.assertEqual(result, 1)
    def test_process_item_skips_too_long(self):
        item = {
            "type": "movie", "title": "Long Film",
            "grandparentTitle": "", "ratingKey": "123",
            "duration": _mod.MAX_DURATION_MIN * 60 * 1000 + 1,
            "Genre": [], "Media": []
        }
        result = _mod.process_item(item, "Movies")
        self.assertFalse(result)
    def test_process_item_skips_too_short(self):
        item = {
            "type": "episode", "title": "Short Ep",
            "grandparentTitle": "Show", "ratingKey": "124",
            "duration": 0, "Genre": [], "Media": []
        }
        result = _mod.process_item(item, "TV Shows")
        self.assertFalse(result)


class TestFunctional(unittest.TestCase):
    def test_classify_action_content(self):
        result = classify_content("Action Movie", "", ["Action", "Adventure"], "fight chase hero battle")
        self.assertEqual(result, "action")
    def test_chunk_metadata_privacy(self):
        chunks = ["Test content chunk that is quite long enough to be useful for ingestion."]
        sent = []
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        def capture(req, timeout=None):
            sent.append(json.loads(req.data.decode()))
            return mock_resp
        with patch("urllib.request.urlopen", side_effect=capture):
            ingest_chunks(chunks, "documentary", {"title": "Test"})
        self.assertTrue(any(d.get("metadata", {}).get("privacy") == "local-only" for d in sent))


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.PLEX_URL, str)
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIsInstance(_mod.WORK_DIR, Path)
        self.assertIsInstance(_mod.VECTOR_MAP, dict)
        self.assertIsInstance(_mod.SECTIONS, dict)
        self.assertIsInstance(_mod.CHUNK_SIZE, int)
    def test_functions_exist(self):
        for fn in ("plex_token", "plex_get", "get_recently_added",
                   "load_state", "save_state", "extract_audio", "transcribe",
                   "classify_content", "chunk_text", "ingest_chunks",
                   "process_item", "trigger_library_scan", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
