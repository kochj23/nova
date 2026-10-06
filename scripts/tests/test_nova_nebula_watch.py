#!/usr/bin/env python3
"""Tests for nova_nebula_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_nebula_watch.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nw = _load("nebula_watch_t", SCRIPT)
SRC = SCRIPT.read_text()
URLOPEN = MagicMock(side_effect=RuntimeError("urlopen not mocked in test"))
nw.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=URLOPEN))
FAKE_SECRET = "pw-" + "for-test-only"


def _creds(email="e@example.com", pw=FAKE_SECRET):
    return patch.object(nw.feed, "keychain", side_effect=lambda svc, account="nova": {
        "nova-nebula-email": email, "nova-nebula-password": pw}.get(svc))


def _resp(obj):
    r = MagicMock(); r.read.return_value = json.dumps(obj).encode()
    return r


EPS = {"results": [{"id": 1, "title": "A", "share_url": "https://nebula.tv/videos/a", "published_at": "t"},
                   {"slug": "b-slug", "title": "B"}, {"title": "no id"}] + [{"id": i} for i in range(10, 20)]}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{8,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('feed.keychain("nova-nebula-password")', SRC)

    def test_login_failure_never_logs_secret(self):
        URLOPEN.reset_mock(); URLOPEN.side_effect = OSError("403 cloudflare")
        err = io.StringIO()
        try:
            with _creds(), redirect_stderr(err):
                self.assertIsNone(nw.login())
        finally:
            URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        self.assertNotIn(FAKE_SECRET, err.getvalue())

    def test_only_https_nebula_endpoints(self):
        for u in (nw.AUTH_URL, nw.EPISODES_URL):
            self.assertRegex(u, r"^https://[a-z.]+\.nebula\.app/")


class TestPerformance(unittest.TestCase):
    def test_fetch_recent_caps_and_is_fast(self):
        big = {"results": [{"id": i + 1, "title": str(i)} for i in range(10_000)]}
        t0 = time.perf_counter()
        with patch.object(nw, "_get_json", return_value=big):
            out = nw.fetch_recent("tok", "s")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(out), nw.RECENT)


class TestRetry(unittest.TestCase):
    def test_fetch_failure_fails_open(self):
        # RETRY GAP: fetch_recent — one GET per creator per run; failure returns [] and the run continues
        with patch.object(nw, "_get_json", side_effect=OSError("timeout")) as g, redirect_stderr(io.StringIO()):
            self.assertEqual(nw.fetch_recent("tok", "s"), [])
        self.assertEqual(g.call_count, 1)

    def test_missing_creds_skip_cleanly(self):
        with _creds(email=None), patch("psycopg2.connect") as pc, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nw.main(), 0)
        pc.assert_not_called()
        self.assertIn("not configured", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_fetch_recent_shapes_uploads(self):
        with patch.object(nw, "_get_json", return_value=EPS) as g:
            out = nw.fetch_recent("TOK", "joescott")
        self.assertEqual(g.call_args.args[1], {"Authorization": "Bearer TOK"})
        self.assertEqual(out[0], {"video_id": "joescott:1", "title": "A", "url": "https://nebula.tv/videos/a",
                                  "published_at": "t"})
        self.assertEqual(out[1]["url"], "https://nebula.tv/videos/b-slug")
        self.assertEqual(len(out), nw.RECENT - 1)          # the id-less row is dropped
        with patch.object(nw, "_get_json", return_value={}):
            self.assertEqual(nw.fetch_recent("t", "s"), [])


class TestIntegration(unittest.TestCase):
    def test_login_posts_creds_and_returns_token(self):
        URLOPEN.reset_mock(); URLOPEN.side_effect = None; URLOPEN.return_value = _resp({"token": "BEARER"})
        try:
            with _creds():
                self.assertEqual(nw.login(), "BEARER")
        finally:
            URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        req = URLOPEN.call_args.args[0]
        self.assertEqual(req.full_url, nw.AUTH_URL)
        self.assertEqual(json.loads(req.data)["email"], "e@example.com")

    def test_uses_shared_creator_feed(self):
        self.assertIn("import nova_creator_feed as feed", SRC)
        self.assertNotIn("CREATE TABLE", SRC)                 # schema lives in the shared module


class TestFunctional(unittest.TestCase):
    def test_main_golden_path(self):
        conn = MagicMock()
        seen = []
        def proc(c, platform, name, ups):
            seen.append((platform, name)); return ups[:1]
        with patch.object(nw, "login", return_value="T"), patch("psycopg2.connect", return_value=conn), \
                patch.object(nw.feed, "ensure_schema") as es, patch.object(nw.feed, "process_creator", side_effect=proc), \
                patch.object(nw, "fetch_recent", return_value=[{"video_id": "x"}]), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(nw.main(), 0)
        es.assert_called_once_with(conn)
        self.assertEqual(len(seen), len(nw.CREATORS))
        self.assertTrue(all(p == "nebula" for p, _ in seen))
        self.assertIn(f"{len(nw.CREATORS)} new upload(s)", out.getvalue())
        conn.close.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_nebula_watch as m; print(m.PLATFORM)"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "nebula")


if __name__ == "__main__":
    unittest.main()
