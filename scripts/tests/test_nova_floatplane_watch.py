#!/usr/bin/env python3
"""Tests for nova_floatplane_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_floatplane_watch.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nfw_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fw = _load()
# stub the shared feed helper at module load: no Safari cookie read, no PG, no Slack announce
fw.feed = MagicMock()
fw.feed.DSN = "host=test dbname=x"


def _opener(routes):
    """Fake urllib opener: routes maps URL substring -> payload (or Exception)."""
    op = MagicMock()
    op.calls = []
    def open_(req, timeout=None):
        op.calls.append(req.full_url)
        for k, v in routes.items():
            if k in req.full_url:
                if isinstance(v, Exception):
                    raise v
                return io.BytesIO(json.dumps(v).encode())
        raise OSError("404")
    op.open.side_effect = open_
    return op


POSTS = [{"id": "abc", "title": "New gun", "releaseDate": "2026-01-01"}, {"id": "", "title": "skip"},
         {"id": "def"}]


class _Base(unittest.TestCase):
    def setUp(self):
        fw.feed.reset_mock(return_value=True, side_effect=True)
        fw.feed.DSN = "host=test dbname=x"


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token|sails\.sid)\s*=\s*['\"][A-Za-z0-9+/%]{12,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_https_only_and_no_password_login(self):
        self.assertTrue(fw.BASE.startswith("https://"))
        self.assertNotIn("/auth/login\"", SRC)
        self.assertNotIn("keychain(", SRC)


class TestPerformance(_Base):
    def test_parse_10k_posts_fast(self):
        posts = [{"id": str(i), "title": f"t{i}"} for i in range(10_000)]
        op = _opener({"creator/named": [{"id": "g1"}], "content/creator": posts})
        t0 = time.perf_counter()
        out = fw.fetch_recent(op, "x")
        self.assertEqual(len(out), 10_000)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(_Base):
    def test_resolve_failure_fails_open(self):
        # RETRY GAP: _creator_gid()/fetch_recent() — one HTTP attempt each, failures -> [] (no retry)
        op = _opener({"creator/named": OSError("timeout")})
        self.assertEqual(fw.fetch_recent(op, "x"), [])
        self.assertEqual(len(op.calls), 1)
        op = _opener({"creator/named": [{"id": "g"}], "content/creator": OSError("503")})
        self.assertEqual(fw.fetch_recent(op, "x"), [])

    def test_expired_session_is_none(self):
        fw.feed.refresh_browser_cookies.return_value = "/tmp/c.txt"
        fw.feed.cookie_opener.return_value = _opener({"user/self": OSError("403 notLoggedInError")})
        self.assertIsNone(fw.session())


class TestUnit(_Base):
    def test_fetch_recent_shapes_posts(self):
        op = _opener({"creator/named": [{"id": "g1"}], "content/creator": POSTS})
        out = fw.fetch_recent(op, "ForgottenWeapons")
        self.assertEqual([p["video_id"] for p in out], ["abc", "def"])
        self.assertEqual(out[0]["url"], "https://www.floatplane.com/post/abc")
        self.assertEqual(out[1]["title"], "(untitled)")
        self.assertIn("creatorURL=ForgottenWeapons", op.calls[0])

    def test_creator_gid_dict_or_list(self):
        self.assertEqual(fw._creator_gid(_opener({"named": {"id": "z"}}), "u"), "z")
        self.assertIsNone(fw._creator_gid(_opener({"named": []}), "u"))

    def test_session_requires_cookie_and_username(self):
        fw.feed.refresh_browser_cookies.return_value = None
        self.assertIsNone(fw.session())
        fw.feed.refresh_browser_cookies.return_value = "c"
        fw.feed.cookie_opener.return_value = _opener({"user/self": {"username": None}})
        self.assertIsNone(fw.session())
        op = _opener({"user/self": {"username": "jk"}})
        fw.feed.cookie_opener.return_value = op
        self.assertIs(fw.session(), op)


class TestIntegration(_Base):
    def test_uses_shared_creator_feed(self):
        self.assertIn("import nova_creator_feed as feed", SRC)
        fw.feed.refresh_browser_cookies.return_value = None
        fw.session()
        args = fw.feed.refresh_browser_cookies.call_args[0]
        self.assertEqual(args[:2], ("safari", fw.SELF_URL))


class TestFunctional(_Base):
    def test_main_golden_path(self):
        op = _opener({"creator/named": [{"id": "g1"}], "content/creator": POSTS})
        fw.feed.process_creator.side_effect = lambda conn, plat, name, ups: ups[:1]
        conn = MagicMock()
        import psycopg2
        with patch.object(fw, "session", return_value=op), patch.object(psycopg2, "connect", return_value=conn) as pc:
            self.assertEqual(fw.main(), 0)
        pc.assert_called_once_with("host=test dbname=x")
        fw.feed.ensure_schema.assert_called_once_with(conn)
        self.assertEqual(fw.feed.process_creator.call_count, len(fw.CREATORS))
        self.assertEqual(fw.feed.process_creator.call_args[0][1], "floatplane")
        conn.close.assert_called_once()

    def test_no_session_is_clean_skip(self):
        import psycopg2
        with patch.object(fw, "session", return_value=None), patch.object(psycopg2, "connect") as pc:
            self.assertEqual(fw.main(), 0)
        pc.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # any invocation reads the Safari cookie jar, so the frame check is an import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_floatplane_watch"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
