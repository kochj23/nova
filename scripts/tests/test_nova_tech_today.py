#!/usr/bin/env python3
"""Tests for nova_tech_today.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_tech_today.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="tech_today_"))


def _load():
    spec = importlib.util.spec_from_file_location("tech_today", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.LOG_FILE = TMP / "tech.log"
    mod.STATE_FILE = TMP / "state.json"
    mod.JOURNAL_DIR = TMP / "nova-journal"
    mod.CONTENT_DIR = mod.JOURNAL_DIR / "content/tech-today"
    mod.IMAGES_DIR = mod.JOURNAL_DIR / "static/images/tech-today"
    mod.notify = MagicMock()
    return mod


tt = _load()


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _net(route):
    """urlopen stand-in: route(url) -> dict payload or Exception."""
    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        a = route(url)
        if isinstance(a, Exception):
            raise a
        return _Resp(json.dumps(a).encode())
    return urlopen


ARTICLE = "# Quantum Chips Are Here\n\nA long opinionated piece. " * 40


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_key_from_keychain(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/\-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("sk-or-", SRC)
        self.assertIn('"-s", "nova-openrouter-api-key"', SRC)

    def test_private_memories_filtered_before_publish(self):
        route = lambda u: {"memories": [{"text": "public", "source": "research"}]}
        with patch.object(tt.urllib.request, "urlopen", _net(route)), \
             patch.object(tt.nova_config, "filter_private_memories", side_effect=lambda m: []) as flt, redirect_stdout(io.StringIO()):
            self.assertEqual(tt.recall_memories("q"), [])
        flt.assert_called_once()

    def test_slug_is_url_safe(self):
        self.assertEqual(tt.slugify("Hello, World!! & Friends"), "hello-world-friends")
        self.assertLessEqual(len(tt.slugify("x" * 200)), 60)
        self.assertNotIn("/", tt.slugify("a/b/c"))


class TestPerformance(unittest.TestCase):
    def test_gather_dedups_many_results_fast(self):
        big = [{"title": f"t{i}", "url": f"http://x/{i % 40}", "content": "c"} for i in range(5000)]
        with patch.object(tt, "search_searxng", return_value=big), redirect_stdout(io.StringIO()):
            t0 = time.perf_counter()
            out = tt.gather_web_results()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertLessEqual(len(out), 40)                 # deduped by url, capped


class TestRetry(unittest.TestCase):
    def test_search_failure_is_one_shot_and_fails_open(self):
        # RETRY GAP: search_searxng/urlopen — one attempt per query, [] on failure
        with patch.object(tt.urllib.request, "urlopen", side_effect=OSError("searxng down")) as u, redirect_stdout(io.StringIO()):
            self.assertEqual(tt.search_searxng("q"), [])
        self.assertEqual(u.call_count, 1)

    def test_main_aborts_when_too_few_results(self):
        with patch.object(tt, "gather_web_results", return_value=[{"title": "t", "url": "u", "content": "c"}]), \
             patch.object(tt, "select_topic") as sel, redirect_stdout(io.StringIO()):
            tt.main()
        sel.assert_not_called()
        self.assertEqual(tt.notify.call_args.args[0], "Tech Today failed")


class TestUnit(unittest.TestCase):
    def test_extract_title(self):
        self.assertEqual(tt.extract_title("## Big News Today\nbody"), "Big News Today")
        self.assertEqual(tt.extract_title("   \n#  \nShort\nReal Title Here"), "Real Title Here")
        self.assertEqual(tt.extract_title("hi\n"), "Tech Today")

    def test_state_roundtrip_and_corruption(self):
        tt.save_state({"recent_topics": ["ai"], "article_count": 3})
        self.assertEqual(tt.load_state()["article_count"], 3)
        tt.STATE_FILE.write_text("{not json")
        self.assertEqual(tt.load_state(), {"recent_topics": [], "article_count": 0})

    def test_select_topic_strips_code_fences(self):
        route = lambda u: {"choices": [{"message": {"content": '```json\n{"topic":"Quantum","keywords":["q"]}\n```'}}]}
        with patch.object(tt, "get_openrouter_key", return_value="k"), patch.object(tt.urllib.request, "urlopen", _net(route)), \
             redirect_stdout(io.StringIO()):
            td = tt.select_topic([{"title": "t", "url": "u", "content": "c"}], {"recent_topics": []})
        self.assertEqual(td["topic"], "Quantum")


class TestIntegration(unittest.TestCase):
    def test_recall_posts_to_memory_server(self):
        seen = []
        route = lambda u: seen.append(u) or {"memories": [{"text": "m", "source": "research"}]}
        with patch.object(tt.urllib.request, "urlopen", _net(route)), \
             patch.object(tt.nova_config, "filter_private_memories", side_effect=lambda m: m), redirect_stdout(io.StringIO()):
            tt.recall_memories("quantum", n=5)
        self.assertTrue(seen[0].startswith(tt.MEMORY_SERVER + "/recall?"))

    def test_publish_writes_frontmatter_and_sources(self):
        td = {"topic": "Quantum", "keywords": ["quantum", "chips"],
              "web_results": [{"title": "Src", "url": "http://x"}], "memories": [{"text": "note", "metadata": {"source": "research"}}]}
        with redirect_stdout(io.StringIO()):
            path = tt.publish_to_hugo(ARTICLE, td, None)
        text = Path(path).read_text()
        self.assertTrue(text.startswith('---\ntitle: "Quantum Chips Are Here"'))
        self.assertIn('tags: ["quantum", "chips"]', text)
        self.assertIn("[Src](http://x)", text)
        self.assertIn("*— Nova*", text)
        self.assertNotIn("# Quantum Chips Are Here", text.split("---", 3)[-1])   # title line stripped from body


class TestFunctional(unittest.TestCase):
    def test_main_golden_path_publishes_pushes_posts(self):
        tt.notify.reset_mock(); tt.STATE_FILE.unlink(missing_ok=True)
        td = {"topic": "Quantum", "angle": "a", "keywords": ["quantum"]}
        with patch.object(tt, "gather_web_results", return_value=[{"title": f"t{i}", "url": f"u{i}", "content": "c"} for i in range(10)]), \
             patch.object(tt, "select_topic", return_value=td), patch.object(tt, "recall_memories", return_value=[]), \
             patch.object(tt, "generate_article", return_value=ARTICLE), patch.object(tt, "generate_cover_image", return_value=None), \
             patch.object(tt.nj, "git_push") as push, redirect_stdout(io.StringIO()):
            tt.main()
        push.assert_called_once()
        self.assertEqual(tt.notify.call_args.args[0], "Tech Today published")
        state = json.loads(tt.STATE_FILE.read_text())
        self.assertEqual(state["recent_topics"], ["Quantum"])
        self.assertEqual(state["article_count"], 1)

    def test_main_aborts_on_empty_article(self):
        with patch.object(tt, "gather_web_results", return_value=[{"title": f"t{i}", "url": f"u{i}", "content": "c"} for i in range(10)]), \
             patch.object(tt, "select_topic", return_value={"topic": "X", "keywords": []}), \
             patch.object(tt, "recall_memories", return_value=[]), patch.object(tt, "generate_article", return_value=None), \
             patch.object(tt.nj, "git_push") as push, redirect_stdout(io.StringIO()):
            tt.main()
        push.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_tech_today as m; print(len(m.SEARCH_QUERIES))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "8")


if __name__ == "__main__":
    unittest.main()
