#!/usr/bin/env python3
"""Tests for nova_writeas.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_writeas.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wa = _load("wa", SCRIPT)
PASSWORD = "kc-hunter2-very-secret"
TOKEN = "tok-0123456789abcdefghij"


class _Env:
    """Redirects the log and token cache into a tempdir and stubs Keychain + HTTP.
    `responses` is a list of JSON bodies (or exceptions) handed to successive urlopen calls."""
    def __init__(self, responses=(), token=None, keychain_rc=0):
        self.dir = Path(tempfile.mkdtemp()); self.log = self.dir / "w.log"; self.cache = self.dir / ".tok"
        if token:
            self.cache.write_text(token)
        self.calls = []; self.responses = list(responses); self.keychain_rc = keychain_rc
        self.run = MagicMock(side_effect=self._run); self.urlopen = MagicMock(side_effect=self._urlopen)

    def _run(self, argv, **kw):
        self.calls.append(("security", argv))
        return MagicMock(returncode=self.keychain_rc, stdout=PASSWORD + "\n" if self.keychain_rc == 0 else "")

    def _urlopen(self, req, timeout=None):
        self.calls.append(("http", req))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        m = MagicMock(); m.read.return_value = json.dumps(r).encode(); return m

    def __enter__(self):
        self.patches = [patch.object(wa, "LOG_FILE", self.log), patch.object(wa, "TOKEN_CACHE", self.cache),
                        patch.object(wa.subprocess, "run", self.run), patch.object(wa.urllib.request, "urlopen", self.urlopen)]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self.patches:
            p.stop()
        return False

    def logged(self):
        return self.log.read_text() if self.log.exists() else ""

    def http(self):
        return [c[1] for c in self.calls if c[0] == "http"]


def _main(argv, env):
    out = io.StringIO()
    with env, patch.object(sys, "argv", ["nova_writeas.py"] + argv), redirect_stdout(out):
        try:
            wa.main(); rc = 0
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_password_comes_from_keychain_and_never_reaches_log_or_stdout(self):
        env = _Env(responses=[{"data": {"access_token": TOKEN}}, {"data": {"title": "Nova", "url": "u", "public": True}}])
        with env, redirect_stdout(io.StringIO()) as out:
            wa.test_connection()
        self.assertEqual(env.calls[0][1], ["security", "find-generic-password", "-s", "nova-writeas-password", "-w"])
        self.assertNotIn(PASSWORD, out.getvalue()); self.assertNotIn(PASSWORD, env.logged())
        self.assertNotIn(TOKEN, env.logged())                                              # only a 10-char prefix is logged
        self.assertIn(TOKEN[:10], env.logged())

    def test_token_cache_is_owner_only_and_sent_as_a_header(self):
        env = _Env(responses=[{"data": {"access_token": TOKEN}}, {"data": {"posts": []}}])
        with env:
            wa.list_posts()
        self.assertEqual(oct(env.cache.stat().st_mode & 0o777), "0o600")
        self.assertEqual(env.http()[1].get_header("Authorization"), f"Token {TOKEN}")
        self.assertTrue(env.http()[1].full_url.startswith("https://write.as/api/"))


class TestPerformance(unittest.TestCase):
    def test_list_posts_10k_under_bound(self):
        env = _Env(responses=[{"data": {"posts": [{"title": str(i)} for i in range(10_000)]}}], token=TOKEN)
        t0 = time.perf_counter()
        with env:
            posts = wa.list_posts(25)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(posts), 25)

    def test_main_arg_scan_is_linear_in_argv(self):
        env = _Env(responses=[{"data": {"slug": "s"}}], token=TOKEN)
        argv = ["post", "--title", "T", "--body", "B"] + ["junk"] * 10_000
        t0 = time.perf_counter()
        rc, _ = _main(argv, env)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(rc, 0)


class TestRetry(unittest.TestCase):
    def test_api_http_error_is_logged_once_and_reraised(self):
        # RETRY GAP: api_request()/urlopen — one attempt; the error body is logged (truncated) and the HTTPError escapes
        err = urllib.error.HTTPError("https://write.as/api/x", 500, "boom", {}, io.BytesIO(b"E" * 1000))
        env = _Env(responses=[err], token=TOKEN)
        with env:
            with self.assertRaises(urllib.error.HTTPError):
                wa.api_request("GET", "/x")
        self.assertEqual(len(env.http()), 1)
        self.assertIn("API error 500: " + "E" * 300 + "\n", env.logged())

    def test_missing_keychain_item_fails_closed(self):
        # RETRY GAP: get_password()/security — one attempt; absence raises a named error with no value in it
        env = _Env(keychain_rc=44)
        with env:
            with self.assertRaises(RuntimeError) as cm:
                wa.get_token()
        self.assertEqual(str(cm.exception), "nova-writeas-password not in Keychain")
        self.assertEqual(env.http(), [])
        self.assertFalse(env.cache.exists())


class TestUnit(unittest.TestCase):
    def test_fresh_cache_skips_login_and_stale_cache_relogs(self):
        env = _Env(token="cached")
        with env:
            self.assertEqual(wa.get_token(), "cached")
        self.assertEqual(env.calls, [])
        env = _Env(responses=[{"data": {"access_token": "fresh"}}], token="stale")
        old = time.time() - wa.TOKEN_TTL - 60
        os.utime(env.cache, (old, old))
        with env:
            self.assertEqual(wa.get_token(), "fresh")
        self.assertEqual(env.cache.read_text(), "fresh")
        self.assertEqual(json.loads(env.http()[0].data), {"alias": "NovaKoch", "pass": PASSWORD})

    def test_main_usage_and_validation(self):
        rc, out = _main([], _Env()); self.assertEqual(rc, 1); self.assertIn("Usage:", out)
        rc, out = _main(["post", "--title", "T"], _Env()); self.assertEqual(rc, 1); self.assertIn("--title and (--body or --file) required", out)
        rc, out = _main(["bogus"], _Env()); self.assertEqual(rc, 1); self.assertIn("Unknown command: bogus", out)

    def test_log_appends_a_timestamped_line(self):
        env = _Env()
        with env, redirect_stdout(io.StringIO()) as out:
            wa.log("hello"); wa.log("again")
        lines = env.logged().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] hello$")
        self.assertEqual(out.getvalue(), env.logged())


class TestIntegration(unittest.TestCase):
    def test_publish_post_targets_the_collection_with_tags_and_created(self):
        env = _Env(responses=[{"data": {"slug": "my-post"}}], token=TOKEN)
        with env, redirect_stdout(io.StringIO()):
            post = wa.publish_post("Title", "Body", ["dream", "surreal"], created="2026-10-05T00:00:00Z")
        req = env.http()[0]
        self.assertEqual((req.full_url, req.get_method()), ("https://write.as/api/collections/novakoch/posts", "POST"))
        self.assertEqual(json.loads(req.data), {"title": "Title", "body": "Body", "tags": ["dream", "surreal"], "created": "2026-10-05T00:00:00Z"})
        self.assertEqual(post["slug"], "my-post")
        self.assertIn("Published: Title → https://novakoch.writeas.com/my-post", env.logged())

    def test_get_requests_carry_no_body(self):
        env = _Env(responses=[{"data": {"posts": [{"title": "a"}]}}], token=TOKEN)
        with env:
            wa.list_posts()
        req = env.http()[0]
        self.assertEqual((req.get_method(), req.data), ("GET", None))


class TestFunctional(unittest.TestCase):
    def test_golden_path_post_from_file(self):
        md = Path(tempfile.mkdtemp()) / "post.md"; md.write_text("# Hello\n\nbody")
        env = _Env(responses=[{"data": {"access_token": TOKEN}}, {"data": {"slug": "hello"}}])
        rc, out = _main(["post", "--title", "Hello", "--file", str(md), "--tags", "essay, security"], env)
        self.assertEqual(rc, 0)
        body = json.loads(env.http()[1].data)
        self.assertEqual((body["body"], body["tags"]), ("# Hello\n\nbody", ["essay", "security"]))
        self.assertIn("Published: Hello → https://novakoch.writeas.com/hello", out)

    def test_list_prints_date_and_title(self):
        env = _Env(responses=[{"data": {"posts": [{"created": "2026-10-05T01:02:03Z", "title": "A"}, {"title": "B"}]}}], token=TOKEN)
        rc, out = _main(["list", "--limit", "1"], env)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "2026-10-05  A")

    def test_error_path_api_failure_surfaces(self):
        err = urllib.error.HTTPError("u", 401, "nope", {}, io.BytesIO(b"bad token"))
        env = _Env(responses=[err], token=TOKEN)
        with self.assertRaises(urllib.error.HTTPError):
            _main(["post", "--title", "T", "--body", "B"], env)
        self.assertIn("API error 401: bad token", env.logged())


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_bare_run_prints_usage(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_writeas"], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30, env=env)   # no args: usage, offline
        self.assertEqual(r.returncode, 1); self.assertIn("nova_writeas.py post --title", r.stdout)


if __name__ == "__main__":
    unittest.main()
