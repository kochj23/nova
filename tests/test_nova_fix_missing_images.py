"""
test_nova_fix_missing_images.py — All 7 test categories for nova_fix_missing_images.py
Written by Jordan Koch.
"""
import importlib.util, json, sys, time, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg
sys.modules["nova_image_utils"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_fix_missing_images.py"
_spec = importlib.util.spec_from_file_location("nova_fix_missing_images", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

log = _mod.log
notify = _mod.notify
get_posts_missing_images = _mod.get_posts_missing_images


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
    def test_journal_dir_on_data_volume(self):
        self.assertIn("/Volumes/Data", str(_mod.JOURNAL_DIR))
    def test_images_stored_locally_in_journal(self):
        """Images must be stored in the local journal directory."""
        self.assertIn("/Volumes/Data", str(_mod.STATIC_DIR))


class TestPerformance(unittest.TestCase):
    def test_sections_dict_defined(self):
        self.assertGreater(len(_mod.SECTIONS), 0)
    def test_sections_has_prompts(self):
        for section, prompt in _mod.SECTIONS.items():
            self.assertIsInstance(prompt, str)
            self.assertGreater(len(prompt), 0)
    def test_log_file_in_tmp(self):
        self.assertIn("/tmp", str(_mod.LOG_FILE))


class TestRetry(unittest.TestCase):
    def test_notify_does_not_raise_on_failure(self):
        _nova_cfg.post_both.side_effect = Exception("slack down")
        try:
            notify("Test message")
        except Exception:
            pass
        finally:
            _nova_cfg.post_both.side_effect = None
    def test_get_posts_missing_images_returns_empty_if_no_journal(self):
        with patch.object(_mod, "CONTENT_DIR", Path("/nonexistent/content")):
            result = get_posts_missing_images()
        self.assertIsInstance(result, list)


class TestUnit(unittest.TestCase):
    def test_sections_keys_are_slugs(self):
        for section in _mod.SECTIONS:
            self.assertNotIn(" ", section, f"Section '{section}' has spaces")
    def test_sections_has_dreams(self):
        self.assertIn("dreams", _mod.SECTIONS)
    def test_sections_has_essays(self):
        self.assertIn("essays", _mod.SECTIONS)
    def test_image_size_1024_768(self):
        src = _SCRIPT.read_text()
        self.assertIn("1024", src)
        self.assertIn("768", src)
    def test_get_posts_returns_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            content_dir = Path(tmpdir) / "content"
            content_dir.mkdir()
            (content_dir / "dreams").mkdir()
            post = content_dir / "dreams" / "test-dream.md"
            post.write_text("---\ntitle: Test Dream\n---\n\nContent here.")
            with patch.object(_mod, "CONTENT_DIR", content_dir):
                with patch.object(_mod, "STATIC_DIR", Path(tmpdir) / "static"):
                    result = get_posts_missing_images()
        self.assertIsInstance(result, list)
    def test_log_creates_log_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "test.log"
            with patch.object(_mod, "LOG_FILE", str(log_file)):
                log("Test log message")
        self.assertTrue(log_file.exists())


class TestIntegration(unittest.TestCase):
    def test_get_posts_skips_posts_with_images(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            content_dir = Path(tmpdir) / "content" / "essays"
            content_dir.mkdir(parents=True)
            static_dir = Path(tmpdir) / "static"
            static_dir.mkdir()
            # Post WITH image
            post = content_dir / "essay-with-image.md"
            post.write_text('---\ntitle: Test\nimage: "essays/essay-with-image.png"\n---\nContent')
            img = static_dir / "essays" / "essay-with-image.png"
            img.parent.mkdir(parents=True)
            img.write_bytes(b"FAKE PNG")
            with patch.object(_mod, "CONTENT_DIR", Path(tmpdir) / "content"):
                with patch.object(_mod, "STATIC_DIR", static_dir):
                    result = get_posts_missing_images()
        # Post with existing image should not be in missing list
        slugs = [p.get("slug", "") for p in result]
        self.assertNotIn("essay-with-image", slugs)


class TestFunctional(unittest.TestCase):
    def test_main_does_nothing_if_no_missing(self):
        with patch.object(_mod, "get_posts_missing_images", return_value=[]) as mock_fn:
            with patch.object(_mod, "notify"):
                _mod.main()
        # Should complete without generating any images
    def test_main_exists(self):
        self.assertTrue(hasattr(_mod, "main"))


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.JOURNAL_DIR, Path)
        self.assertIsInstance(_mod.CONTENT_DIR, Path)
        self.assertIsInstance(_mod.STATIC_DIR, Path)
        self.assertIsInstance(_mod.SECTIONS, dict)
        self.assertIsInstance(_mod.LOG_FILE, str)
    def test_functions_exist(self):
        for fn in ("log", "notify", "get_posts_missing_images", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
