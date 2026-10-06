#!/usr/bin/env python3
"""Tests for nova_weekly_ops_capstone.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

main() publishes to Hugo and git-pushes: every one of those calls is mocked for the whole file."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_weekly_ops_capstone.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("weekly_capstone_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cap = _load()
_PATCHES = []


def setUpModule():
    for name in ("generate_image", "publish_hugo", "git_push"):
        p = patch.object(cap, name, side_effect=AssertionError(f"unmocked {name}"))
        p.start(); _PATCHES.append(p)
    p = patch.object(cap.nova_config, "post_both"); p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


def _main(image=("/tmp/x.png",), publish=True, image_exc=None):
    with patch.object(cap, "generate_image", return_value=image[0], side_effect=image_exc) as gi, \
            patch.object(cap, "publish_hugo", return_value=publish) as ph, \
            patch.object(cap, "git_push") as gp, redirect_stdout(io.StringIO()) as out:
        rc = cap.main()
    return rc, gi, ph, gp, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_public_post_leaks_no_private_data(self):
        home = str(Path.home())
        mail = "kochj23" + "@" + "gmail.com"
        for s in (cap.BODY, cap.TITLE, cap.DESCRIPTION):
            self.assertNotIn(home, s)
            self.assertNotIn(mail, s)
            self.assertIsNone(re.search(r"\b192\.168\.\d+\.\d+\b", s))

    def test_no_push_when_publish_fails(self):
        rc, _, _, gp, _ = _main(publish=False)
        self.assertEqual(rc, 1)
        gp.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_main_fast_with_mocks(self):
        t0 = time.perf_counter()
        for _ in range(200):
            _main()
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_image_failure_is_non_fatal(self):
        # RETRY GAP: main()/generate_image — one attempt; failure publishes without a cover
        rc, gi, ph, gp, out = _main(image_exc=RuntimeError("flux 503"))
        self.assertEqual(rc, 0)
        self.assertEqual(gi.call_count, 1)
        self.assertIsNone(ph.call_args.kwargs["image_path"])
        self.assertIn("non-fatal", out)


class TestUnit(unittest.TestCase):
    def test_content_constants(self):
        self.assertEqual(cap.SECTION, "operations")
        self.assertIn("operations", cap.TAGS)
        self.assertGreater(len(cap.BODY), 2000)
        self.assertIn("no text", cap.IMAGE_PROMPT)

    def test_body_has_markdown_sections(self):
        self.assertGreaterEqual(len(re.findall(r"^## ", cap.BODY, re.M)), 4)


class TestIntegration(unittest.TestCase):
    def test_reuses_journal_pipeline(self):
        import nova_image_utils
        import nova_journal
        self.assertIn("from nova_journal import publish_hugo, git_push", SRC)
        self.assertIn("from nova_image_utils import generate_image", SRC)
        self.assertTrue(callable(nova_journal.publish_hugo) and callable(nova_image_utils.generate_image))

    def test_image_path_chains_into_publish(self):
        rc, gi, ph, gp, _ = _main(image=("/tmp/cover.png",))
        self.assertEqual(gi.call_args.kwargs["section"], "operations")
        self.assertEqual(ph.call_args.kwargs["image_path"], "/tmp/cover.png")
        gp.assert_called_once_with("operations", cap.TITLE)


class TestFunctional(unittest.TestCase):
    def test_golden_path(self):
        rc, gi, ph, gp, out = _main()
        self.assertEqual(rc, 0)
        kw = ph.call_args.kwargs
        self.assertEqual((kw["title"], kw["section"], kw["tags"]), (cap.TITLE, "operations", cap.TAGS))
        self.assertIn("git pushed", out)

    def test_publish_failure_aborts(self):
        rc, _, _, gp, out = _main(publish=False)
        self.assertIn("ABORT", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_weekly_ops_capstone"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
