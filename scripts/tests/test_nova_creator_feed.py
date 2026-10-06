#!/usr/bin/env python3
"""Tests for nova_creator_feed.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cf = _load("creator_feed_t", SCRIPTS / "nova_creator_feed.py")
SRC = (SCRIPTS / "nova_creator_feed.py").read_text()
NOTIFY = MagicMock()
FAKE_NOTIFY_MOD = types.SimpleNamespace(notify=NOTIFY)


class _Cur:
    def __init__(self, rows=()):
        self.rows = list(rows); self.sql = []; self.params = []

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params)

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows=()):
        self.cur = _Cur(rows); self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def U(i):
    return {"video_id": f"v{i}", "title": f"t{i}", "url": f"https://n.example/{i}", "published_at": None}


def _process(conn, uploads, **kw):
    NOTIFY.reset_mock()
    with patch.dict(sys.modules, {"nova_notify": FAKE_NOTIFY_MOD}):
        return cf.process_creator(conn, "nebula", "Creator", uploads, **kw)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_param_sql(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))

    def test_secrets_from_keychain(self):
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="s3cret\n")) as r:
            self.assertEqual(cf.keychain("nova-nebula"), "s3cret")
        self.assertEqual(r.call_args.args[0][:2], ["security", "find-generic-password"])

    def test_cookie_file_filtered_to_target_domain(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.txt"
            p.write_text("# Netscape\n.nebula.tv\tTRUE\t/\tTRUE\t0\tsess\tA\n.bank.example\tTRUE\t/\tTRUE\t0\ts\tB\n")
            cf._filter_cookies_to_domain(str(p), "https://nebula.tv/videos")
            txt = p.read_text()
        self.assertIn("nebula.tv", txt)
        self.assertNotIn("bank.example", txt)


class TestPerformance(unittest.TestCase):
    def test_select_new_10k(self):
        ups = [U(i % 5000) for i in range(10_000)]
        t0 = time.perf_counter()
        out = cf.select_new(ups, {f"v{i}" for i in range(2500)})
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(out), 2500)


class TestRetry(unittest.TestCase):
    def test_keychain_falls_back_to_service_only_lookup(self):
        r = MagicMock(side_effect=[SimpleNamespace(returncode=44, stdout=""), SimpleNamespace(returncode=0, stdout="k\n")])
        with patch("subprocess.run", r):
            self.assertEqual(cf.keychain("svc"), "k")
        self.assertEqual(r.call_count, 2)
        self.assertNotIn("-a", r.call_args.args[0])

    def test_keychain_and_cookies_fail_open(self):
        # RETRY GAP: keychain / refresh_browser_cookies — no backoff; any failure returns None, never raises
        with patch("subprocess.run", side_effect=OSError("boom")):
            self.assertIsNone(cf.keychain("svc"))
        with tempfile.TemporaryDirectory() as d, patch("subprocess.run", side_effect=OSError("boom")):
            self.assertIsNone(cf.refresh_browser_cookies("safari", "https://x.example", Path(d) / "c.txt"))
        self.assertIsNone(cf.cookie_opener("/nonexistent/cookies.txt"))


class TestUnit(unittest.TestCase):
    def test_select_new_edges(self):
        self.assertEqual(cf.select_new([], set()), [])
        self.assertEqual(cf.select_new([{"video_id": None}, {}, U(1), U(1)], set()), [U(1)])
        self.assertEqual(cf.select_new([U(1), U(2)], {"v1"}), [U(2)])

    def test_record_seen_noop_on_empty(self):
        c = _Conn()
        cf.record_seen(c, "nebula", "x", [])
        self.assertEqual((c.cur.sql, c.commits), ([], 0))

    def test_fresh_cookie_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as d, patch("subprocess.run") as r:
            p = Path(d) / "c.txt"; p.write_text("#")
            self.assertEqual(cf.refresh_browser_cookies("safari", "https://x.example", p), str(p))
        r.assert_not_called()


class TestIntegration(unittest.TestCase):
    def test_seen_ids_and_record_use_creator_feed_seen(self):
        c = _Conn(rows=[("v1",), ("v2",)])
        self.assertEqual(cf.seen_ids(c, "nebula"), {"v1", "v2"})
        cf.record_seen(c, "nebula", "Cr", [U(3)])
        self.assertIn("INSERT INTO creator_feed_seen", c.cur.sql[-1])
        self.assertEqual(c.cur.params[-1][:3], ("nebula", "v3", "Cr"))
        self.assertEqual(c.commits, 1)


class TestFunctional(unittest.TestCase):
    def test_new_upload_announced_and_recorded(self):
        c = _Conn(rows=[("v1",)])
        new = _process(c, [U(1), U(2)])
        self.assertEqual(new, [U(2)])
        NOTIFY.assert_called_once()
        self.assertIn("Creator posted on Nebula: t2", NOTIFY.call_args.args[0])
        self.assertEqual(NOTIFY.call_args.kwargs["dedup_key"], "creator-feed:nebula:https://n.example/2")

    def test_first_run_seeds_silently(self):
        c = _Conn(rows=[])
        self.assertEqual(len(_process(c, [U(1), U(2)])), 2)
        NOTIFY.assert_not_called()
        self.assertEqual(sum("INSERT" in q for q in c.cur.sql), 2)


class TestFrame(unittest.TestCase):
    def test_import_has_no_side_effects(self):
        r = subprocess.run([sys.executable, "-c", "import nova_creator_feed as m; print(m.DSN.split()[0])"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "host=localhost")


if __name__ == "__main__":
    unittest.main()
