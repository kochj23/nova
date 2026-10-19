#!/usr/bin/env python3
"""Tests for nova_fix_missing_images.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
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
SCRIPT = SCRIPTS / "nova_fix_missing_images.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_fix_missing_images_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fm = _load()
_real_build_prompt = fm.build_prompt   # _Base stubs build_prompt (it calls the LLM)
POST = '---\ntitle: "{t}"\ndate: 2026-01-01\n---\nBody text.\n'


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        j = Path(self.tmp.name) / "nova-journal"
        (j / "content" / "dreams").mkdir(parents=True)
        (j / "content" / "adhoc").mkdir(parents=True)
        (j / "static" / "images").mkdir(parents=True)
        self.j = j
        self.ps = [patch.object(fm, "JOURNAL_DIR", j), patch.object(fm, "CONTENT_DIR", j / "content"),
                   patch.object(fm, "STATIC_DIR", j / "static/images"),
                   patch.object(fm, "LOG_FILE", str(Path(self.tmp.name) / "fix.log")),
                   patch.object(fm, "bus_notify"), patch.object(fm.time, "sleep"),
                   patch.object(fm, "LOCK_FILE", Path(self.tmp.name) / "fix.lock"),
                   patch.object(fm, "MIN_AGE_S", 0),
                   patch.object(fm, "_acquire_push_lock", return_value=None),
                   patch.object(fm, "build_prompt", side_effect=lambda post: f"PROMPT {post['title']}")]
        self.bus = [p.start() for p in self.ps][4]
        self._r = redirect_stdout(io.StringIO())
        self._r.__enter__()

    def tearDown(self):
        self._r.__exit__(None, None, None)
        for p in self.ps:
            p.stop()
        self.tmp.cleanup()

    def post(self, section, name, title):
        p = self.j / "content" / section / f"{name}.md"
        p.write_text(POST.format(t=title))
        return p


def _git(pull_rc=0):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        rc = pull_rc if cmd[:2] == ["git", "pull"] else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="conflict" if rc else "")
    return calls, run


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")
        self.assertNotIn("shell=True", SRC)

    def test_never_force_pushes_stashes_or_adds_everything(self):
        self.assertNotIn("--force", SRC)
        self.assertNotRegex(SRC, r'"push",\s*"-f"')
        self.assertNotRegex(SRC, r"\"stash\"|--autostash")
        self.assertNotIn('"-A"', SRC)
        self.assertNotIn("openrouter", SRC.lower())               # local images only (2026-10-19)


class TestPerformance(_Base):
    def test_scan_many_posts(self):
        for i in range(800):
            self.post("dreams", f"p{i}", f"Post {i}")
        t0 = time.perf_counter()
        missing = fm.get_posts_missing_images()
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(missing), 800)

    def test_budget_stops_before_starting_another_cover(self):
        for i in range(5):
            self.post("dreams", f"p{i}", f"P{i}")
        with patch.object(fm, "generate_image_for_post", return_value=None) as gen, \
             patch.object(fm.subprocess, "run"):
            fm.main(["--budget", str(fm.EST_IMAGE_S + fm.COMMIT_RESERVE_S - 1)])
        gen.assert_not_called()

    def test_young_posts_are_left_to_their_publisher(self):
        self.post("dreams", "fresh", "Fresh")
        self.assertEqual(fm.get_posts_missing_images(min_age_s=3600), [])


class TestRetry(_Base):
    def test_pull_conflict_aborts_rebase_and_never_pushes(self):
        # No retry here by design: a failed pull --rebase aborts the rebase, leaves the commit
        # local, and the journal's stranded-commit sweep (every 10 min on .6) pushes it.
        p = self.post("dreams", "a", "A")
        img = self.j / "static/images/dreams/a.webp"
        calls, run = _git(pull_rc=1)
        with patch.object(fm, "generate_image_for_post", return_value=str(img)), \
             patch.object(fm, "add_image_to_post", return_value=img), patch.object(fm.subprocess, "run", side_effect=run):
            fm.main([])
        self.assertIn(["git", "rebase", "--abort"], calls)
        self.assertFalse([c for c in calls if c[:2] == ["git", "push"]])

    def test_image_failure_counts_failed_and_commits_nothing(self):
        self.post("dreams", "a", "A")
        with patch.object(fm, "generate_image_for_post", return_value=None), \
             patch.object(fm.subprocess, "run") as run:
            fm.main([])
        run.assert_not_called()
        self.assertIn("Failed: 1", self.bus.call_args.kwargs["body"])

    def test_post_moved_mid_run_does_not_cost_the_batch_its_commit(self):
        a = self.post("dreams", "a", "A")
        b = self.post("dreams", "b", "B")
        src = Path(self.tmp.name) / "g.webp"
        src.write_bytes(b"i")
        calls, run = _git()

        def gen(post, budget_s=None):
            if post["file"] == b:
                b.unlink()                       # another agent moved it while we rendered
            return str(src)
        with patch.object(fm, "generate_image_for_post", side_effect=gen), \
             patch.object(fm.subprocess, "run", side_effect=run):
            fm.main([])
        add = [c for c in calls if c[:2] == ["git", "add"]][0]
        self.assertEqual(sorted(add[3:]), ["content/dreams/a.md", "static/images/dreams/a.webp"])
        self.assertIn("Fixed: 1", self.bus.call_args.kwargs["body"])

    def test_second_instance_exits_without_work(self):
        self.post("dreams", "a", "A")
        held = fm._single_instance()
        try:
            with patch.object(fm, "generate_image_for_post") as gen:
                fm.main([])
            gen.assert_not_called()
        finally:
            held.close()


class TestUnit(_Base):
    def test_existing_cover_is_skipped_and_default_style(self):
        img = self.j / "static/images/dreams/a.webp"
        img.parent.mkdir(parents=True)
        img.write_bytes(b"x")
        p = self.post("dreams", "a", "A")
        p.write_text(p.read_text().replace("---\nBody", 'cover:\n  image: "/images/dreams/a.webp"\n---\nBody'))
        self.post("adhoc", "b", "B")
        (self.j / "content/dreams/_index.md").write_text("x")
        missing = fm.get_posts_missing_images()
        self.assertEqual([m["section"] for m in missing], ["adhoc"])
        self.assertEqual(missing[0]["style"], fm.DEFAULT_STYLE)

    def test_zero_byte_missing_and_empty_refs_count_as_missing(self):
        d = self.j / "static/images/dreams"
        d.mkdir(parents=True)
        (d / "zero.webp").write_bytes(b"")
        for name, ref in (("zero", '"/images/dreams/zero.webp"'), ("gone", '"/images/dreams/gone.webp"'), ("empty", '""')):
            p = self.post("dreams", name, name)
            p.write_text(p.read_text().replace("---\nBody", f"cover:\n  image: {ref}\n---\nBody"))
        self.assertEqual(sorted(m["file"].stem for m in fm.get_posts_missing_images()), ["empty", "gone", "zero"])

    def test_drafts_and_description_parsing(self):
        p = self.post("dreams", "d", "Draft")
        p.write_text(p.read_text().replace("date:", "draft: true\ndate:"))
        q = self.post("dreams", "e", "Essay")
        q.write_text(q.read_text().replace("date:", 'description: "A walk through clocks"\ndate:'))
        missing = fm.get_posts_missing_images()
        self.assertEqual([m["file"].stem for m in missing], ["e"])
        self.assertEqual(missing[0]["description"], "A walk through clocks")

    def test_add_image_rewrites_frontmatter_once(self):
        p = self.post("dreams", "a", "A")
        src = Path(self.tmp.name) / "gen.webp"
        src.write_bytes(b"img")
        post = {"file": p, "section": "dreams", "title": "A"}
        self.assertTrue(fm.add_image_to_post(post, str(src)))
        self.assertTrue(fm.add_image_to_post(post, str(src)))
        txt = p.read_text()
        self.assertEqual(txt.count("cover:"), 1)
        self.assertIn('image: "/images/dreams/a.webp"', txt)
        self.assertIn("\n---\nBody", txt)                     # front matter still closes cleanly
        self.assertTrue(fm.cover_ok(txt))

    def test_malformed_frontmatter(self):
        p = self.j / "content/dreams/bad.md"
        p.write_text("no frontmatter here")
        src = Path(self.tmp.name) / "g.webp"
        src.write_bytes(b"i")
        self.assertFalse(fm.add_image_to_post({"file": p, "section": "dreams", "title": ""}, str(src)))


class TestIntegration(_Base):
    def test_reuses_existing_render_and_uses_shared_generator(self):
        p = self.post("dreams", "a", "A")
        existing = self.j / "static/images/dreams/a.png"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"x")
        with patch.object(fm, "generate_image") as gen:
            self.assertEqual(fm.generate_image_for_post({"file": p, "section": "dreams", "style": "s", "title": "A"}),
                             str(existing))
            gen.assert_not_called()
            fm.generate_image_for_post({"file": self.j / "content/dreams/zz.md", "section": "dreams",
                                        "style": "STYLE", "title": "Hello"})
        self.assertEqual(gen.call_args[0][0], "PROMPT Hello")
        self.assertEqual(gen.call_args[1]["section"], "dreams")
        self.assertIn("from nova_image_utils import generate_image", SRC)

    def test_prompt_comes_from_title_and_description_via_journal_helper(self):
        fake = MagicMock()
        fake.get_image_prompt.return_value = "a lighthouse at dusk"
        post = {"file": Path("x.md"), "section": "research", "style": "STYLE",
                "title": "🧵 This Week in My Head", "description": "Nova's synthesis"}
        with patch.dict(sys.modules, {"nova_journal": fake}):
            self.assertEqual(_real_build_prompt(post), "a lighthouse at dusk")
        fake.get_image_prompt.assert_called_once_with("This Week in My Head", "Nova's synthesis", "research")
        with patch.dict(sys.modules, {"nova_journal": None}):           # helper unavailable
            self.assertTrue(_real_build_prompt(post).startswith("STYLE, inspired by: This Week"))

    def test_png_is_converted_with_cwebp_and_failure_refuses(self):
        p = self.post("dreams", "a", "A")
        png = Path(self.tmp.name) / "g.png"
        png.write_bytes(b"png")
        with patch.object(fm.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
            self.assertIsNone(fm.add_image_to_post({"file": p, "section": "dreams", "title": "A"}, str(png)))
        self.assertEqual(run.call_args[0][0][0], "cwebp")
        self.assertNotIn("cover:", p.read_text())

        def ok(cmd, **kw):
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"WEBP")
            return subprocess.CompletedProcess(cmd, 0)
        with patch.object(fm.subprocess, "run", side_effect=ok):
            dest = fm.add_image_to_post({"file": p, "section": "dreams", "title": "A"}, str(png))
        self.assertEqual(dest.read_bytes(), b"WEBP")


class TestFunctional(_Base):
    def test_golden_path_commits_only_its_paths_and_pushes(self):
        self.post("dreams", "a", "A")
        src = Path(self.tmp.name) / "g.webp"
        src.write_bytes(b"i")
        calls, run = _git()
        with patch.object(fm, "generate_image_for_post", return_value=str(src)), \
             patch.object(fm.subprocess, "run", side_effect=run):
            fm.main([])
        self.assertEqual([c[:2] for c in calls], [["git", "add"], ["git", "commit"], ["git", "pull"], ["git", "push"]])
        add = calls[0]
        self.assertEqual(add[2], "--")
        self.assertEqual(sorted(add[3:]), ["content/dreams/a.md", "static/images/dreams/a.webp"])
        self.assertEqual(sorted(calls[1][calls[1].index("--") + 1:]), sorted(add[3:]))   # commit is path-limited
        self.assertIn("Fixed: 1", self.bus.call_args.kwargs["body"])
        self.assertEqual(self.bus.call_args.kwargs["category"], "journal")

    def test_nothing_missing_is_quiet(self):
        with patch.object(fm.subprocess, "run") as run:
            fm.main([])
        run.assert_not_called()
        self.bus.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_fix_missing_images; print('ok')"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
