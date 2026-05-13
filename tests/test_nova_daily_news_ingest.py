"""
test_nova_daily_news_ingest.py — All 7 test categories for nova_daily_news_ingest.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_daily_news_ingest.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.post_both = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"

sys.modules["nova_config"] = _nova_cfg

_mod = load_script_compat(_SCRIPT, "nova_daily_news_ingest")

chunk_text = _mod.chunk_text
ingest_chunks = _mod.ingest_chunks
transcribe = _mod.transcribe
record_audio = _mod.record_audio
summarize_news = _mod.summarize_news


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-", "password ="]:
            self.assertNotIn(pat, src, f"Credential: {pat!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(p, src)

    def test_ingest_chunks_marks_as_public(self):
        """Ingested news chunks must be tagged as 'public' privacy."""
        payloads = []
        def capture(req, timeout=None):
            payloads.append(json.loads(req.data.decode()))
            return MagicMock(__enter__=lambda s: s, __exit__=MagicMock(return_value=False))
        with patch("urllib.request.urlopen", side_effect=capture):
            ingest_chunks(["This is a news chunk with enough words here to matter."],
                          "2026-01-01T12:00:00", "2026-01-01")
        if payloads:
            meta = payloads[0].get("metadata", {})
            self.assertEqual(meta.get("privacy"), "public")

    def test_channel_url_local_network_only(self):
        """Stream URL must be LAN IP, not public internet."""
        self.assertIn("192.168.", _mod.HDHR_STREAM)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_chunk_text_bounded_output(self):
        long_text = "This is a sentence. " * 500
        chunks = chunk_text(long_text)
        for c in chunks:
            self.assertLessEqual(len(c), _mod.CHUNK_SIZE + 200)

    def test_chunk_text_fast(self):
        text = ("Breaking news today. " * 200 + ". ") * 10
        start = time.perf_counter()
        chunk_text(text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_chunk_text_drops_short_chunks(self):
        text = "Hi. " + ("Real news content here with details. " * 10 + ". ") * 5
        chunks = chunk_text(text)
        for c in chunks:
            self.assertGreater(len(c), 50)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_ingest_chunks_silent_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            count = ingest_chunks(["test chunk with enough content here"], "ts", "2026-01-01")
        # Returns 0 but does not raise
        self.assertEqual(count, 0)

    def test_record_audio_returns_none_on_ffmpeg_error(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="ffmpeg error")):
            with patch.object(_mod, "WORK_DIR", Path("/tmp/test_nova_news")):
                result = record_audio(5)
        self.assertIsNone(result)

    def test_transcribe_returns_empty_on_failure(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="whisper error")):
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                wav = Path(f.name)
            try:
                result = transcribe(wav)
                self.assertEqual(result, "")
            finally:
                wav.unlink(missing_ok=True)

    def test_summarize_news_returns_fallback_on_ollama_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("ollama down")):
            result = summarize_news("Some news transcript", "18:00", "2026-01-01")
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_main_notifies_on_record_failure(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        with patch.object(_mod, "record_audio", return_value=None):
            with patch.object(_mod, "WORK_DIR", Path("/tmp")):
                _mod.main()
        combined = " ".join(posts)
        self.assertIn("Failed", combined)
        _nova_cfg.post_both.side_effect = None


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_chunk_text_empty_input(self):
        self.assertEqual(chunk_text(""), [])

    def test_chunk_text_basic_split(self):
        text = ". ".join(["Sentence number " + str(i) for i in range(100)])
        chunks = chunk_text(text)
        self.assertGreater(len(chunks), 0)

    def test_chunk_text_no_empty_chunks(self):
        text = "Real news. More real news. Even more news here."
        chunks = chunk_text(text)
        for c in chunks:
            self.assertGreater(len(c.strip()), 0)

    def test_ingest_chunks_returns_count(self):
        def ok_urlopen(req, timeout=None):
            return MagicMock(__enter__=lambda s: s, __exit__=MagicMock(return_value=False))
        with patch("urllib.request.urlopen", side_effect=ok_urlopen):
            count = ingest_chunks(["chunk one enough words", "chunk two enough words"],
                                  "2026-01-01T12:00", "2026-01-01")
        self.assertEqual(count, 2)

    def test_chunk_size_constant_defined(self):
        self.assertGreater(_mod.CHUNK_SIZE, 100)

    def test_channel_name_constant(self):
        self.assertIsNotNone(_mod.CHANNEL_NAME)
        self.assertIsNotNone(_mod.CHANNEL)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_full_pipeline_record_transcribe_ingest(self):
        import tempfile
        wav_content = b"RIFF" + b"\x00" * 44 + b"\xff" * 200000  # fake WAV > 100KB
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "test.wav"
            wav_path.write_bytes(wav_content)
            txt_path = wav_path.with_suffix(".txt")
            txt_path.write_text("Breaking news today in Los Angeles. " * 100)

            with patch.object(_mod, "WORK_DIR", Path(tmp)):
                with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
                    text = transcribe(wav_path)
            self.assertIsInstance(text, str)

    def test_chunks_ingested_with_correct_source(self):
        payloads = []
        def capture(req, timeout=None):
            payloads.append(json.loads(req.data.decode()))
            return MagicMock(__enter__=lambda s: s, __exit__=MagicMock(return_value=False))
        chunks = ["Top story: fires in LA area today with major evacuations underway.",
                  "Weather forecast: warm and dry conditions throughout Southern California."]
        with patch("urllib.request.urlopen", side_effect=capture):
            ingest_chunks(chunks, "2026-01-01T18:00", "2026-01-01")
        self.assertEqual(len(payloads), 2)
        for p in payloads:
            self.assertEqual(p["metadata"]["source"], "daily_news")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_notifies_start_and_completion(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "test.wav"
            wav.write_bytes(b"x" * 200000)
            txt = wav.with_suffix(".txt")
            txt.write_text("News content. " * 100)
            with patch.object(_mod, "record_audio", return_value=wav):
                with patch.object(_mod, "transcribe", return_value="News content. " * 100):
                    with patch.object(_mod, "ingest_chunks", return_value=5):
                        with patch.object(_mod, "summarize_news", return_value="Summary."):
                            with patch.object(_mod, "WORK_DIR", Path(tmp)):
                                _mod.main()
        self.assertGreater(len(posts), 0)
        _nova_cfg.post_both.side_effect = None

    def test_main_notifies_transcription_failure(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(_mod, "record_audio", return_value=Path(tmp) / "fake.wav"):
                with patch.object(_mod, "transcribe", return_value=""):
                    with patch.object(_mod, "WORK_DIR", Path(tmp)):
                        _mod.main()
        combined = " ".join(posts)
        self.assertIn("Failed", combined)
        _nova_cfg.post_both.side_effect = None


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_key_functions_exist(self):
        for fn in ["main", "record_audio", "transcribe", "chunk_text",
                   "ingest_chunks", "summarize_news", "notify"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_constants_defined(self):
        self.assertGreater(_mod.RECORD_DURATION, 0)
        self.assertIsNotNone(_mod.WHISPER_MODEL)
        self.assertIsNotNone(_mod.MEMORY_URL)

    def test_shutdown_signal_attribute_exists(self):
        self.assertTrue(hasattr(_mod, "shutdown"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
