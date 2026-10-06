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
                   patch.object(fm, "bus_notify"), patch.object(fm.time, "sleep")]
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

    def test_never_force_pushes(self):
        self.assertNotIn("--force", SRC)
        self.assertNotRegex(SRC, r'"push",\s*"-f"')


class TestPerformance(_Base):
    def test_scan_many_posts(self):
        for i in range(800):
            self.post("dreams", f"p{i}", f"Post {i}")
        t0 = time.perf_counter()
        missing = fm.get_posts_missing_images()
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(missing), 800)


class TestRetry(_Base):
    def test_pull_conflict_aborts_push(self):
        # RETRY GAP: main git sync — no retry; a failed pull --rebase aborts the rebase and never pushes
        self.post("dreams", "a", "A")
        calls, run = _git(pull_rc=1)
        with patch.object(fm, "generate_image_for_post", return_value=str(self.j / "x.webp")), \
             patch.object(fm, "add_image_to_post", return_value=True), patch.object(fm.subprocess, "run", side_effect=run):
            fm.main()
        self.assertIn(["git", "rebase", "--abort"], calls)
        self.assertNotIn(["git", "push"], calls)

    def test_image_failure_counts_failed(self):
        self.post("dreams", "a", "A")
        with patch.object(fm, "generate_image_for_post", return_value=None), \
             patch.object(fm.subprocess, "run") as run:
            fm.main()
        run.assert_not_called()
        self.assertIn("Failed: 1", self.bus.call_args.kwargs["body"])


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
                                        "style": "STYLE", "title": "📝 Hello"})
        self.assertTrue(gen.call_args[0][0].startswith("STYLE, inspired by: Hello"))
        self.assertIn("from nova_image_utils import generate_image", SRC)

    def test_png_is_converted_with_cwebp(self):
        p = self.post("dreams", "a", "A")
        png = Path(self.tmp.name) / "g.png"
        png.write_bytes(b"png")
        with patch.object(fm.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
            self.assertTrue(fm.add_image_to_post({"file": p, "section": "dreams", "title": "A"}, str(png)))
        self.assertEqual(run.call_args[0][0][0], "cwebp")
        self.assertTrue((self.j / "static/images/dreams/a.webp").exists())  # copy fallback


class TestFunctional(_Base):
    def test_golden_path_commits_and_pushes(self):
        self.post("dreams", "a", "A")
        src = Path(self.tmp.name) / "g.webp"
        src.write_bytes(b"i")
        calls, run = _git()
        with patch.object(fm, "generate_image_for_post", return_value=str(src)), \
             patch.object(fm.subprocess, "run", side_effect=run):
            fm.main()
        self.assertEqual([c[:2] for c in calls], [["git", "add"], ["git", "commit"], ["git", "pull"], ["git", "push"]])
        self.assertIn("Fixed: 1", self.bus.call_args.kwargs["body"])
        self.assertEqual(self.bus.call_args.kwargs["category"], "journal")

    def test_nothing_missing_is_quiet(self):
        with patch.object(fm.subprocess, "run") as run:
            fm.main()
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
