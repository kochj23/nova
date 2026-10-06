#!/usr/bin/env python3
"""nova_speaks_sweep.sync_journal + origin-tree scan, against real temp git repos (bare origin + clones).
Articles published from nova-core never reached the Studio's ~/nova-journal, so they were never queued.
Written by Jordan Koch (via Claude). PG is a fake cursor; is_live is mocked; no network."""
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("nova_speaks_sweep_sync_t", SCRIPTS / "nova_speaks_sweep.py")
ss = importlib.util.module_from_spec(spec); spec.loader.exec_module(ss)

ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

ARTICLE = '---\ntitle: "{t}"\ndate: 2026-10-05T17:01:49-07:00\ndraft: false\n---\nbody\n'


class Cur:
    def __init__(self, lock_free=True):
        self.lock_free, self.inserts, self.sql, self._r = lock_free, [], [], None

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if "pg_try_advisory_lock" in sql: self._r = (self.lock_free,)
        elif "::timestamptz >=" in sql: self._r = (True,)
        elif sql.startswith("SELECT 1 FROM nova_speaks_renders"): self._r = None
        elif sql.startswith("INSERT"): self.inserts.append(params); self._r = None
        else: self._r = (True,)

    def fetchone(self):
        return self._r


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout


class TestSync(unittest.TestCase):
    def setUp(self):
        for k, v in ENV.items():
            p = patch.dict(os.environ, {k: v}); p.start(); self.addCleanup(p.stop)
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup); root = Path(td.name)
        self.origin = root / "origin.git"; self.core = root / "core"; self.studio = root / "studio"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(root, "clone", "-q", str(self.origin), str(self.core))
        git(self.core, "checkout", "-q", "-b", "main")
        self._publish("operations", "old-post", "Old Post")
        git(root, "clone", "-q", str(self.origin), str(self.studio))
        # nova-core publishes a new article after the Studio's last pull
        self._publish("operations", "2026-10-05-the-heartbeat", "The Heartbeat", cover=True)
        for p in (patch.object(ss, "JOURNAL", self.studio), patch.object(ss, "ORIGIN_CACHE", root / "cache"),
                  patch.object(ss, "is_live", return_value=True)):
            p.start(); self.addCleanup(p.stop)
        self.cache = root / "cache"
        self.out = io.StringIO(); r = redirect_stdout(self.out); r.__enter__(); self.addCleanup(r.__exit__, None, None, None)

    def _publish(self, section, slug, title, cover=False):
        f = self.core / "content" / section / f"{slug}.md"; f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(ARTICLE.format(t=title))
        if cover:
            c = self.core / "static/images" / section / f"{slug}.webp"; c.parent.mkdir(parents=True, exist_ok=True)
            c.write_bytes(b"RIFF\x00webp")
        git(self.core, "add", "-A"); git(self.core, "commit", "-q", "-m", slug); git(self.core, "push", "-q", "origin", "main")

    def test_behind_clean_clone_is_fast_forwarded_and_new_article_queued(self):
        cur = Cur()
        ref = ss.sync_journal(cur)
        self.assertIsNone(ref)
        self.assertTrue((self.studio / "content/operations/2026-10-05-the-heartbeat.md").exists())
        self.assertEqual(git(self.studio, "rev-parse", "HEAD"), git(self.studio, "rev-parse", "origin/main"))
        self.assertTrue(any("pg_advisory_unlock" in s for s in cur.sql))
        n = ss.scan_new(cur, ref)
        slugs = [p[0] for p in cur.inserts]
        self.assertIn("2026-10-05-the-heartbeat", slugs)
        self.assertEqual(n, 2)   # old-post is "new" to the fake DB too

    def test_dirty_clone_untouched_but_article_found_via_origin_tree(self):
        (self.studio / "content/operations/old-post.md").write_text("local edit in progress\n")
        head = git(self.studio, "rev-parse", "HEAD")
        cur = Cur()
        ref = ss.sync_journal(cur)
        self.assertEqual(ref, "origin/main")
        self.assertEqual(git(self.studio, "rev-parse", "HEAD"), head)                 # not merged/reset
        self.assertEqual((self.studio / "content/operations/old-post.md").read_text(), "local edit in progress\n")
        self.assertFalse((self.studio / "content/operations/2026-10-05-the-heartbeat.md").exists())
        self.assertFalse(any("pg_try_advisory_lock" in s for s in cur.sql))
        ss.scan_new(cur, ref)
        row = next(p for p in cur.inserts if p[0] == "2026-10-05-the-heartbeat")
        art = Path(row[2])
        self.assertTrue(art.is_file() and str(art).startswith(str(self.cache)))
        self.assertIn("The Heartbeat", art.read_text())
        self.assertTrue((self.cache / "static/images/operations/2026-10-05-the-heartbeat.webp").exists())
        self.assertIn("left untouched", self.out.getvalue())

    def test_lock_held_skips_merge_and_scans_origin(self):
        head = git(self.studio, "rev-parse", "HEAD")
        ref = ss.sync_journal(Cur(lock_free=False))
        self.assertEqual(ref, "origin/main")
        self.assertEqual(git(self.studio, "rev-parse", "HEAD"), head)

    def test_diverged_clone_is_not_touched(self):
        f = self.studio / "content/operations/local-only.md"; f.write_text(ARTICLE.format(t="Local"))
        git(self.studio, "add", "-A"); git(self.studio, "commit", "-q", "-m", "local")
        head = git(self.studio, "rev-parse", "HEAD")
        self.assertEqual(ss.sync_journal(Cur()), "origin/main")
        self.assertEqual(git(self.studio, "rev-parse", "HEAD"), head)

    def test_up_to_date_returns_none(self):
        git(self.studio, "pull", "-q", "--ff-only")
        self.assertIsNone(ss.sync_journal(Cur()))


if __name__ == "__main__":
    unittest.main()
