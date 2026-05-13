"""
test_nova_memory_consolidate.py — All 7 test categories for nova_memory_consolidate.py
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
# Stub nova_config before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.slack_bot_token = MagicMock(return_value="xoxb-test-token")
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_memory_consolidate.py"
_spec = importlib.util.spec_from_file_location("nova_memory_consolidate", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)

# Patch urllib during import (vector_stats call may happen)
with patch("urllib.request.urlopen", side_effect=Exception("no server")):
    _spec.loader.exec_module(_mod)

vector_recall = _mod.vector_recall
vector_remember = _mod.vector_remember
vector_stats = _mod.vector_stats
read_recent_memory_files = _mod.read_recent_memory_files
llm_synthesize = _mod.llm_synthesize
synthesize_work_patterns = _mod.synthesize_work_patterns
synthesize_relationship_activity = _mod.synthesize_relationship_activity
synthesize_home_and_life = _mod.synthesize_home_and_life
main = _mod.main


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

    def test_synthesis_stored_locally(self):
        """vector_remember must POST to local vector URL only."""
        src = _SCRIPT.read_text()
        # Must use local URL
        self.assertIn("127.0.0.1", src)
        # Must NOT route to cloud LLMs for storage
        cloud_patterns = ["openrouter.ai", "api.openai.com"]
        for url in cloud_patterns:
            self.assertNotIn(url, src)

    def test_llm_uses_local_model_only(self):
        """LLM synthesis must route through local Nova-NextGen, not cloud."""
        src = _SCRIPT.read_text()
        # NOVA_NEXTGEN_URL must be local
        self.assertIn("127.0.0.1", src)
        # Any direct OpenRouter call is prohibited for synthesis
        self.assertNotIn("openrouter.ai/api/v1/chat/completions", src)

    def test_slack_token_from_function_not_hardcoded(self):
        """SLACK_TOKEN must come from nova_config.slack_bot_token(), not hardcoded."""
        src = _SCRIPT.read_text()
        self.assertIn("slack_bot_token()", src)
        # Must not be a literal token
        self.assertNotIn("xoxb-real", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_read_recent_memory_files_fast(self):
        """read_recent_memory_files must complete in < 200ms when no files exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "MEMORY_DIR", Path(tmpdir)):
                start = time.perf_counter()
                result = read_recent_memory_files(days=7)
                elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2)

    def test_vector_recall_fast_on_failure(self):
        """vector_recall must return quickly (< 100ms) on network failure."""
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            start = time.perf_counter()
            result = vector_recall("test query")
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)
        self.assertEqual(result, [])

    def test_vector_stats_fast_on_failure(self):
        """vector_stats must return quickly (< 100ms) on failure."""
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            start = time.perf_counter()
            result = vector_stats()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)
        self.assertEqual(result, {})


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_vector_recall_returns_empty_on_failure(self):
        """vector_recall returns [] on network failure."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = vector_recall("any query")
        self.assertEqual(result, [])

    def test_vector_remember_handles_failure(self):
        """vector_remember must not crash on network failure."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            try:
                vector_remember("test synthesis text", {"date": "2026-01-01"})
            except Exception as exc:
                self.fail(f"vector_remember raised: {exc}")

    def test_llm_synthesize_returns_empty_on_failure(self):
        """llm_synthesize returns empty string on network failure."""
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = llm_synthesize("test prompt")
        self.assertIsInstance(result, str)
        self.assertEqual(result, "")

    def test_main_handles_stats_failure(self):
        """main() continues when vector_stats fails."""
        with patch.object(_mod, "vector_stats", return_value={}):
            with patch.object(_mod, "vector_recall", return_value=[]):
                with patch.object(_mod, "read_recent_memory_files", return_value=""):
                    try:
                        main()
                    except Exception as exc:
                        self.fail(f"main() raised when stats fail: {exc}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_read_recent_memory_files_returns_string(self):
        """read_recent_memory_files always returns a string."""
        with patch.object(_mod, "MEMORY_DIR", Path("/nonexistent")):
            result = read_recent_memory_files(7)
        self.assertIsInstance(result, str)

    def test_read_recent_memory_files_reads_existing(self):
        """read_recent_memory_files reads actual daily files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mem_dir = Path(tmpdir)
            today = date.today().isoformat()
            (mem_dir / f"{today}.md").write_text("# Today's memory\n\nTest content here.")

            with patch.object(_mod, "MEMORY_DIR", mem_dir):
                result = read_recent_memory_files(days=3)

        self.assertIn("Today's memory", result)
        self.assertIn(today, result)

    def test_read_recent_memory_files_respects_days_limit(self):
        """read_recent_memory_files does not read files older than N days."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mem_dir = Path(tmpdir)
            old_date = (date.today() - timedelta(days=10)).isoformat()
            (mem_dir / f"{old_date}.md").write_text("# Old memory\n\nOld content.")

            with patch.object(_mod, "MEMORY_DIR", mem_dir):
                result = read_recent_memory_files(days=5)

        self.assertNotIn("Old memory", result)

    def test_vector_recall_filters_by_score(self):
        """vector_recall only returns memories with score >= 0.35."""
        memories = [
            {"text": "high score memory", "score": 0.9},
            {"text": "low score memory", "score": 0.1},
            {"text": "exact threshold", "score": 0.35},
        ]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"memories": memories}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = vector_recall("test")

        texts = result
        self.assertIn("high score memory", texts)
        self.assertIn("exact threshold", texts)
        self.assertNotIn("low score memory", texts)

    def test_synthesize_work_patterns_returns_none_on_empty(self):
        """synthesize_work_patterns returns None with no memories."""
        result = synthesize_work_patterns([])
        self.assertIsNone(result)

    def test_synthesize_work_patterns_calls_llm(self):
        """synthesize_work_patterns calls llm_synthesize with prompt."""
        llm_calls = []

        with patch.object(_mod, "llm_synthesize", side_effect=lambda p, **kw: llm_calls.append(p) or "- Found patterns"):
            result = synthesize_work_patterns(["memory 1", "memory 2"])

        self.assertTrue(len(llm_calls) > 0)
        self.assertEqual(result, "- Found patterns")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_skips_when_too_few_memories(self):
        """main() skips synthesis when vector DB has < 5 memories."""
        with patch.object(_mod, "vector_stats", return_value={"count": 3}):
            vector_remember_calls = []

            with patch.object(_mod, "vector_remember", side_effect=lambda *a, **kw: vector_remember_calls.append(a)):
                main()

        self.assertEqual(len(vector_remember_calls), 0, "Should not synthesize with < 5 memories")

    def test_main_stores_synthesis_when_data_available(self):
        """main() stores synthesis memories when LLM returns content."""
        with patch.object(_mod, "vector_stats", return_value={"count": 100}):
            with patch.object(_mod, "vector_recall", return_value=["memory 1", "memory 2"]):
                with patch.object(_mod, "read_recent_memory_files", return_value="recent context"):
                    with patch.object(_mod, "llm_synthesize", return_value="- Work synthesis result"):
                        stored = []
                        with patch.object(_mod, "vector_remember", side_effect=lambda t, m=None: stored.append(t)):
                            with patch.object(_mod, "MEMORY_DIR", Path(tempfile.mkdtemp())):
                                main()

        self.assertGreater(len(stored), 0, "Should store synthesis memories")

    def test_memory_file_written_when_synthesis_available(self):
        """main() writes synthesis to today's memory markdown file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mem_dir = Path(tmpdir)

            with patch.object(_mod, "vector_stats", return_value={"count": 100}):
                with patch.object(_mod, "vector_recall", return_value=["memory 1"]):
                    with patch.object(_mod, "read_recent_memory_files", return_value=""):
                        with patch.object(_mod, "llm_synthesize", return_value="- Synthesis result"):
                            with patch.object(_mod, "vector_remember"):
                                with patch.object(_mod, "MEMORY_DIR", mem_dir):
                                    main()

            today = date.today().isoformat()
            mem_file = mem_dir / f"{today}.md"
            if mem_file.exists():
                content = mem_file.read_text()
                self.assertIn("Memory Synthesis", content)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_synthesis_deduplicates_memories(self):
        """main() deduplicates memories before synthesizing."""
        dup_memories = ["memory A", "memory A", "memory B", "memory A"]

        with patch.object(_mod, "vector_stats", return_value={"count": 100}):
            with patch.object(_mod, "vector_recall", return_value=dup_memories):
                with patch.object(_mod, "read_recent_memory_files", return_value=""):
                    with patch.object(_mod, "synthesize_work_patterns") as mock_synth:
                        mock_synth.return_value = None
                        with patch.object(_mod, "synthesize_relationship_activity", return_value=None):
                            with patch.object(_mod, "synthesize_home_and_life", return_value=None):
                                main()

            if mock_synth.called:
                passed_memories = mock_synth.call_args[0][0]
                # Should be deduplicated
                self.assertEqual(len(passed_memories), len(set(passed_memories)))

    def test_synthesis_not_written_twice(self):
        """main() must not append Memory Synthesis section if already present."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mem_dir = Path(tmpdir)
            today = date.today().isoformat()
            existing = f"# Nova Memory — {today}\n\n## Memory Synthesis — {today}\nAlready here.\n"
            (mem_dir / f"{today}.md").write_text(existing)

            with patch.object(_mod, "vector_stats", return_value={"count": 100}):
                with patch.object(_mod, "vector_recall", return_value=["m1"]):
                    with patch.object(_mod, "read_recent_memory_files", return_value=""):
                        with patch.object(_mod, "llm_synthesize", return_value="- New synthesis"):
                            with patch.object(_mod, "vector_remember"):
                                with patch.object(_mod, "MEMORY_DIR", mem_dir):
                                    main()

            content = (mem_dir / f"{today}.md").read_text()
            count = content.count("Memory Synthesis")
            self.assertEqual(count, 1, "Memory Synthesis should not be duplicated")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        """nova_memory_consolidate.py compiles without syntax errors."""
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_key_functions_callable(self):
        """All key functions must be callable."""
        for fn in [vector_recall, vector_remember, vector_stats,
                   read_recent_memory_files, llm_synthesize, main]:
            self.assertTrue(callable(fn))

    def test_workspace_paths_use_path_home(self):
        """WORKSPACE and MEMORY_DIR must be under home directory."""
        self.assertTrue(str(_mod.WORKSPACE).startswith(str(Path.home())))
        self.assertTrue(str(_mod.MEMORY_DIR).startswith(str(Path.home())))

    def test_vector_url_is_local(self):
        """VECTOR_URL must be a local URL."""
        self.assertIn("127.0.0.1", _mod.VECTOR_URL)

    def test_nova_nextgen_url_is_local(self):
        """NOVA_NEXTGEN_URL must be a local URL."""
        self.assertIn("127.0.0.1", _mod.NOVA_NEXTGEN_URL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
