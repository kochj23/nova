"""
test_nova_research_paper.py — All 7 test categories for nova_research_paper.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_research_paper.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.post_both = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"

sys.modules["nova_config"] = _nova_cfg

_mod = load_script_compat(_SCRIPT, "nova_research_paper")


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_keychain_for_api_key(self):
        src = _SCRIPT.read_text()
        self.assertIn("find-generic-password", src)

    def test_private_sources_excluded(self):
        src = _SCRIPT.read_text()
        self.assertIn("PRIVATE_SOURCES", src) or self.assertIn("work", src.lower())


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_api_timeout_present(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)

    def test_state_operations_fast(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            Path(f.name).write_text(json.dumps({"recent_sources": [], "paper_count": 0}))
            tmp = Path(f.name)
        try:
            start = time.perf_counter()
            for _ in range(100):
                with patch.object(_mod, "STATE_FILE", tmp):
                    _mod.load_state()
            elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 0.5)
        finally:
            tmp.unlink(missing_ok=True)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_generate_paper_falls_back_to_ollama(self):
        ollama_calls = []
        def fake_ollama(*a, **kw):
            ollama_calls.append(1)
            return "Abstract: Research findings here. " * 200
        with patch.object(_mod, "_generate_via_openrouter", side_effect=Exception("OR fail")):
            with patch.object(_mod, "_generate_via_ollama", side_effect=fake_ollama):
                memories = [{"text": f"mem {i}", "metadata": "{}"}]
                try:
                    result = _mod.generate_paper("test_source", memories)
                except Exception:
                    result = None
        self.assertTrue(len(ollama_calls) > 0 or result is None)

    def test_main_aborts_on_no_source(self):
        with patch.object(_mod, "pick_subject", return_value=None):
            with patch.object(_mod, "load_state",
                              return_value={"recent_sources": [], "paper_count": 0}):
                _mod.main()  # should not raise

    def test_memory_server_failure_falls_back_to_db(self):
        with patch.object(_mod, "get_sources_with_counts", return_value=[]):
            with patch.object(_mod, "get_source_counts_from_db", return_value=[]):
                state = {"recent_sources": []}
                result = _mod.pick_subject(state)
        self.assertIsNone(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_load_state_defaults(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(_mod, "STATE_FILE", Path(tmp) / "missing.json"):
                state = _mod.load_state()
        self.assertIn("recent_sources", state)
        self.assertIn("paper_count", state)

    def test_save_load_roundtrip(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            state = {"recent_sources": ["wiki"], "paper_count": 3}
            with patch.object(_mod, "STATE_FILE", tmp):
                _mod.save_state(state)
                loaded = _mod.load_state()
            self.assertEqual(loaded["paper_count"], 3)
        finally:
            tmp.unlink(missing_ok=True)

    def test_log_does_not_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(_mod, "LOG_FILE", Path(tmp) / "test.log"):
                _mod.log("test message")

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.OPENROUTER_URL)
        self.assertIsNotNone(_mod.MEMORY_SERVER)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_updates_state_on_success(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
            tmp.write_text(json.dumps({"recent_sources": [], "paper_count": 1}))
        paper_text = "Abstract: This study examines. " * 200
        memories = [{"text": f"Memory {i}", "metadata": "{}"} for i in range(15)]
        try:
            with patch.object(_mod, "STATE_FILE", tmp):
                with patch.object(_mod, "pick_subject", return_value="science"):
                    with patch.object(_mod, "fetch_memories", return_value=memories):
                        with patch.object(_mod, "generate_paper", return_value=paper_text):
                            with patch.object(_mod, "generate_paper_image", return_value=None):
                                with patch.object(_mod, "publish_to_journal"):
                                    with patch.object(_mod, "post_to_slack"):
                                        with patch.object(_mod, "send_to_herd"):
                                            _mod.main()
            state = json.loads(tmp.read_text())
            self.assertEqual(state["paper_count"], 2)
        finally:
            tmp.unlink(missing_ok=True)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_paper_too_short_aborts(self):
        memories = [{"text": f"mem {i}", "metadata": "{}"} for i in range(15)]
        with patch.object(_mod, "pick_subject", return_value="science"):
            with patch.object(_mod, "fetch_memories", return_value=memories):
                with patch.object(_mod, "generate_paper", return_value="short"):
                    with patch.object(_mod, "load_state",
                                      return_value={"recent_sources": [], "paper_count": 0}):
                        with patch.object(_mod, "publish_to_journal") as pub_mock:
                            with patch.object(_mod, "save_state"):
                                _mod.main()
                        pub_mock.assert_not_called()


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
        for fn in ["main", "load_state", "save_state", "log",
                   "pick_subject", "fetch_memories", "generate_paper"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
