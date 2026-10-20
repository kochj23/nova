#!/usr/bin/env python3
"""7-category tests for the 2026-10-20 Nova Speaks stall: the 2026-10-19 section consolidation moved
article files, queued rows kept the old article_path, every dispatch raised FileNotFoundError and all
4 render hosts auto-disabled (no videos for a day). Covers: scan_new healing moved paths, dispatch
failing the row (not the host) when the article is gone, and copy_to skipping a symlinked self-copy.
Reuses the fakes from test_nova_speaks_sweep. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_speaks_sweep_moved_7cat.py
"""
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_nova_speaks_sweep import Cur, _Base, _host, ss, SRC  # noqa: E402


def _cur_with_row(stored_path):
    cur = Cur()
    orig = cur.execute

    def execute(sql, params=None):
        orig(sql, params)
        if "::timestamptz >=" in sql:
            cur._last = (True,)
        elif "SELECT article_path FROM nova_speaks_renders WHERE slug=%s" in sql:
            cur._last = (stored_path,)
    cur.execute = execute
    return cur


class TestSecurity(_Base):
    def test_heal_update_is_parameterized(self):
        self.assertIn('"UPDATE nova_speaks_renders SET article_path=%s, section=%s, url=%s WHERE slug=%s"', SRC)


class TestPerformance(_Base):
    def test_heal_scan_of_200_moved_rows_is_fast(self):
        for i in range(200):
            self.article(f"a{i}")
        cur = _cur_with_row("/gone/operations/x.md")
        t = time.time()
        with patch.object(ss, "is_live", return_value=True):
            ss.scan_new(cur)
        self.assertLess(time.time() - t, 2.0)
        self.assertEqual(len(cur.writes("UPDATE nova_speaks_renders SET article_path")), 200)


class TestRetry(_Base):
    def test_missing_article_does_not_strike_the_host(self):
        cur = Cur([("status='queued'", [("gone", "operations", "/nope/operations/gone.md", "u")])])
        with patch.object(ss, "sh", return_value=(1, "")):
            ss.dispatch(cur, {"studio": _host()})
        self.assertFalse(cur.writes("UPDATE nova_speaks_hosts"))
        (_, params), = cur.writes("UPDATE nova_speaks_renders SET status='failed'")
        self.assertIn("article missing", params[0])


class TestUnit(_Base):
    def test_moved_article_path_section_and_url_are_healed(self):
        p = self.article("glass-tides")
        cur = _cur_with_row("/old/content/operations/glass-tides.md")
        with patch.object(ss, "is_live", return_value=True):
            self.assertEqual(ss.scan_new(cur), 0)                    # healed, not re-queued
        (_, params), = cur.writes("UPDATE nova_speaks_renders SET article_path")
        self.assertEqual(params, (str(p), "essays", "https://nova.digitalnoise.net/essays/glass-tides/", "glass-tides"))

    def test_existing_path_is_left_alone(self):
        p = self.article("glass-tides")
        cur = _cur_with_row(str(p))
        ss.scan_new(cur)
        self.assertFalse(cur.writes("UPDATE nova_speaks_renders"))


class TestIntegration(_Base):
    def test_copy_to_skips_symlinked_self_copy(self):
        real = Path(self.td.name) / "scripts"; real.mkdir()
        (real / "nova_speaks.py").write_text("x")
        link = Path(self.td.name) / "openclaw-scripts"; os.symlink(real, link)
        ss.copy_to(_host(), str(real / "nova_speaks.py"), str(link / "nova_speaks.py"))   # subprocess.run would raise


class TestFunctional(_Base):
    def test_healed_row_dispatches(self):
        art = self.article()
        cur = Cur([("status='queued'", [("glass-tides", "essays", str(art), "u")])])
        with patch.object(ss, "sh", lambda h, cmd, timeout=60: (1, "") if cmd.startswith("pgrep") else (0, "77\n")), \
             patch.object(ss, "copy_to"), patch.dict(sys.modules, {"nova_satire": None}):
            ss.dispatch(cur, {"studio": _host()})
        self.assertTrue(cur.writes("UPDATE nova_speaks_renders SET status='rendering'"))


class TestFrame(_Base):
    def test_guards_present_in_source(self):
        self.assertIn("os.path.realpath(src) != os.path.realpath(dst)", SRC)
        self.assertIn("if not os.path.exists(art):", SRC)


if __name__ == "__main__":
    unittest.main()
