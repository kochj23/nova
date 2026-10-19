#!/usr/bin/env python3
"""Tests for nova_digest_fold.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_digest_fold.py"
SRC = SCRIPT.read_text()
os.environ.setdefault("NOVA_TEST_QUIET", "1")

import nova_digest_fold as f  # noqa: E402

BODY = ("*Burbank · Monday*\n\n# Heading\n\n![img](/x.webp)\n\nFirst real paragraph with words here.\n\n"
        "Second paragraph " + "word " * 300)

FAKE_GEN = '''
import nova_journal as nj
CALLS = []
def notify(*a, **k):
    CALLS.append("notify")
def publish(title, body, image_path=None):
    CALLS.append("own-publish")
def main():
    nj.publish_hugo("Ops Day: Everything Burned", "A body worth folding. " * 20, "operations", [], "d")
    nj.git_push("operations", "x")
    notify("t", "p")
    return 0
def recap():
    publish("Recap Title", "Recap body text here. " * 10, None)
'''


def _conn(rows=None):
    cur = MagicMock()
    cur.fetchall.return_value = rows or []
    cur.__enter__ = lambda s: cur
    cur.__exit__ = lambda s, *a: False
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_secrets(self):
        self.assertIsNone(re.search(r"(password|api_key|token)\s*=\s*['\"][^'\"]{6,}", SRC, re.I))

    def test_sql_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")

    def test_refuses_scripts_outside_scripts_dir(self):
        self.assertEqual(f.run("/etc/passwd"), 2)
        self.assertEqual(f.run("../../bin/evil.py"), 2)


class TestPerformance(unittest.TestCase):
    def test_excerpt_large_body_fast(self):
        big = "\n\n".join("para " * 50 for _ in range(10000))
        t = time.time()
        ex = f.excerpt(big)
        self.assertLess(time.time() - t, 1.0)
        self.assertLessEqual(len(ex.split()), f.EXCERPT_WORDS + 1)


class TestRetry(unittest.TestCase):
    # RETRY GAP: sections_for_digest — one PG attempt; fails open so the digest still publishes
    def test_sections_fail_open(self):
        with patch.object(f, "_pg", side_effect=RuntimeError("pg down")):
            self.assertEqual(f.sections_for_digest(), "")


class TestUnit(unittest.TestCase):
    def test_excerpt_skips_chrome(self):
        ex = f.excerpt(BODY, 20)
        self.assertTrue(ex.startswith("First real paragraph"))
        self.assertNotIn("Burbank", ex)
        self.assertTrue(ex.endswith("…"))

    def test_excerpt_empty(self):
        self.assertEqual(f.excerpt(""), "")

    def test_render_sections(self):
        out = f.render_sections([("nova_copenhagen", "🧯 554 Alerts", BODY), ("nova_x", "Empty", "# only header")])
        self.assertIn("## Folded in from today's desks", out)
        self.assertIn("### Alert review: real vs. noise: 554 Alerts", out)
        self.assertNotIn("Empty", out)
        self.assertEqual(f.render_sections([]), "")

    def test_store_rejects_empty(self):
        self.assertFalse(f.store("g", "", "body"))
        self.assertFalse(f.store("g", "t", "  "))


class TestIntegration(unittest.TestCase):
    def test_store_upserts_into_nova_ops(self):
        conn, cur = _conn()
        with patch.object(f, "_pg", return_value=conn):
            self.assertTrue(f.store("nova_copenhagen", "T", "B", "operations", day="2026-10-19"))
        sql = cur.execute.call_args_list[-1][0][0]
        self.assertIn("INSERT INTO digest_folds", sql)
        self.assertIn("ON CONFLICT (day, generator)", sql)

    def test_digest_calls_fold(self):
        self.assertIn('__import__("nova_digest_fold").sections_for_digest()', (SCRIPTS / "nova_journal.py").read_text())

    def test_sections_read_today(self):
        conn, cur = _conn([("nova_vector_audit", "Memory diary", BODY)])
        with patch.object(f, "_pg", return_value=conn):
            out = f.sections_for_digest("2026-10-19")
        self.assertIn("Inside my memory store", out)
        self.assertEqual(cur.execute.call_args_list[-1][0][1], ("2026-10-19",))


class TestFunctional(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.gen = self.d / "fake_gen.py"
        self.gen.write_text(FAKE_GEN)
        self.stored = []
        self.p1 = patch.object(f, "SCRIPTS_DIR", self.d.resolve())
        self.p2 = patch.object(f, "store", side_effect=lambda *a, **k: self.stored.append(a) or True)
        self.p1.start(); self.p2.start()
        import nova_journal as nj
        self.nj = nj
        self.saved = {k: getattr(nj, k) for k in ("publish_hugo", "git_push", "notify_slack", "generate_image")
                      if hasattr(nj, k)}
        self.argv = list(sys.argv)
        import nova_image_utils
        self.niu, self.niu_gen = nova_image_utils, nova_image_utils.generate_image

    def tearDown(self):
        self.p1.stop(); self.p2.stop()
        for k, v in self.saved.items():
            setattr(self.nj, k, v)
        sys.argv[:] = self.argv
        self.niu.generate_image = self.niu_gen

    def test_main_publish_is_captured_and_stored(self):
        rc = f.run(str(self.gen))
        self.assertEqual(rc, 0)
        self.assertEqual(self.stored[0][0], "fake_gen")
        self.assertEqual(self.stored[0][1], "Ops Day: Everything Burned")
        self.assertEqual(self.stored[0][3], "operations")

    def test_own_publish_function_captured(self):
        self.assertEqual(f.run(str(self.gen), call="recap"), 0)
        self.assertEqual(self.stored[0][1], "Recap Title")

    def test_missing_entry_point(self):
        self.assertEqual(f.run(str(self.gen), call="nope"), 2)
        self.assertEqual(self.stored, [])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, timeout=30,
                           env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
