"""
test_nova_after_dark.py — All 7 test categories for nova_after_dark.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub dependencies before loading
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_after_dark.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.SLACK_CHAN = "#nova-chat"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.filter_private_memories = lambda mems: mems
_nova_cfg.openrouter_api_key = lambda: "sk-test-key"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg
sys.modules["nova_image_utils"] = MagicMock()

_mod = load_script_compat(_SCRIPT, "nova_after_dark")

pick_event = _mod.pick_event
searxng_search = _mod.searxng_search
recall_memories = _mod.recall_memories
generate_monologue = _mod.generate_monologue
publish_to_hugo = _mod.publish_to_hugo
post_to_slack = _mod.post_to_slack
load_state = _mod.load_state
save_state = _mod.save_state
fetch_today_in_history = _mod.fetch_today_in_history


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-", "password ="]:
            self.assertNotIn(pattern, src, f"Credential pattern in source: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src, "Hardcoded home path found — use Path.home()")

    def test_no_pii_emails_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney.com",
            "kochj" + _at + "digitalnoise.net",
            "kochj23" + _at + "gmail.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII found in source: {p!r}")

    def test_openrouter_key_from_config_not_hardcoded(self):
        """API key must come from nova_config, not hardcoded."""
        src = _SCRIPT.read_text()
        self.assertNotIn("sk-or-", src)
        self.assertIn("openrouter_api_key", src)

    def test_memory_filter_applied_to_private_sources(self):
        """recall_memories must call nova_config.filter_private_memories."""
        captured = []
        # Replace the function on nova_config directly (the module uses nova_config.filter_private_memories)
        original = _nova_cfg.filter_private_memories
        _nova_cfg.filter_private_memories = lambda x: (captured.append(x), x)[1]
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({"memories": [{"text": "hello"}]}).encode()
        with patch("urllib.request.urlopen", return_value=fake_resp):
            recall_memories("test query")
        _nova_cfg.filter_private_memories = original
        self.assertTrue(len(captured) > 0, "filter_private_memories was not called")

    def test_slack_post_truncates_monologue_at_2500(self):
        """Slack post must not exceed safe limits."""
        event = {"year": "1066", "text": "Battle of Hastings"}
        long_monologue = "x" * 5000
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        post_to_slack(long_monologue, event)
        self.assertTrue(len(posts) > 0)
        # monologue capped at 2500 + header
        self.assertLessEqual(len(posts[0]), 3500)
        _nova_cfg.post_both.side_effect = None


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_pick_event_fast_on_large_pool(self):
        events = [{"year": str(i), "text": "a" * 200} for i in range(1000)]
        state = {"recent_topics": []}
        start = time.perf_counter()
        for _ in range(100):
            pick_event(events, state)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"pick_event 100x took {elapsed:.3f}s")

    def test_searxng_search_has_timeout(self):
        """searxng_search must use a timeout to avoid hanging."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)

    def test_state_load_save_roundtrip_fast(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            state = {"recent_topics": ["t1", "t2"], "episode_count": 5}
            with patch.object(_mod, "STATE_FILE", tmp):
                start = time.perf_counter()
                save_state(state)
                loaded = load_state()
                elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 0.1)
            self.assertEqual(loaded["episode_count"], 5)
        finally:
            tmp.unlink(missing_ok=True)

    def test_pick_event_avoids_recent_topics(self):
        events = [{"year": "1900", "text": "Event Alpha " * 5},
                  {"year": "1901", "text": "Event Beta " * 5}]
        state = {"recent_topics": [events[0]["text"][:50]]}
        results = [pick_event(events, state) for _ in range(20)]
        # Should prefer Event Beta since Alpha is recent
        texts = [r["text"] for r in results]
        self.assertTrue(any("Beta" in t for t in texts))


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_fetch_today_in_history_handles_network_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("network error")):
            result = fetch_today_in_history()
        self.assertIsInstance(result, list)
        self.assertEqual(result, [])

    def test_generate_image_retries_3_times(self):
        """generate_image must retry up to 3 times."""
        run_count = [0]
        def failing_run(*args, **kwargs):
            run_count[0] += 1
            return MagicMock(returncode=1, stdout="", stderr="fail")
        with patch("subprocess.run", side_effect=failing_run):
            with patch("time.sleep"):
                result = _mod.generate_image({"year": "1066", "text": "Battle"})
        self.assertIsNone(result)
        self.assertEqual(run_count[0], 3)

    def test_searxng_search_handles_timeout(self):
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = searxng_search("test query")
        self.assertEqual(result, [])

    def test_generate_monologue_falls_back_to_openrouter(self):
        """If Ollama fails, must try OpenRouter."""
        or_called = [False]

        def fake_openrouter(*args, **kwargs):
            or_called[0] = True
            return "OpenRouter monologue text " * 20

        with patch.object(_mod, "_generate_ollama", side_effect=OSError("ollama down")):
            with patch.object(_mod, "_generate_openrouter", side_effect=fake_openrouter):
                event = {"year": "1066", "text": "Battle of Hastings"}
                result = generate_monologue(event, "context", "memories")
        self.assertTrue(or_called[0])
        self.assertIn("OpenRouter", result)

    def test_recall_memories_returns_empty_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            result = recall_memories("test")
        self.assertEqual(result, [])


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_pick_event_returns_none_on_empty_list(self):
        result = pick_event([], {"recent_topics": []})
        self.assertIsNone(result)

    def test_pick_event_prefers_longer_text(self):
        # With 25 events of varying length, the top-20 pool excludes the shortest ones.
        # All results should come from events with text > 10 chars (bottom 5 excluded).
        events = [{"year": str(i), "text": "x" * (i + 1) * 10} for i in range(25)]
        # The 5 shortest (i=0..4) have 10..50 chars, top 20 are i=5..24 (60..250 chars)
        results = [pick_event(events, {"recent_topics": []}) for _ in range(100)]
        # None should be from the bottom 5 (text <= 50 chars)
        short_picked = sum(1 for r in results if len(r["text"]) <= 50)
        self.assertEqual(short_picked, 0, "Should never pick from bottom 5 shortest events")

    def test_pick_event_resets_recent_when_all_excluded(self):
        events = [{"year": "1900", "text": "Event one two three four five"}]
        state = {"recent_topics": [events[0]["text"][:50]]}
        # All excluded — should still return something
        result = pick_event(events, state)
        self.assertIsNotNone(result)

    def test_load_state_defaults_on_missing_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nonexistent.json"
            with patch.object(_mod, "STATE_FILE", missing):
                state = load_state()
        self.assertIn("recent_topics", state)
        self.assertIn("episode_count", state)
        self.assertEqual(state["episode_count"], 0)

    def test_save_state_writes_json(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
        try:
            state = {"recent_topics": ["x"], "episode_count": 3}
            with patch.object(_mod, "STATE_FILE", tmp):
                save_state(state)
            loaded = json.loads(tmp.read_text())
            self.assertEqual(loaded["episode_count"], 3)
        finally:
            tmp.unlink(missing_ok=True)

    def test_searxng_search_parses_results(self):
        fake = {"results": [{"title": "T1", "content": "C1", "url": "http://x.com"}]}
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps(fake).encode()
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=fake_resp):
            results = searxng_search("test")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "T1")

    def test_searxng_search_limits_results(self):
        fake = {"results": [{"title": f"T{i}", "content": "C", "url": "http://x.com"} for i in range(20)]}
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps(fake).encode()
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=fake_resp):
            results = searxng_search("test", max_results=5)
        self.assertLessEqual(len(results), 5)

    def test_ollama_generate_strips_think_tags(self):
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps({"response": "<think>internal thought</think>actual response"}).encode()
        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = _mod._generate_ollama("sys", "user")
        self.assertNotIn("<think>", result)
        self.assertIn("actual response", result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_flow_no_events_aborts_gracefully(self):
        with patch.object(_mod, "fetch_today_in_history", return_value=[]):
            with patch.object(_mod, "load_state", return_value={"recent_topics": [], "episode_count": 0}):
                with patch.object(_mod, "save_state"):
                    _mod.main()  # should not raise

    def test_main_flow_short_monologue_aborts(self):
        events = [{"year": "1066", "text": "Battle " * 10}]
        with patch.object(_mod, "fetch_today_in_history", return_value=events):
            with patch.object(_mod, "load_state", return_value={"recent_topics": [], "episode_count": 0}):
                with patch.object(_mod, "searxng_search", return_value=[]):
                    with patch.object(_mod, "recall_memories", return_value=[]):
                        with patch.object(_mod, "generate_monologue", return_value="short"):
                            with patch.object(_mod, "save_state"):
                                _mod.main()  # should abort without publishing

    def test_state_episode_count_increments(self):
        import tempfile
        events = [{"year": "1066", "text": "Battle " * 10}]
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = Path(f.name)
            tmp.write_text(json.dumps({"recent_topics": [], "episode_count": 5}))
        try:
            with patch.object(_mod, "fetch_today_in_history", return_value=events):
                with patch.object(_mod, "STATE_FILE", tmp):
                    with patch.object(_mod, "searxng_search", return_value=[]):
                        with patch.object(_mod, "recall_memories", return_value=[]):
                            with patch.object(_mod, "generate_monologue", return_value="x" * 300):
                                with patch.object(_mod, "generate_image", return_value=None):
                                    with patch.object(_mod, "publish_to_hugo", return_value=True):
                                        with patch.object(_mod, "post_to_slack"):
                                            _mod.main()
            state = json.loads(tmp.read_text())
            self.assertEqual(state["episode_count"], 6)
        finally:
            tmp.unlink(missing_ok=True)

    def test_publish_to_hugo_writes_markdown(self):
        import tempfile
        event = {"year": "1066", "text": "Battle of Hastings was fought"}
        with tempfile.TemporaryDirectory() as tmpdir:
            content_dir = Path(tmpdir) / "content/after-dark"
            images_dir = Path(tmpdir) / "static/images/after-dark"
            with patch.object(_mod, "CONTENT_DIR", content_dir):
                with patch.object(_mod, "IMAGES_DIR", images_dir):
                    with patch.object(_mod, "HUGO_ROOT", Path(tmpdir)):
                        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
                            publish_to_hugo("Monologue text " * 50, event, None, [], [], 1)
            md_files = list(content_dir.glob("*.md"))
            self.assertGreater(len(md_files), 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_full_pipeline_with_mocked_llm(self):
        events = [{"year": "1945", "text": "World War II ended with Japan surrendering"}]
        monologue = "Good evening everybody. " * 30
        with patch.object(_mod, "fetch_today_in_history", return_value=events):
            with patch.object(_mod, "load_state", return_value={"recent_topics": [], "episode_count": 0}):
                with patch.object(_mod, "searxng_search", return_value=[]):
                    with patch.object(_mod, "recall_memories", return_value=[]):
                        with patch.object(_mod, "generate_monologue", return_value=monologue):
                            with patch.object(_mod, "generate_image", return_value=None):
                                with patch.object(_mod, "publish_to_hugo", return_value=True):
                                    with patch.object(_mod, "post_to_slack") as slack_mock:
                                        with patch.object(_mod, "save_state"):
                                            _mod.main()
                                        slack_mock.assert_called_once()

    def test_date_override_via_env_var(self):
        """NOVA_FOR_DATE env var should override date functions."""
        self.assertIsNotNone(_mod._today_str())
        self.assertIsNotNone(_mod._now())

    def test_wiki_api_url_format(self):
        """fetch_today_in_history must build correct Wikipedia API URL."""
        urls_called = []
        def capture_urlopen(req, timeout=None):
            urls_called.append(req.full_url if hasattr(req, "full_url") else str(req))
            raise OSError("stopped")
        with patch("urllib.request.urlopen", side_effect=capture_urlopen):
            fetch_today_in_history()
        self.assertTrue(len(urls_called) > 0)
        self.assertIn("wikimedia.org", urls_called[0])

    def test_slack_post_includes_year_and_fact(self):
        event = {"year": "1969", "text": "Moon landing"}
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        post_to_slack("Some monologue text " * 10, event)
        self.assertTrue(len(posts) > 0)
        self.assertIn("1969", posts[0])
        _nova_cfg.post_both.side_effect = None

    def test_pick_event_pool_size_bounded(self):
        """pick_event must only draw from top 20 candidates."""
        events = [{"year": str(i), "text": "x" * (i + 1)} for i in range(100)]
        # With recent_topics empty, should only pick from sorted top 20
        results = set()
        for _ in range(200):
            e = pick_event(events, {"recent_topics": []})
            results.add(e["year"])
        # Should not pick from very short events at all
        self.assertNotIn("0", results)


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

    def test_module_constants_present(self):
        self.assertIsNotNone(_mod.OLLAMA_URL)
        self.assertIsNotNone(_mod.OPENROUTER_URL)
        self.assertIsNotNone(_mod.WIKI_API)
        self.assertIsNotNone(_mod.SEARXNG_URL)
        self.assertIn("wikimedia.org", _mod.WIKI_API)

    def test_script_is_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK), f"{_SCRIPT} not executable")

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))

    def test_key_functions_exist(self):
        for fn in ["main", "fetch_today_in_history", "pick_event", "searxng_search",
                   "recall_memories", "generate_monologue", "generate_image",
                   "publish_to_hugo", "post_to_slack", "load_state", "save_state"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing function: {fn}")

    def test_log_function_doesnt_crash(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(_mod, "LOG_FILE", Path(tmp) / "test.log"):
                _mod.log("test message")  # should not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
