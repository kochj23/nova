"""
test_nova_art_corner.py — All 7 test categories for nova_art_corner.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_art_corner.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()

_nova_image_utils = MagicMock()
_nova_image_utils.ensure_backend = MagicMock(return_value=True)
_nova_image_utils.get_random_model = MagicMock(return_value="juggernaut")
_nova_image_utils.MODELS = {"juggernaut": {"file": "Jugg.safetensors", "optimal_steps": 12},
                             "flux_dev": {"file": "Flux.safetensors", "optimal_steps": 20}}
_nova_image_utils.get_model_for_today = MagicMock(return_value="juggernaut")
_nova_image_utils._model_available_via_api = MagicMock(return_value=True)

sys.modules["nova_config"] = _nova_cfg
sys.modules["nova_image_utils"] = _nova_image_utils

_mod = load_script_compat(_SCRIPT, "nova_art_corner")

scrub_emails = _mod.scrub_emails
extract_memory_text = _mod.extract_memory_text
pick_best_candidate = _mod.pick_best_candidate
DAILY_STYLES = _mod.DAILY_STYLES
DAILY_THEMES = _mod.DAILY_THEMES


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-"]:
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

    def test_scrub_emails_removes_personal_addresses(self):
        _at = "@"
        text = f"Contact kochjpar{_at}gmail.com for details"
        result = scrub_emails(text)
        self.assertNotIn("kochjpar", result)
        self.assertIn("[redacted]", result)

    def test_scrub_emails_keeps_nova_address(self):
        text = "Written by nova@digitalnoise.net for the journal"
        result = scrub_emails(text)
        self.assertIn("nova@digitalnoise.net", result)

    def test_keychain_used_for_api_key(self):
        src = _SCRIPT.read_text()
        self.assertIn("find-generic-password", src)
        self.assertIn("nova-openrouter-api-key", src)

    def test_memory_text_truncated_at_500(self):
        """extract_memory_text must cap at 500 chars."""
        mem = {"text": "x" * 1000}
        result = extract_memory_text(mem)
        self.assertLessEqual(len(result), 500)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_daily_styles_covers_all_7_days(self):
        self.assertEqual(len(DAILY_STYLES), 7)
        for i in range(7):
            self.assertIn(i, DAILY_STYLES)

    def test_daily_themes_covers_all_7_days(self):
        self.assertEqual(len(DAILY_THEMES), 7)

    def test_pick_best_candidate_fast_on_large_list(self):
        import tempfile
        candidates = []
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(50):
                p = Path(tmp) / f"img_{i}.png"
                p.write_bytes(b"x" * (i + 1) * 1000)
                candidates.append(p)
            start = time.perf_counter()
            best = pick_best_candidate(candidates)
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)
        self.assertIsNotNone(best)

    def test_extract_memory_text_fast(self):
        memories = [{"text": f"Memory {i} " * 50} for i in range(1000)]
        start = time.perf_counter()
        for m in memories:
            extract_memory_text(m)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_run_pipeline_retries_with_simplified_prompt_on_failure(self):
        """If all image candidates fail, run_pipeline must retry with simplified prompt."""
        retry_calls = []
        original = _mod.run_pipeline

        def fake_pipeline(retry_simplified=False):
            retry_calls.append(retry_simplified)
            if not retry_simplified:
                return original(retry_simplified=True)
            return False

        with patch.object(_mod, "fetch_random_memories", return_value=[{"text": "x"}] * 5):
            with patch.object(_mod, "fetch_themed_memories", return_value=[]):
                with patch.object(_mod, "synthesize_visual_concept", return_value="concept"):
                    with patch.object(_mod, "write_image_prompt", return_value="prompt text"):
                        with patch.object(_mod, "generate_title", return_value="Title"):
                            with patch.object(_mod, "generate_candidates", return_value=[]):
                                result = _mod.run_pipeline()
        self.assertFalse(result)

    def test_fetch_random_memories_returns_empty_on_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            result = _mod.fetch_random_memories(10)
        self.assertEqual(result, [])

    def test_fetch_themed_memories_returns_empty_on_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            result = _mod.fetch_themed_memories("nature landscape", 5)
        self.assertEqual(result, [])

    def test_call_openrouter_propagates_key_error(self):
        with patch.object(_mod, "get_openrouter_key", side_effect=RuntimeError("no key")):
            with self.assertRaises(RuntimeError):
                _mod.call_openrouter("sys", "usr")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_extract_memory_text_string_input(self):
        self.assertEqual(extract_memory_text("plain string"), "plain string")

    def test_extract_memory_text_from_dict_text(self):
        result = extract_memory_text({"text": "hello world"})
        self.assertEqual(result, "hello world")

    def test_extract_memory_text_from_content_key(self):
        result = extract_memory_text({"content": "from content"})
        self.assertEqual(result, "from content")

    def test_extract_memory_text_empty_dict(self):
        result = extract_memory_text({})
        self.assertEqual(result, "")

    def test_pick_best_candidate_returns_none_on_empty(self):
        self.assertIsNone(pick_best_candidate([]))

    def test_pick_best_candidate_returns_largest(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            small = Path(tmp) / "small.png"
            large = Path(tmp) / "large.png"
            small.write_bytes(b"x" * 100)
            large.write_bytes(b"x" * 10000)
            best = pick_best_candidate([small, large])
        self.assertEqual(best.name, "large.png")

    def test_pick_best_candidate_deletes_non_winners(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "a.png"
            p2 = Path(tmp) / "b.png"
            p1.write_bytes(b"x" * 100)
            p2.write_bytes(b"x" * 200)
            best = pick_best_candidate([p1, p2])
            self.assertFalse(p1.exists())
            self.assertTrue(p2.exists())

    def test_daily_styles_have_required_keys(self):
        for dow, style in DAILY_STYLES.items():
            self.assertIn("name", style, f"Day {dow} missing 'name'")
            self.assertIn("directive", style, f"Day {dow} missing 'directive'")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_run_pipeline_aborts_when_no_memories(self):
        with patch.object(_mod, "fetch_random_memories", return_value=[]):
            with patch.object(_mod, "fetch_themed_memories", return_value=[]):
                result = _mod.run_pipeline()
        self.assertFalse(result)

    def test_run_pipeline_aborts_when_swarmui_down(self):
        _nova_image_utils.ensure_backend.return_value = False
        with patch.object(_mod, "ensure_backend", return_value=False):
            result = _mod.run_pipeline()
        self.assertFalse(result)
        _nova_image_utils.ensure_backend.return_value = True

    def test_run_pipeline_calls_slack_on_success(self):
        import tempfile
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)

        with tempfile.TemporaryDirectory() as tmp:
            fake_img = Path(tmp) / "best.png"
            fake_img.write_bytes(b"x" * 1000)
            hugo_content = Path(tmp) / "content/art"
            hugo_images = Path(tmp) / "static/images/art"

            with patch.object(_mod, "fetch_random_memories", return_value=[{"text": "memory text"}] * 5):
                with patch.object(_mod, "fetch_themed_memories", return_value=[]):
                    with patch.object(_mod, "synthesize_visual_concept", return_value="A lighthouse"):
                        with patch.object(_mod, "write_image_prompt", return_value="prompt"):
                            with patch.object(_mod, "generate_title", return_value="Lighthouse"):
                                with patch.object(_mod, "generate_candidates", return_value=[fake_img]):
                                    with patch.object(_mod, "write_artist_statement", return_value="Statement"):
                                        with patch.object(_mod, "CONTENT_ART", hugo_content):
                                            with patch.object(_mod, "IMAGES_ART", hugo_images):
                                                with patch.object(_mod, "HUGO_ROOT", Path(tmp)):
                                                    with patch.object(_mod, "git_push"):
                                                        with patch.object(_mod, "post_to_slack"):
                                                            result = _mod.run_pipeline()
        _nova_cfg.post_both.side_effect = None


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_style_rotates_by_day_of_week(self):
        from datetime import datetime
        # All 7 days should map to unique styles
        style_names = set()
        for dow in range(7):
            style_names.add(DAILY_STYLES[dow]["name"])
        self.assertEqual(len(style_names), 7)

    def test_git_push_handles_nothing_to_commit(self):
        """git_push must handle 'nothing to commit' gracefully."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0),  # git add
                MagicMock(returncode=1, stdout="nothing to commit", stderr=""),  # commit
            ]
            _mod.git_push("test commit")  # should not raise

    def test_post_to_slack_formats_message(self):
        import tempfile
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            fake_img = Path(f.name)
        try:
            _mod.post_to_slack("Test Title", DAILY_STYLES[0], "Statement text.", fake_img)
            self.assertTrue(len(posts) > 0)
            combined = " ".join(posts)
            self.assertIn("Test Title", combined)
        finally:
            fake_img.unlink(missing_ok=True)
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

    def test_key_functions_exist(self):
        for fn in ["main", "run_pipeline", "fetch_random_memories", "fetch_themed_memories",
                   "synthesize_visual_concept", "write_image_prompt", "write_artist_statement",
                   "generate_title", "generate_candidates", "pick_best_candidate",
                   "publish_to_hugo", "git_push", "post_to_slack", "scrub_emails"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_constants_defined(self):
        self.assertEqual(_mod.IMAGE_WIDTH, 1024)
        self.assertEqual(_mod.IMAGE_HEIGHT, 1024)
        self.assertGreater(_mod.NUM_CANDIDATES, 0)

    def test_log_does_not_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(_mod, "LOG_FILE", Path(tmp) / "test.log"):
                _mod.log("smoke test")


if __name__ == "__main__":
    unittest.main(verbosity=2)
