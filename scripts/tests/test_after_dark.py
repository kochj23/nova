#!/usr/bin/env python3
"""
test_after_dark.py — Comprehensive tests for nova_after_dark.py.

Covers: Wikipedia event fetching, event selection, SearXNG search, memory recall,
Ollama/OpenRouter generation, image generation, Hugo publishing, Slack posting,
state management, comedy rules enforcement, security checks.

Run: python3 -m pytest tests/test_after_dark.py -v
Written by Jordan Koch.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def after_dark_module(mock_nova_config, monkeypatch, tmp_path):
    """Import nova_after_dark fresh with mocked nova_config; log/state/Hugo paths go to tmp_path
    so the suite never writes the real ~/.openclaw/logs or nova-journal."""
    import importlib
    for mod in list(sys.modules.keys()):
        if "nova_after_dark" in mod:
            del sys.modules[mod]
    import nova_after_dark
    monkeypatch.setattr(nova_after_dark, "LOG_FILE", tmp_path / "after_dark.log")
    monkeypatch.setattr(nova_after_dark, "STATE_FILE", tmp_path / "after_dark_state.json")
    monkeypatch.setattr(nova_after_dark, "CONTENT_DIR", tmp_path / "content")
    monkeypatch.setattr(nova_after_dark, "IMAGES_DIR", tmp_path / "images")
    return nova_after_dark


@pytest.fixture
def sample_events():
    return [
        {"year": 1969, "text": "The Apollo 11 mission lands on the Moon"},
        {"year": 1776, "text": "The United States Declaration of Independence is adopted"},
        {"year": 2004, "text": "Facebook launches from a Harvard dorm room"},
        {"year": 1912, "text": "The Titanic sinks on its maiden voyage"},
        {"year": 1989, "text": "The Berlin Wall falls"},
    ]


@pytest.fixture
def sample_state():
    return {"recent_topics": ["The Apollo 11 mission lands on the Moon"[:50]], "episode_count": 42}


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoadSaveState:
    """Tests for state management."""

    def test_load_default_state(self, after_dark_module, tmp_path):
        with patch.object(after_dark_module, "STATE_FILE", tmp_path / "nonexistent.json"):
            state = after_dark_module.load_state()
        assert state == {"recent_topics": [], "episode_count": 0}

    def test_save_and_reload_state(self, after_dark_module, tmp_path):
        state_file = tmp_path / "state.json"
        with patch.object(after_dark_module, "STATE_FILE", state_file):
            after_dark_module.save_state({"recent_topics": ["test"], "episode_count": 10})
            loaded = after_dark_module.load_state()
        assert loaded["episode_count"] == 10


class TestFetchTodayInHistory:
    """Tests for Wikipedia event fetching."""

    @patch("urllib.request.urlopen")
    def test_parses_events_response(self, mock_urlopen, after_dark_module):
        response_data = {
            "events": [
                {"year": 1969, "text": "Moon landing"},
                {"year": 1776, "text": "Independence"},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        events = after_dark_module.fetch_today_in_history()
        assert len(events) == 2
        assert events[0]["year"] == 1969

    @patch("urllib.request.urlopen")
    def test_handles_api_failure(self, mock_urlopen, after_dark_module):
        mock_urlopen.side_effect = Exception("timeout")
        events = after_dark_module.fetch_today_in_history()
        assert events == []


class TestPickEvent:
    """Tests for event selection logic."""

    def test_avoids_recent_topics(self, after_dark_module, sample_events):
        state = {"recent_topics": [sample_events[0]["text"][:50]], "episode_count": 5}
        event = after_dark_module.pick_event(sample_events, state)
        assert event is not None
        assert event["text"][:50] != sample_events[0]["text"][:50]

    def test_returns_none_for_empty_events(self, after_dark_module):
        event = after_dark_module.pick_event([], {"recent_topics": []})
        assert event is None

    def test_prefers_longer_events(self, after_dark_module):
        events = [
            {"year": 2000, "text": "Short"},
            {"year": 2001, "text": "A much longer event description with many more details and interesting facts"},
        ]
        # Over many picks, longer events should be preferred (weighted by length)
        picks = set()
        for _ in range(50):
            event = after_dark_module.pick_event(events, {"recent_topics": []})
            picks.add(event["text"])
        # Should at least pick the longer one sometimes (it's in top pool)
        assert "A much longer event description with many more details and interesting facts" in picks


class TestSearxngSearch:
    """Tests for SearXNG integration."""

    @patch("urllib.request.urlopen")
    def test_returns_structured_results(self, mock_urlopen, after_dark_module):
        response_data = {
            "results": [
                {"title": "Apollo 11", "content": "Moon landing details", "url": "https://nasa.gov/apollo11"},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        results = after_dark_module.searxng_search("apollo 11")
        assert len(results) == 1
        assert results[0]["title"] == "Apollo 11"

    @patch("urllib.request.urlopen")
    def test_handles_failure_gracefully(self, mock_urlopen, after_dark_module):
        mock_urlopen.side_effect = Exception("connection refused")
        results = after_dark_module.searxng_search("test")
        assert results == []


class TestRecallMemories:
    """Tests for vector memory recall."""

    @patch("urllib.request.urlopen")
    def test_returns_text_list(self, mock_urlopen, after_dark_module):
        response_data = {
            "memories": [
                {"text": "Memory about space exploration and the moon."},
                {"text": "Another memory about astronomy."},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp
        # recall_memories routes every hit through nova_config.filter_private_memories
        # (DLP gate, commit 5e640b7). The fixture's nova_config is a MagicMock, so give
        # the gate a pass-through body or it returns an empty MagicMock iterable.
        after_dark_module.nova_config.filter_private_memories.side_effect = lambda ms: list(ms)
        memories = after_dark_module.recall_memories("moon landing")
        assert len(memories) == 2
        assert "space exploration" in memories[0]
        after_dark_module.nova_config.filter_private_memories.assert_called_once()

    @patch("urllib.request.urlopen")
    def test_private_memories_are_dropped(self, mock_urlopen, after_dark_module):
        """Whatever the DLP gate removes must never reach the monologue prompt."""
        response_data = {"memories": [
            {"text": "Public memory about the moon.", "source": "wiki"},
            {"text": "Private work memory.", "source": "work-email"},
        ]}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp
        after_dark_module.nova_config.filter_private_memories.side_effect = (
            lambda ms: [m for m in ms if m.get("source") != "work-email"])
        memories = after_dark_module.recall_memories("moon landing")
        assert memories == ["Public memory about the moon."]

    @patch("urllib.request.urlopen")
    def test_handles_failure(self, mock_urlopen, after_dark_module):
        mock_urlopen.side_effect = Exception("timeout")
        memories = after_dark_module.recall_memories("test")
        assert memories == []


class TestGenerateMonologue:
    """Tests for LLM monologue generation."""

    @patch("nova_after_dark._generate_openrouter")
    @patch("nova_after_dark._generate_ollama")
    def test_tries_ollama_first(self, mock_ollama, mock_openrouter, after_dark_module):
        mock_ollama.return_value = "A" * 500  # Long enough result
        event = {"year": 1969, "text": "Moon landing"}
        result = after_dark_module.generate_monologue(event, "context", "memories")
        assert len(result) >= 300
        mock_ollama.assert_called_once()
        mock_openrouter.assert_not_called()

    @patch("nova_after_dark._generate_openrouter")
    @patch("nova_after_dark._generate_ollama")
    def test_falls_back_to_openrouter(self, mock_ollama, mock_openrouter, after_dark_module):
        mock_ollama.side_effect = Exception("Ollama down")
        mock_openrouter.return_value = "B" * 500
        event = {"year": 1969, "text": "Moon landing"}
        result = after_dark_module.generate_monologue(event, "context", "memories")
        assert len(result) >= 300
        mock_openrouter.assert_called_once()

    @patch("nova_after_dark._generate_openrouter")
    @patch("nova_after_dark._generate_ollama")
    def test_returns_empty_on_all_failures(self, mock_ollama, mock_openrouter, after_dark_module):
        mock_ollama.side_effect = Exception("down")
        mock_openrouter.side_effect = Exception("also down")
        event = {"year": 1969, "text": "Moon landing"}
        result = after_dark_module.generate_monologue(event, "context", "memories")
        assert result == ""


class TestImageGeneration:
    """Tests for image generation.

    Since commit 4aead36 After Dark no longer runs its own subprocess retry loop:
    it delegates to nova_image_utils.generate_image (local ComfyUI with MAX_RETRIES
    attempts, then OpenRouter fallback). The shared retry/fallback behaviour is
    covered in tests/test_content_schedule.py; here we only check the delegation.
    """

    @patch("nova_image_utils.generate_image", return_value=None)
    def test_returns_none_when_generator_fails(self, mock_gen, after_dark_module):
        event = {"year": 1969, "text": "Moon landing"}
        result = after_dark_module.generate_image(event)
        assert result is None
        mock_gen.assert_called_once()
        assert mock_gen.call_args.kwargs.get("section") == "after-dark"

    @patch("nova_image_utils.generate_image")
    def test_returns_path_on_success(self, mock_gen, after_dark_module, tmp_path):
        img_path = tmp_path / "image.png"
        img_path.write_bytes(b"fake png")
        mock_gen.return_value = str(img_path)
        event = {"year": 1969, "text": "Moon landing"}
        result = after_dark_module.generate_image(event)
        assert result == str(img_path)
        prompt = mock_gen.call_args.args[0]
        assert "Moon landing" in prompt
        assert "1969" in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestSecurityLegacy:
    """Security tests for nova_after_dark.py (pytest-fixture style; the house TestSecurity is below)."""

    def test_no_hardcoded_credentials(self, after_dark_module):
        import inspect
        source = inspect.getsource(after_dark_module)
        assert "sk-" not in source
        assert "xoxb-" not in source
        assert "Bearer " not in source or "Bearer {" in source or 'f"Bearer ' in source

    def test_services_are_private_network(self, after_dark_module):
        """Ollama stays on loopback; SearXNG/memory-server resolve via the fleet
        service registry / internal DNS (commit e1759ed), so they may be a LAN
        address or a *.digitalnoise.net name -- never a public host."""
        import ipaddress
        from urllib.parse import urlparse

        def is_internal(url: str) -> bool:
            host = urlparse(url).hostname or ""
            if host in ("localhost",) or host.endswith(".digitalnoise.net"):
                return True
            try:
                return ipaddress.ip_address(host).is_private
            except ValueError:
                return False

        assert "127.0.0.1" in after_dark_module.OLLAMA_URL
        assert is_internal(after_dark_module.SEARXNG_URL), after_dark_module.SEARXNG_URL
        assert is_internal(after_dark_module.MEMORY_SERVER), after_dark_module.MEMORY_SERVER

    def test_no_pii_in_prompts(self, after_dark_module):
        """The comedy system prompt should not contain personal info."""
        import inspect
        source = inspect.getsource(after_dark_module.generate_monologue)
        assert "kochj" not in source.lower()
        # Verify no personal work email leaked into prompts
        assert "jordan.koch@" not in source


# ═══════════════════════════════════════════════════════════════════════════════
# FRAMEWORK TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    """Tests for graceful error handling."""

    @patch("nova_after_dark.post_to_slack")
    @patch("nova_after_dark.publish_to_hugo")
    @patch("nova_after_dark.generate_image")
    @patch("nova_after_dark.generate_monologue")
    @patch("nova_after_dark.recall_memories")
    @patch("nova_after_dark.searxng_search")
    @patch("nova_after_dark.pick_event")
    @patch("nova_after_dark.fetch_today_in_history")
    @patch("nova_after_dark.save_state")
    @patch("nova_after_dark.load_state")
    def test_aborts_on_short_monologue(
        self, mock_load, mock_save, mock_fetch, mock_pick,
        mock_search, mock_recall, mock_gen, mock_image,
        mock_publish, mock_slack, after_dark_module
    ):
        mock_load.return_value = {"recent_topics": [], "episode_count": 0}
        mock_fetch.return_value = [{"year": 1969, "text": "Moon landing"}]
        mock_pick.return_value = {"year": 1969, "text": "Moon landing"}
        mock_search.return_value = []
        mock_recall.return_value = []
        mock_gen.return_value = "Too short"  # Under 200 chars

        after_dark_module.main()

        mock_publish.assert_not_called()
        mock_slack.assert_not_called()
        mock_save.assert_not_called()

    @patch("nova_after_dark.fetch_today_in_history")
    @patch("nova_after_dark.load_state")
    def test_aborts_on_no_events(self, mock_load, mock_fetch, after_dark_module):
        mock_load.return_value = {"recent_topics": [], "episode_count": 0}
        mock_fetch.return_value = []
        # Should not raise
        after_dark_module.main()


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
@pytest.mark.skipif(not __import__("os").environ.get("NOVA_LIVE_TESTS"),
                    reason="live network test; set NOVA_LIVE_TESTS=1 to run (house rule: offline by default)")
class TestLiveWikipedia:
    """Integration tests requiring live services (opt-in only)."""

    def test_wikipedia_api_reachable(self, after_dark_module):
        """Verify Wikipedia On This Day API is reachable."""
        import urllib.request
        try:
            url = f"{after_dark_module.WIKI_API}/05/06"
            req = urllib.request.Request(url, headers={"User-Agent": "Nova/1.0 test", "Accept": "application/json"})
            resp = urllib.request.urlopen(req, timeout=10)
            assert resp.status == 200
        except Exception:
            pytest.skip("Wikipedia API not reachable")


# ═══════════════════════════════════════════════════════════════════════════════
# HOUSE CATEGORIES (unittest) — the 7 house categories (Security, Performance, Retry, Unit,
# Integration, Functional, Frame). Written by Jordan Koch (via Claude).
# Loaded under a private module name; nova_config / nova_journal are swapped for stubs on THAT
# module object only (post_both, git_push, OpenRouter key), and every file path is a tempdir.
# ═══════════════════════════════════════════════════════════════════════════════

import importlib.util as _ilu  # noqa: E402
import os as _os  # noqa: E402
import re as _re  # noqa: E402
import shutil as _shutil  # noqa: E402
import subprocess as _sp  # noqa: E402
import tempfile as _tempfile  # noqa: E402
import types as _types  # noqa: E402
import unittest  # noqa: E402

_SCRIPTS = Path(__file__).resolve().parents[1]
_SRC = (_SCRIPTS / "nova_after_dark.py").read_text()
_TMP = Path(_tempfile.mkdtemp(prefix="afterdark_"))


def _load_ad():
    spec = _ilu.spec_from_file_location("nova_after_dark_house_t", _SCRIPTS / "nova_after_dark.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import nova_config as _real_cfg
    mod.nova_config = _types.SimpleNamespace(
        post_both=MagicMock(), SLACK_CHAN="C_TEST", openrouter_api_key=MagicMock(return_value=None),
        filter_private_memories=_real_cfg.filter_private_memories)
    mod.nj = _types.SimpleNamespace(git_push=MagicMock())
    mod.LOG_FILE = _TMP / "ad.log"
    mod.STATE_FILE = _TMP / "state" / "ad.json"
    mod.CONTENT_DIR = _TMP / "content"
    mod.IMAGES_DIR = _TMP / "images"
    mod.print = lambda *a, **k: None
    return mod


AD = _load_ad()
_EVENT = {"year": 1969, "text": "The Apollo 11 mission lands on the Moon and everyone watches"}
_LONG = "Good evening, everybody. " * 30


def _resp(obj):
    m = MagicMock()
    m.read.return_value = json.dumps(obj).encode()
    m.__enter__.return_value = m
    return m


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_keys_and_key_from_config(self):
        self.assertIsNone(_re.search(r"(sk-or-|sk-ant-|xoxb-)[A-Za-z0-9]", _SRC))
        self.assertIn("nova_config.openrouter_api_key()", _SRC)

    def test_private_memories_filtered_before_public_output(self):
        mems = {"memories": [{"text": "public fact", "source": "wikipedia"},
                             {"text": "secret work thing", "source": "work_email"}]}
        with patch.object(AD.nova_config, "filter_private_memories", side_effect=lambda m: m[:1]) as f, \
             patch.object(AD.urllib.request, "urlopen", return_value=_resp(mems)):
            out = AD.recall_memories("q")
        f.assert_called_once()
        self.assertEqual(out, ["public fact"])

    def test_slug_is_path_safe(self):
        ev = {"year": 1, "text": "../../etc/passwd; $(boom) <script>"}
        AD.publish_to_hugo(_LONG, ev, None, [], [], 1)
        names = [p.name for p in AD.CONTENT_DIR.iterdir()]
        self.assertTrue(all(_re.fullmatch(r"[\w.-]+\.md", n) for n in names))
        self.assertFalse((_TMP / "etc").exists())


class TestPerformance(unittest.TestCase):
    def test_pick_event_from_10k_fast(self):
        events = [{"year": i, "text": f"event number {i} " * (i % 7 + 1)} for i in range(10_000)]
        state = {"recent_topics": [e["text"][:50] for e in events[:5000]]}
        t0 = time.perf_counter()
        for _ in range(10):
            AD.pick_event(events, state)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_ollama_failure_falls_back_to_openrouter(self):
        with patch.object(AD, "_generate_ollama", side_effect=OSError("down")) as o, \
             patch.object(AD, "_generate_openrouter", return_value="cloud monologue") as r:
            self.assertEqual(AD.generate_monologue(_EVENT, "", ""), "cloud monologue")
        self.assertEqual((o.call_count, r.call_count), (1, 1))

    def test_both_llms_down_returns_empty(self):
        with patch.object(AD, "_generate_ollama", return_value="short"), \
             patch.object(AD, "_generate_openrouter", side_effect=RuntimeError("No OpenRouter key")):
            self.assertEqual(AD.generate_monologue(_EVENT, "", ""), "")

    def test_image_retried_once_then_warns(self):
        with _MainRun() as m:
            m.gen_image.side_effect = [None, None]
            AD.main()
        self.assertEqual(m.gen_image.call_count, 2)
        self.assertIn("Image generation failed", AD.nova_config.post_both.call_args_list[0][0][0])

    def test_fetchers_fail_open(self):
        # RETRY GAP: fetch_today_in_history()/searxng_search() — one GET each, [] on failure
        with patch.object(AD.urllib.request, "urlopen", side_effect=OSError("x")) as u:
            self.assertEqual(AD.fetch_today_in_history(), [])
            self.assertEqual(AD.searxng_search("q"), [])
            self.assertEqual(AD.recall_memories("q"), [])
        self.assertEqual(u.call_count, 3)


class TestUnit(unittest.TestCase):
    def test_pick_event_avoids_recent_and_handles_empty(self):
        self.assertIsNone(AD.pick_event([], {}))
        evs = [{"year": 1, "text": "A" * 60}, {"year": 2, "text": "B" * 10}]
        for _ in range(20):
            self.assertEqual(AD.pick_event(evs, {"recent_topics": ["A" * 50]})["year"], 2)
        self.assertIn(AD.pick_event(evs[:1], {"recent_topics": ["A" * 50]})["year"], (1,))

    def test_ollama_strips_think(self):
        with patch.object(AD.urllib.request, "urlopen", return_value=_resp({"response": "<think>x</think> Joke"})):
            self.assertEqual(AD._generate_ollama("s", "u"), "Joke")

    def test_state_roundtrip(self):
        if AD.STATE_FILE.exists():
            AD.STATE_FILE.unlink()
        self.assertEqual(AD.load_state(), {"recent_topics": [], "episode_count": 0})
        AD.save_state({"recent_topics": ["x"], "episode_count": 3})
        self.assertEqual(AD.load_state()["episode_count"], 3)


class TestIntegration(unittest.TestCase):
    def test_publish_writes_hugo_post_and_uses_hardened_push(self):
        img = _TMP / "cover.png"; img.write_bytes(b"png")
        AD.nj.git_push.reset_mock()
        ok = AD.publish_to_hugo(_LONG, _EVENT, str(img), [{"title": "T", "url": "https://e.x", "content": "c"}],
                                ["a memory"], 7)
        self.assertTrue(ok)
        post = max(AD.CONTENT_DIR.glob("*apollo*.md"), key=lambda p: p.stat().st_mtime).read_text()
        self.assertIn('title: "\U0001f303 On This Day in 1969"', post)
        self.assertIn("Episode 7", post)
        self.assertIn("[T](https://e.x)", post)
        self.assertIn("cover:", post)
        AD.nj.git_push.assert_called_once()
        self.assertEqual(AD.nj.git_push.call_args[0][0], "after-dark")

    def test_slack_post_truncates_and_targets_chat(self):
        AD.nova_config.post_both.reset_mock()
        AD.post_to_slack("x" * 5000, _EVENT)
        msg = AD.nova_config.post_both.call_args[0][0]
        self.assertLess(len(msg), 2700)
        self.assertEqual(AD.nova_config.post_both.call_args.kwargs["slack_channel"], "C_TEST")


class _MainRun:
    def __enter__(self):
        self.ps = [patch.object(AD, "fetch_today_in_history", return_value=[_EVENT]),
                   patch.object(AD, "searxng_search", return_value=[{"title": "t", "content": "c", "url": "u"}]),
                   patch.object(AD, "recall_memories", return_value=["m1"]),
                   patch.object(AD, "generate_monologue", return_value=_LONG),
                   patch.object(AD, "generate_image", return_value="/nonexistent.png"),
                   patch.object(AD, "publish_to_hugo", return_value=True),
                   patch.object(AD, "post_to_slack")]
        mocks = [p.start() for p in self.ps]
        (self.fetch, self.search, self.recall, self.gen, self.gen_image, self.publish, self.slack) = mocks
        AD.nova_config.post_both.reset_mock()
        if AD.STATE_FILE.exists():
            AD.STATE_FILE.unlink()
        return self

    def __exit__(self, *a):
        for p in self.ps:
            p.stop()


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_posts_and_saves_state(self):
        with _MainRun() as m:
            AD.main()
        m.publish.assert_called_once()
        self.assertEqual(m.publish.call_args[0][5], 1)
        m.slack.assert_called_once()
        st = json.loads(AD.STATE_FILE.read_text())
        self.assertEqual(st["episode_count"], 1)
        self.assertEqual(st["recent_topics"], [_EVENT["text"][:50]])

    def test_short_monologue_aborts_everything(self):
        with _MainRun() as m:
            m.gen.return_value = "too short"
            AD.main()
        m.publish.assert_not_called(); m.slack.assert_not_called()
        self.assertFalse(AD.STATE_FILE.exists())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', _SRC)
        r = _sp.run([sys.executable, "-c", "import nova_after_dark"], cwd=str(_SCRIPTS), capture_output=True,
                    text=True, timeout=30, env={**_os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Nova After Dark ===", r.stdout)


def teardown_module(module=None):
    _shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
