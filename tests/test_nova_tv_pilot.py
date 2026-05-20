"""
test_nova_tv_pilot.py — All 7 test categories for nova_tv_pilot.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_tv_pilot.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.post_both = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"

_nova_image_utils = MagicMock()
_nova_image_utils.ensure_backend = MagicMock(return_value=True)
_nova_image_utils.generate_image = MagicMock(return_value=None)

sys.modules["nova_config"] = _nova_cfg
sys.modules["nova_image_utils"] = _nova_image_utils

_mod = load_script_compat(_SCRIPT, "nova_tv_pilot")

log = _mod.log
load_state = _mod.load_state
save_state = _mod.save_state


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

    def test_keychain_used_for_api_key(self):
        src = _SCRIPT.read_text()
        self.assertIn("find-generic-password", src)

    def test_private_sources_excluded(self):
        src = _SCRIPT.read_text()
        self.assertIn("PRIVATE_SOURCES", src)
        self.assertIn("work", src.lower())


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_state_fast(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
            tmp.write_text(json.dumps({"recent_sources": [], "pilot_count": 0}))
        try:
            start = time.perf_counter()
            for _ in range(100):
                with patch.object(_mod, "STATE_FILE", tmp):
                    load_state()
            elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 0.5)
        finally:
            tmp.unlink(missing_ok=True)

    def test_api_calls_have_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_generate_pilot_falls_back_to_ollama(self):
        ollama_calls = []
        def fake_ollama(*a, **kw):
            ollama_calls.append(1)
            return "COLD OPEN\n" + "Script content. " * 300
        with patch.object(_mod, "_generate_via_openrouter", side_effect=Exception("OR fail")):
            with patch.object(_mod, "_generate_via_ollama", side_effect=fake_ollama):
                memories = [{"text": f"memory {i}", "metadata": "{}"}]
                result = _mod.generate_pilot("test_source", memories)
        self.assertGreater(len(ollama_calls), 0)

    def test_main_aborts_on_no_source(self):
        with patch.object(_mod, "pick_subject", return_value=None):
            with patch.object(_mod, "load_state",
                              return_value={"recent_sources": [], "pilot_count": 0}):
                _mod.main()  # should not raise

    def test_main_aborts_on_too_few_memories(self):
        with patch.object(_mod, "pick_subject", return_value="test_source"):
            with patch.object(_mod, "fetch_memories", return_value=[{"text": "x"}]):
                with patch.object(_mod, "load_state",
                                  return_value={"recent_sources": [], "pilot_count": 0}):
                    _mod.main()  # abort: too few memories


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_load_state_defaults(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(_mod, "STATE_FILE", Path(tmp) / "missing.json"):
                state = load_state()
        self.assertIn("recent_sources", state)
        self.assertIn("pilot_count", state)

    def test_save_load_roundtrip(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            state = {"recent_sources": ["wiki"], "pilot_count": 7}
            with patch.object(_mod, "STATE_FILE", tmp):
                save_state(state)
                loaded = load_state()
            self.assertEqual(loaded["pilot_count"], 7)
        finally:
            tmp.unlink(missing_ok=True)

    def test_log_does_not_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(_mod, "LOG_FILE", Path(tmp) / "test.log"):
                log("test message")

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.OPENROUTER_URL)
        self.assertIsNotNone(_mod.OLLAMA_URL)
        self.assertIsNotNone(_mod.MODEL)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_updates_state_on_success(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
            tmp.write_text(json.dumps({"recent_sources": [], "pilot_count": 2}))
        pilot_text = "INT. SPACE STATION — DAY\n\n" + "DIALOGUE: " * 500
        memories = [{"text": f"Memory {i}", "metadata": "{}"} for i in range(15)]
        try:
            with patch.object(_mod, "STATE_FILE", tmp):
                with patch.object(_mod, "pick_subject", return_value="scifi"):
                    with patch.object(_mod, "fetch_memories", return_value=memories):
                        with patch.object(_mod, "generate_pilot", return_value=pilot_text):
                            with patch.object(_mod, "generate_pilot_image", return_value=None):
                                with patch.object(_mod, "publish_to_journal"):
                                    with patch.object(_mod, "post_to_slack"):
                                        with patch.object(_mod, "ingest_pilot_to_memory"):
                                            _mod.main()
            state = json.loads(tmp.read_text())
            self.assertEqual(state["pilot_count"], 3)
        finally:
            tmp.unlink(missing_ok=True)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_post_to_slack_includes_source(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        _mod.post_to_slack("Pilot script text " * 50, "The Science Lab", "science_fiction")
        combined = " ".join(posts)
        self.assertIn("Science", combined)
        _nova_cfg.post_both.side_effect = None

    def test_pilot_too_short_aborts(self):
        memories = [{"text": f"mem {i}", "metadata": "{}"} for i in range(15)]
        with patch.object(_mod, "pick_subject", return_value="test"):
            with patch.object(_mod, "fetch_memories", return_value=memories):
                with patch.object(_mod, "generate_pilot", return_value="short"):
                    with patch.object(_mod, "load_state",
                                      return_value={"recent_sources": [], "pilot_count": 0}):
                        with patch.object(_mod, "save_state"):
                            with patch.object(_mod, "publish_to_journal") as pub_mock:
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
                   "pick_subject", "fetch_memories", "generate_pilot",
                   "post_to_slack"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
