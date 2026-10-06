#!/usr/bin/env python3
"""Tests for nova_patreon_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_patreon_watch.py"
SRC = SCRIPT.read_text()

import nova_creator_feed as real_feed  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("npw", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pw = _load()


def _fake_feed(cookies="/tmp/c.txt", opener=None):
    """Replaces the module's `feed` handle so no Safari cookie jar, PG or Slack is touched."""
    f = types.SimpleNamespace(DSN="host=x", refresh_browser_cookies=MagicMock(return_value=cookies),
                              cookie_opener=MagicMock(return_value=opener), ensure_schema=MagicMock(),
                              process_creator=MagicMock(return_value=[]))
    return f


pw.feed = _fake_feed()      # module-level guard; each test installs its own


class _Op:
    """urllib opener stand-in: answers by URL substring, records calls."""
    def __init__(self, routes):
        self.routes = routes; self.urls = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        for key, val in self.routes.items():
            if key in req.full_url:
                if isinstance(val, Exception):
                    raise val
                r = MagicMock(); r.read.return_value = json.dumps(val).encode()
                return r
        raise RuntimeError("unrouted " + req.full_url)


SELF = {"data": {"attributes": {"full_name": "Little Mister"}},
        "included": [{"type": "campaign", "id": "11", "attributes": {"name": "Tech Guy"}},
                     {"type": "user", "id": "9"}, {"type": "campaign", "id": "12", "attributes": {}}]}
POSTS = {"data": [{"id": 1, "attributes": {"title": "Ep 1", "url": "/posts/ep-1", "published_at": "2026-01-01"}},
                  {"id": None, "attributes": {"title": "skip"}},
                  {"id": 2, "attributes": {"url": "https://x.example/p"}}]}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_password(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token|session_id)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"(?i)cookie\s*[:=]\s*['\"][^'\"]{10,}")

    def test_only_patreon_https_api_is_called(self):
        self.assertTrue(pw.API.startswith("https://www.patreon.com/"))
        op = _Op({"current_user": SELF})
        pw.feed = _fake_feed(opener=op)
        pw.session()
        self.assertTrue(all(u.startswith("https://www.patreon.com/api/") for u in op.urls))

    def test_campaign_id_stays_in_its_query_slot(self):
        op = _Op({"/posts?": {"data": []}})
        pw.fetch_recent(op, "11&filter[x]=y")
        self.assertIn("filter[campaign_id]=11&filter[x]=y&sort", op.urls[0])   # documented: ids come from Patreon's own API


class TestPerformance(unittest.TestCase):
    def test_parse_10k_posts_fast(self):
        many = {"data": [{"id": i, "attributes": {"title": f"t{i}", "url": f"/p/{i}"}} for i in range(1, 10_001)]}
        t0 = time.perf_counter()
        out = pw.fetch_recent(_Op({"/posts?": many}), "1")
        self.assertEqual(len(out), 10_000)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_posts_failure_is_one_shot_and_fails_open(self):
        # RETRY GAP: fetch_recent()/_get — one GET, no retry; a failure returns [] and logs to stderr
        op = _Op({"/posts?": OSError("timeout")})
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(pw.fetch_recent(op, "11"), [])
        self.assertEqual(len(op.urls), 1)
        self.assertIn("posts 11 failed", err.getvalue())

    def test_expired_session_fails_open(self):
        # RETRY GAP: session()/_get — a 401 means "no session", returned as (None, []) not raised
        pw.feed = _fake_feed(opener=_Op({"current_user": OSError("401")}))
        self.assertEqual(pw.session(), (None, []))


class TestUnit(unittest.TestCase):
    def test_fetch_recent_normalises_posts(self):
        out = pw.fetch_recent(_Op({"/posts?": POSTS}), "11")
        self.assertEqual(out, [
            {"video_id": "1", "title": "Ep 1", "url": "https://www.patreon.com/posts/ep-1", "published_at": "2026-01-01"},
            {"video_id": "2", "title": "(untitled)", "url": "https://x.example/p", "published_at": None}])

    def test_session_edges(self):
        pw.feed = _fake_feed(cookies=None)
        self.assertEqual(pw.session(), (None, []))
        pw.feed = _fake_feed(opener=None)
        self.assertEqual(pw.session(), (None, []))
        pw.feed = _fake_feed(opener=_Op({"current_user": {"data": {"attributes": {}}}}))
        self.assertEqual(pw.session(), (None, []))

    def test_session_discovers_campaigns(self):
        op = _Op({"current_user": SELF})
        pw.feed = _fake_feed(opener=op)
        got_op, camps = pw.session()
        self.assertIs(got_op, op)
        self.assertEqual(camps, [("11", "Tech Guy"), ("12", "(unknown)")])
        pw.feed.refresh_browser_cookies.assert_called_once_with("safari", "https://www.patreon.com/", pw.COOKIES_FILE)


class TestIntegration(unittest.TestCase):
    def test_uses_shared_creator_feed_helpers(self):
        self.assertIn("import nova_creator_feed as feed", SRC)
        for fn in ("refresh_browser_cookies", "cookie_opener", "ensure_schema", "process_creator"):
            self.assertTrue(callable(getattr(real_feed, fn)), fn)
            self.assertIn(f"feed.{fn}(", SRC)
        self.assertEqual(pw.PLATFORM, "patreon")


class TestFunctional(unittest.TestCase):
    def test_golden_path_processes_each_campaign(self):
        op = _Op({"/posts?": POSTS, "current_user": SELF})   # posts URL also contains "current_user_can_view"
        pw.feed = _fake_feed(opener=op)
        pw.feed.process_creator.side_effect = [[{"video_id": "1"}], []]
        conn = MagicMock()
        out = io.StringIO()
        with patch("psycopg2.connect", return_value=conn), redirect_stdout(out):
            self.assertEqual(pw.main(), 0)
        calls = pw.feed.process_creator.call_args_list
        self.assertEqual([c[0][1:3] for c in calls], [("patreon", "Tech Guy"), ("patreon", "(unknown)")])
        self.assertIn("2 creators checked, 1 new post(s)", out.getvalue())
        conn.close.assert_called_once()

    def test_no_session_is_clean_skip_without_pg(self):
        pw.feed = _fake_feed(cookies=None)
        with patch("psycopg2.connect") as pc, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(pw.main(), 0)
        pc.assert_not_called()
        self.assertIn("no valid Safari session", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        # no --help: a bare run reads the Safari cookie jar, so the smoke is an import
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_patreon_watch"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
