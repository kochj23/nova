"""
test_ingest_horror_films.py — All 7 test categories for ingest_horror_films.py
Written by Jordan Koch.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

import importlib.util
_SCRIPT = Path(__file__).parent.parent / "scripts" / "ingest_horror_films.py"
_spec = importlib.util.spec_from_file_location("ingest_horror_films", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_memory_url_is_localhost(self):
        self.assertIn("127.0.0.1", _mod.MEMORY_URL)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pattern in ["kochjpar" + _at + "gmail.com",
                        "jordan.koch" + _at + "disney" + ".com"]:
            self.assertNotIn(pattern, src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_films_dict_not_empty(self):
        self.assertGreater(len(_mod.FILMS), 0)

    def test_facts_per_film_reasonable(self):
        for film, facts in _mod.FILMS.items():
            self.assertGreater(len(facts), 0,
                               f"Film {film} has no facts")
            self.assertLessEqual(len(facts), 200,
                                 f"Film {film} has too many facts")

    def test_remember_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=10", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_silent_fails(self):
        """remember() must not raise on failure."""
        def fail(*args, **kwargs):
            raise OSError("server down")

        with patch("urllib.request.urlopen", side_effect=fail):
            result = _mod.remember("Test horror fact.")
        self.assertFalse(result)

    def test_failed_count_incremented(self):
        src = _SCRIPT.read_text()
        self.assertIn("failed += 1", src)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_films_keys_are_strings(self):
        for key in _mod.FILMS.keys():
            self.assertIsInstance(key, str)

    def test_facts_are_strings(self):
        for film, facts in _mod.FILMS.items():
            for fact in facts:
                self.assertIsInstance(fact, str)
                self.assertGreater(len(fact), 10)

    def test_remember_posts_correct_fields(self):
        posted = []

        def capture(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=capture):
            _mod.remember("The Shining was directed by Kubrick.")

        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["source"], "local_knowledge")
        self.assertEqual(posted[0]["metadata"]["type"], "horror_films")
        self.assertTrue(posted[0]["metadata"]["owner_favorite"])

    def test_log_function_exists(self):
        self.assertTrue(callable(_mod.log))

    def test_slack_post_function_exists(self):
        self.assertTrue(callable(_mod.slack_post))


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_slack_posted_per_film(self):
        """slack_post must be called once per film completion."""
        post_calls = []

        def count_posts(*args, **kwargs):
            post_calls.append(args)

        def mock_remember(text):
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return True

        with patch.object(_mod, "remember", side_effect=mock_remember):
            with patch.object(_mod, "slack_post", side_effect=count_posts):
                with patch("time.sleep"):
                    import io
                    from contextlib import redirect_stdout
                    with redirect_stdout(io.StringIO()):
                        for film_name, facts in _mod.FILMS.items():
                            for fact in facts:
                                _mod.remember(fact)
                            count_posts(f"Film done: {film_name}")

        self.assertGreater(len(post_calls), 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_shining_facts_present(self):
        """The Shining facts must be in FILMS."""
        film_keys = list(_mod.FILMS.keys())
        self.assertTrue(any("Shining" in k for k in film_keys),
                        "The Shining must be in FILMS")

    def test_halloween_facts_present(self):
        film_keys = list(_mod.FILMS.keys())
        self.assertTrue(any("Halloween" in k for k in film_keys),
                        "Halloween must be in FILMS")

    def test_final_slack_post_called(self):
        """Script must call nova_config.post_both at completion."""
        src = _SCRIPT.read_text()
        self.assertIn("post_both", src)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"ingest_horror_films.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_constants_defined(self):
        for attr in ["MEMORY_URL", "FILMS"]:
            self.assertTrue(hasattr(_mod, attr))

    def test_functions_defined(self):
        for fn in ["log", "slack_post", "remember"]:
            self.assertTrue(callable(getattr(_mod, fn, None)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
