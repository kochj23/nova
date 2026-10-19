#!/usr/bin/env python3
"""Tests for nova_regenerate_all_images.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
This script OVERWRITES every journal image, so every test points HUGO_ROOT at a throwaway tempdir tree and
mocks the OpenRouter generator; --dry-run is proven to generate and overwrite nothing."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_regenerate_all_images.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("regenerate_images_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ri = _load()
# module-level stubs: never the real nova-journal tree, never the real ComfyUI, never sleep
_SAFE = Path(tempfile.mkdtemp(prefix="regen-safe-"))
ri.HUGO_ROOT, ri.IMAGES_ROOT, ri.CONTENT_ROOT = _SAFE, _SAFE / "static/images", _SAFE / "content"
ri.generate_image = MagicMock(return_value=None)
ri.time = types.SimpleNamespace(sleep=MagicMock())


def _tree(posts, images, section="essays"):
    """Build a throwaway Hugo tree and point the module at it."""
    root = Path(tempfile.mkdtemp(prefix="regen-test-"))
    (root / "content" / section).mkdir(parents=True)
    (root / "static/images" / section).mkdir(parents=True)
    for name, fm in posts.items():
        (root / "content" / section / name).write_text(fm)
    for name in images:
        (root / "static/images" / section / name).write_bytes(b"OLD")
    ri.HUGO_ROOT, ri.IMAGES_ROOT, ri.CONTENT_ROOT = root, root / "static/images", root / "content"
    return root


def _quiet(fn, *a, **k):
    with redirect_stdout(io.StringIO()) as buf:
        out = fn(*a, **k)
    return out, buf.getvalue()


POST = '---\ntitle: "On Clocks"\ndescription: \'Time, measured\'\ntags: ["oil", "time"]\n---\nbody'


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn("from nova_image_utils import generate_image", SRC)        # local ComfyUI via the shared util
        self.assertNotIn("openrouter", SRC.lower())                               # no cloud images (2026-10-19)

    def test_dry_run_overwrites_nothing(self):
        root = _tree({"2026-01-01-clocks.md": POST}, ["2026-01-01-clocks.png"])
        ri.generate_image.reset_mock()
        with patch.object(sys, "argv", ["x", "--dry-run"]):
            _quiet(ri.main)
        ri.generate_image.assert_not_called()
        self.assertEqual((root / "static/images/essays/2026-01-01-clocks.png").read_bytes(), b"OLD")

    def test_only_existing_png_targets_touched(self):
        root = _tree({}, ["a.png"])
        (root / "static/images/essays/keep.jpg").write_bytes(b"JPG")
        gen = Path(tempfile.mkdtemp()) / "new.png"; gen.write_bytes(b"NEW")
        with patch.object(ri, "generate_image", return_value=str(gen)):
            _quiet(ri.regenerate_section, "essays")
        self.assertEqual((root / "static/images/essays/keep.jpg").read_bytes(), b"JPG")
        self.assertEqual(sorted(p.name for p in (root / "static/images/essays").iterdir()), ["a.png", "keep.jpg"])


    def test_webp_target_is_encoded_not_copied(self):
        dest = Path(tempfile.mkdtemp()) / "c.webp"
        src = Path(tempfile.mkdtemp()) / "n.png"; src.write_bytes(b"PNG")
        def fake(cmd, **kw):
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"WEBP")
            return subprocess.CompletedProcess(cmd, 0)
        with patch.object(ri.subprocess, "run", side_effect=fake) as run:
            self.assertTrue(ri._install(str(src), dest))
        self.assertEqual(run.call_args[0][0][0], "cwebp")
        self.assertEqual(dest.read_bytes(), b"WEBP")


class TestPerformance(unittest.TestCase):
    def test_build_prompt_10k(self):
        fm = {"title": "T", "description": "d" * 1000, "tags": "noir"}
        t0 = time.perf_counter()
        for _ in range(10_000):
            p = ri.build_prompt("art", fm)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertLess(len(p), 400)                            # description capped at 150 chars


class TestRetry(unittest.TestCase):
    def test_generation_failure_counts_and_keeps_old_image(self):
        # RETRY GAP: regenerate_section/generate_image — one attempt per image; a failure is counted,
        # the original image is left in place, and the run continues to the next image
        root = _tree({}, ["a.png", "b.png"])
        gen = Path(tempfile.mkdtemp()) / "g.png"; gen.write_bytes(b"NEW")
        with patch.object(ri, "generate_image", side_effect=[None, str(gen)]) as g:
            (ok, bad), out = _quiet(ri.regenerate_section, "essays")
        self.assertEqual((ok, bad, g.call_count), (1, 1, 2))
        self.assertEqual((root / "static/images/essays/a.png").read_bytes(), b"OLD")
        self.assertEqual((root / "static/images/essays/b.png").read_bytes(), b"NEW")
        self.assertIn("FAILED", out)


class TestUnit(unittest.TestCase):
    def test_extract_frontmatter(self):
        f = Path(tempfile.mkdtemp()) / "p.md"; f.write_text(POST)
        self.assertEqual(ri.extract_frontmatter(f),
                         {"title": "On Clocks", "description": "Time, measured", "tags": '["oil", "time"]'})
        f.write_text("no front matter")
        self.assertEqual(ri.extract_frontmatter(f), {})

    def test_build_prompt_styles(self):
        self.assertIn("Oil painting, visible brushstrokes", ri.build_prompt("art", {"title": "x", "tags": "OIL"}))
        self.assertIn("gallery quality", ri.build_prompt("art", {"title": "x"}))
        self.assertEqual(ri.build_prompt("unknown", {"title": "Hi", "description": "d"}), "Create a cover image for: Hi. d")
        self.assertIn("{braces}", ri.build_prompt("essays", {"title": "{braces}"}))   # titles never re-formatted

    def test_find_post_for_image(self):
        root = _tree({"2026-04-05-dream.md": POST, "_index.md": POST}, [], section="dreams")
        self.assertEqual(ri.find_post_for_image("dreams", "2026-04-05-dream.png").name, "2026-04-05-dream.md")
        self.assertEqual(ri.find_post_for_image("dreams", "2026-04-05.png").name, "2026-04-05-dream.md")
        self.assertIsNone(ri.find_post_for_image("dreams", "1999-01-01-x.png"))
        self.assertEqual(ri.regenerate_section("nope"), (0, 0))


class TestIntegration(unittest.TestCase):
    def test_post_metadata_feeds_section_prompt(self):
        _tree({"2026-01-01-clocks.md": POST}, ["2026-01-01-clocks.png"])
        with patch.object(ri, "generate_image", return_value=None) as g:
            _quiet(ri.regenerate_section, "essays")
        prompt, section = g.call_args[0][0], g.call_args[1]["section"]
        self.assertEqual(section, "essays")
        self.assertIn("Topic: On Clocks. Time, measured", prompt)
        self.assertTrue(set(ri.SECTIONS) <= set(ri.SECTION_PROMPT_STYLE))


class TestFunctional(unittest.TestCase):
    def test_main_replaces_images_and_reports(self):
        root = _tree({"2026-01-01-clocks.md": POST}, ["2026-01-01-clocks.png"])
        gen = Path(tempfile.mkdtemp()) / "out.png"; gen.write_bytes(b"FRESH")
        ri.time.sleep.reset_mock()
        with patch.object(ri, "generate_image", return_value=str(gen)), patch.object(sys, "argv", ["x"]):
            _, out = _quiet(ri.main)
        self.assertEqual((root / "static/images/essays/2026-01-01-clocks.png").read_bytes(), b"FRESH")
        self.assertFalse(gen.exists())                          # temp generator output cleaned up
        self.assertIn("COMPLETE: 1 succeeded, 0 failed", out)
        self.assertEqual(ri.time.sleep.call_count, 1)           # GPU breather between generations


class TestFrame(unittest.TestCase):
    def test_dry_run_exits_zero_in_empty_home(self):
        # --dry-run against an empty HOME: no journal tree, nothing generated, clean exit
        home = Path(tempfile.mkdtemp(prefix="regen-home-"))
        r = subprocess.run([sys.executable, str(SCRIPT), "--dry-run"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(home)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("COMPLETE: 0 succeeded, 0 failed", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(ri.main))


if __name__ == "__main__":
    unittest.main()
