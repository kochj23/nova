#!/usr/bin/env python3
"""Tests for nova_meta_analysis.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_meta_analysis.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="meta_analysis_"))


@contextmanager
def _stubbed(mods):
    """Set sys.modules keys for the duration and restore ONLY those keys afterwards."""
    old = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _fake_publish_hugo(title, body, section, tags, description, emoji="", stable_slug=None, **kw):
    """Stands in for nova_journal.publish_hugo: writes the post where the real one would."""
    d = TMP / "nova-journal" / "content" / section
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stable_slug}.md").write_text(f'---\ntitle: "{emoji} {title}"\ntags: {json.dumps(tags)}\n---\n{body}')
    return True


def _load():
    # nova_journal resolves service URLs through PG at import: stand it in for the load
    nj = types.ModuleType("nova_journal"); nj.git_push = MagicMock()
    nj.publish_hugo = MagicMock(side_effect=_fake_publish_hugo)
    with _stubbed({"nova_journal": nj}):
        spec = importlib.util.spec_from_file_location("meta_analysis", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    mod.HUGO_ROOT = TMP / "nova-journal"
    mod.CONTENT_OUT = mod.HUGO_ROOT / "content/meta"
    mod.LOG_FILE = TMP / "meta.log"
    mod.STATE_FILE = TMP / "state.json"
    mod.extract_tags = lambda *a, **k: ["meta", "reflection"]
    mod.nova_config = types.SimpleNamespace(openrouter_api_key=lambda: "", post_both=MagicMock(), SLACK_CHAN="C_TEST")
    return mod


ma = _load()


def _post(cat, i, days_ago=1, tags='["dreams", "memory"]', body="Lanterns drifting over water memory memory"):
    d = ma.HUGO_ROOT / "content" / cat
    d.mkdir(parents=True, exist_ok=True)
    dt = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")
    (d / f"post-{i}.md").write_text(f'---\ntitle: "Post {i}"\ndate: {dt}\ntags: {tags}\n---\n{body}\n')


class _Resp(io.BytesIO):
    pass


def _fresh_site():
    import shutil
    shutil.rmtree(ma.HUGO_ROOT, ignore_errors=True)
    if ma.STATE_FILE.exists():
        ma.STATE_FILE.unlink()


class _FakeDate(date):
    fixed = date(2026, 3, 1)       # a Sunday, day 1

    @classmethod
    def today(cls):
        return cls.fixed


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_key_from_config(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/\-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("sk-or-", SRC)
        self.assertIn("nova_config.openrouter_api_key()", SRC)

    def test_post_body_excerpt_bounded(self):
        _fresh_site()
        _post("essays", 1, body="Z" * 5000)
        posts = ma._collect_month_posts()
        self.assertEqual(len(posts[0]["body"]), 800)

    def test_underscore_files_and_old_posts_skipped(self):
        _fresh_site()
        _post("essays", 1); _post("essays", 2, days_ago=60)
        (ma.HUGO_ROOT / "content/essays/_index.md").write_text("---\ndate: 2099-01-01\n---\n")
        self.assertEqual([p["slug"] for p in ma._collect_month_posts()], ["post-1"])


class TestPerformance(unittest.TestCase):
    def test_pattern_analysis_10k_posts(self):
        posts = [{"category": ("dreams", "essays")[i % 2], "tags": [f"t{i % 30}"], "body": "lantern water ocean " * 20}
                 for i in range(10_000)]
        t0 = time.perf_counter()
        p = ma._analyze_patterns(posts)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(p["total_posts"], 10_000)
        self.assertEqual(len(p["top_tags"]), 15)


class TestRetry(unittest.TestCase):
    def test_openrouter_failure_falls_back_to_ollama(self):
        urls = []

        def net(req, timeout=None):
            urls.append(req.full_url)
            if "openrouter" in req.full_url:
                raise OSError("429")
            return _Resp(json.dumps({"response": "local essay"}).encode())
        pats = ma._analyze_patterns([])
        with patch.object(ma.nova_config, "openrouter_api_key", lambda: "k"), patch.object(ma.urllib.request, "urlopen", net), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(ma._generate_meta_analysis([], pats, "March 2026"), "local essay")
        self.assertEqual(urls, [ma.OPENROUTER, ma.OLLAMA_URL])

    def test_both_backends_down_returns_none(self):
        # RETRY GAP: _generate_meta_analysis — one try per backend, None when both fail
        with patch.object(ma.urllib.request, "urlopen", side_effect=OSError("down")) as u, redirect_stdout(io.StringIO()):
            self.assertIsNone(ma._generate_meta_analysis([], ma._analyze_patterns([]), "m"))
        self.assertEqual(u.call_count, 1)                 # no key -> Ollama only, once


class TestUnit(unittest.TestCase):
    def test_should_run_only_first_sunday_and_not_twice(self):
        _fresh_site()
        with patch.object(ma, "date", _FakeDate):
            self.assertTrue(ma._should_run())
            ma.STATE_FILE.write_text(json.dumps({"last_run": datetime.now().isoformat()}))
            self.assertFalse(ma._should_run())
            _FakeDate.fixed = date(2026, 3, 8)               # second Sunday
            self.assertFalse(ma._should_run())
            _FakeDate.fixed = date(2026, 3, 2)               # Monday
            self.assertFalse(ma._should_run())
        _FakeDate.fixed = date(2026, 3, 1)

    def test_analyze_empty(self):
        p = ma._analyze_patterns([])
        self.assertEqual((p["total_posts"], p["most_active_category"]), (0, "unknown"))

    def test_stopwords_dropped(self):
        p = ma._analyze_patterns([{"category": "x", "tags": [], "body": "about nova jordan lantern lantern"}])
        self.assertEqual(p["recurring_words"], [("lantern", 2)])


class TestIntegration(unittest.TestCase):
    def test_publish_goes_through_publish_hugo_with_meta_profile_and_sources(self):
        """2026-10-06: the monthly meta must use the regular pipeline (>=3000-word grounded
        expansion + check), not its own writer — profile 'meta' + the month's material as sources."""
        _fresh_site()
        ma.nj.git_push.reset_mock(); ma.nj.publish_hugo.reset_mock()
        posts = [{"category": "dreams", "title": "Lanterns Over Burbank", "tags": ["dreams"], "body": "SRC-BODY-7 water",
                  "slug": "2026-09-01-lanterns", "url": "/dreams/2026-09-01-lanterns/"}]
        pats = {"total_posts": 1, "by_category": {"dreams": 1}, "top_tags": [("dreams", 1)],
                "recurring_words": [("water", 1)], "most_active_category": "dreams"}
        with redirect_stdout(io.StringIO()):
            url = ma._publish("I keep dreaming of water.", pats, posts, "March 2026")
        (title, body, section, tags, desc), kw = ma.nj.publish_hugo.call_args
        self.assertEqual((section, kw["profile"], kw["emoji"]), ("meta", "meta", "🔮"))
        self.assertEqual(title, "What My Mind Has Been Doing — March 2026")
        self.assertRegex(kw["stable_slug"], r"^\d{4}-\d{2}-what-my-mind-has-been-doing$")
        self.assertEqual(url, f"/meta/{kw['stable_slug']}/")
        for needle in ("Lanterns Over Burbank", "SRC-BODY-7", "/dreams/2026-09-01-lanterns/", '"dreams": 1', "MONTH: March 2026"):
            self.assertIn(needle, kw["sources"])
        self.assertIn("| dreams | 1 |", body); self.assertTrue(body.startswith("I keep dreaming of water."))
        ma.nj.git_push.assert_called_once_with("meta", "March 2026 self-analysis")
        self.assertNotIn("write_text(", SRC[SRC.index("def _publish("):SRC.index("def main(")])   # no private writer
        # guard refusal -> nothing pushed, no URL
        ma.nj.git_push.reset_mock()
        with patch.object(ma.nj, "publish_hugo", return_value=False), redirect_stdout(io.StringIO()):
            self.assertIsNone(ma._publish("x", pats, posts, "March 2026"))
        ma.nj.git_push.assert_not_called()

    def test_publish_writes_hugo_post_and_pushes(self):
        _fresh_site()
        ma.nj.git_push.reset_mock()
        pats = {"by_category": {"dreams": 3, "essays": 5}, "top_tags": [("memory", 4)]}
        with redirect_stdout(io.StringIO()):
            url = ma._publish("I keep dreaming of water.", pats, [], "March 2026")
        self.assertTrue(url.startswith("/meta/") and url.endswith("-what-my-mind-has-been-doing/"))
        md = next(ma.CONTENT_OUT.glob("*.md")).read_text()
        self.assertIn('tags: ["meta", "reflection"]', md)
        self.assertLess(md.index("| essays | 5 |"), md.index("| dreams | 3 |"))
        ma.nj.git_push.assert_called_once_with("meta", "March 2026 self-analysis")


class TestFunctional(unittest.TestCase):
    def test_main_golden_path_posts_and_records_state(self):
        _fresh_site()
        for i in range(6):
            _post(("dreams", "essays")[i % 2], i)
        ma.nova_config.post_both.reset_mock()
        with patch.object(ma, "_should_run", return_value=True), \
             patch.object(ma, "_generate_meta_analysis", return_value="Water everywhere."), redirect_stdout(io.StringIO()):
            ma.main()
        msg = ma.nova_config.post_both.call_args.args[0]
        self.assertIn("Water everywhere.", msg)
        self.assertEqual(ma.nova_config.post_both.call_args.kwargs["slack_channel"], "C_TEST")
        self.assertIn("last_run", json.loads(ma.STATE_FILE.read_text()))

    def test_main_skips_with_too_few_posts(self):
        _fresh_site()
        _post("dreams", 1)
        ma.nova_config.post_both.reset_mock()
        with patch.object(ma, "_should_run", return_value=True), patch.object(ma, "_generate_meta_analysis") as gen, \
             redirect_stdout(io.StringIO()) as out:
            ma.main()
        gen.assert_not_called()
        ma.nova_config.post_both.assert_not_called()
        self.assertIn("Not enough posts", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        # separate interpreter: stand in nova_journal (PG at import) and forbid the network
        code = ("import sys, types, urllib.request\n"
                "nj = types.ModuleType('nova_journal'); nj.git_push = None\n"
                "sys.modules.update({'nova_journal': nj})\n"
                "def _no(*a, **k): raise AssertionError('network at import')\n"
                "urllib.request.urlopen = _no\n"
                "import nova_meta_analysis as m\nprint(m.MODEL_OLLAMA)\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "qwen3-coder:30b")


if __name__ == "__main__":
    unittest.main()
