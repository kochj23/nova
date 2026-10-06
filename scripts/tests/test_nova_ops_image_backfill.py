#!/usr/bin/env python3
"""Tests for nova_ops_image_backfill.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The script is a flat top-level program (no main()), so every test drives it with runpy under a
temporary HOME: ~/nova-journal and ~/.openclaw/logs resolve inside a tempdir, image generation is a
stub and every git/cwebp call is a recorded mock."""
import os
import re
import runpy
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ops_image_backfill.py"
SRC = SCRIPT.read_text()


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


def _home(articles):
    home = Path(tempfile.mkdtemp(prefix="ops_backfill_"))
    (home / ".openclaw/logs").mkdir(parents=True)
    content = home / "nova-journal/content/operations"
    content.mkdir(parents=True)
    for name, text in articles.items():
        (content / name).write_text(text)
    return home


def _run(home, image_ok=True, git_rc=None):
    """Execute the script; returns (git/cwebp calls, generate_image mock)."""
    git_rc = git_rc or {}
    calls = []
    img = home / "gen.png"; img.write_bytes(b"png")

    def run(cmd, **kw):
        calls.append(cmd)
        verb = cmd[1] if cmd[0] == "git" else cmd[0]
        if cmd[0] == "cwebp":
            Path(cmd[-1]).write_bytes(b"webp")
        return MagicMock(returncode=git_rc.get(verb, 0), stdout="", stderr="conflict" if git_rc.get(verb) else "")
    gen = MagicMock(return_value=str(img) if image_ok else None)
    utils = types.ModuleType("nova_image_utils"); utils.generate_image = gen
    with _stubbed({"nova_image_utils": utils}), patch.dict(os.environ, {"HOME": str(home)}), \
         patch("subprocess.run", run):
        runpy.run_path(str(SCRIPT), run_name="ops_backfill_under_test")
    return calls, gen


NO_COVER = '---\ntitle: "Pager Storm"\ndescription: "a bad night"\n---\nbody\n'
HAS_COVER = '---\ntitle: "x"\ncover:\n  image: "/a.webp"\n---\nbody\n'
INLINE_IMG = '---\ntitle: "y"\n---\n![pic](/p.png)\n'


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_no_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)

    def test_writes_stay_inside_the_repo(self):
        home = _home({"a.md": NO_COVER})
        calls, _ = _run(home)
        cwebp = [c for c in calls if c[0] == "cwebp"][0]
        self.assertTrue(cwebp[-1].startswith(str(home / "nova-journal/static/images/operations")))
        self.assertTrue(all(c[0] in ("git", "cwebp") for c in calls))


class TestPerformance(unittest.TestCase):
    def test_scans_many_already_covered_articles_fast(self):
        home = _home({f"p{i}.md": HAS_COVER for i in range(2000)})
        t0 = time.perf_counter()
        calls, gen = _run(home)
        self.assertLess(time.perf_counter() - t0, 5.0)
        gen.assert_not_called()
        self.assertEqual(calls, [])                         # nothing fixed -> no git at all


class TestRetry(unittest.TestCase):
    def test_image_failure_skips_article_without_retry(self):
        # RETRY GAP: generate_image — one attempt per article; failure logs and moves on
        home = _home({"a.md": NO_COVER, "b.md": NO_COVER})
        calls, gen = _run(home, image_ok=False)
        self.assertEqual(gen.call_count, 2)
        self.assertEqual(calls, [])
        self.assertIn("image FAILED for a.md", (home / ".openclaw/logs/ops_image_backfill.log").read_text())

    def test_rebase_conflict_aborts_push(self):
        home = _home({"a.md": NO_COVER})
        calls, _ = _run(home, git_rc={"pull": 1})
        verbs = [c[1] for c in calls if c[0] == "git"]
        self.assertEqual(verbs, ["add", "commit", "pull", "rebase"])
        self.assertIn("push ABORTED", (home / ".openclaw/logs/ops_image_backfill.log").read_text())


class TestUnit(unittest.TestCase):
    def test_skip_rules(self):
        home = _home({"_index.md": NO_COVER, "c.md": HAS_COVER, "i.md": INLINE_IMG})
        _, gen = _run(home)
        gen.assert_not_called()

    def test_prompt_built_from_title_and_description(self):
        home = _home({"a.md": NO_COVER})
        _, gen = _run(home)
        prompt = gen.call_args.args[0]
        self.assertIn("titled 'Pager Storm'", prompt)
        self.assertIn("a bad night", prompt)
        self.assertEqual(gen.call_args.kwargs["section"], "operations")


class TestIntegration(unittest.TestCase):
    def test_cover_frontmatter_inserted(self):
        home = _home({"a.md": NO_COVER})
        _run(home)
        text = (home / "nova-journal/content/operations/a.md").read_text()
        self.assertIn('cover:\n  image: "/images/operations/a.webp"\n  alt: "Pager Storm"\n  relative: false\n---', text)
        self.assertTrue(text.endswith("body\n"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_commits_and_pushes(self):
        home = _home({"a.md": NO_COVER, "b.md": HAS_COVER})
        calls, _ = _run(home)
        verbs = [c[1] for c in calls if c[0] == "git"]
        self.assertEqual(verbs, ["add", "commit", "pull", "push"])
        commit = [c for c in calls if c[:2] == ["git", "commit"]][0]
        self.assertIn("1 articles", commit[-1])
        self.assertIn("BACKFILL DONE — fixed 1: ['a.md']", (home / ".openclaw/logs/ops_image_backfill.log").read_text())


class TestFrame(unittest.TestCase):
    def test_runs_clean_on_empty_repo(self):
        # no main(): running IS the job. Under an empty temp HOME it must exit 0 and change nothing.
        home = _home({})
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "HOME": str(home), "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("fixed 0", (home / ".openclaw/logs/ops_image_backfill.log").read_text())
        self.assertNotIn("def main", SRC)


if __name__ == "__main__":
    unittest.main()
