#!/usr/bin/env python3
"""Tests for nova_fishbowl_channel_discovery.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). Written by Jordan Koch (via Claude).
yt-dlp, PG and post_both are mocked at module load: no network, no Slack."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_fishbowl_channel_discovery.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("fishbowl_discovery_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fd = _load()
# module-level stubs: no yt-dlp, no PG, no Slack
fd.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=OSError("offline: yt-dlp stubbed")))
fd.nova_config = types.SimpleNamespace(post_both=MagicMock(), SLACK_NOTIFY="C_TEST")
fd._db = MagicMock(side_effect=OSError("offline: pg stubbed"))
fd.log = MagicMock()
fd.CHANNELS = [{"key": "a", "url": "https://yt.test/@a"}, {"key": "b", "url": "https://yt.test/@b"}]


class _Cur:
    def __init__(self, cached=(), tracked=("UCa",), candidates=()):
        self.sql = []; self._cached = list(cached); self._tracked = list(tracked); self._cand = list(candidates)
        self._next = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if "SELECT handle_key" in sql:
            self._next = [(k,) for k in self._cached]
        elif "SELECT channel_id FROM fishbowl_tracked" in sql:
            self._next = [(c,) for c in self._tracked]
        elif "FROM fishbowl_commenters" in sql:
            self._next = self._cand

    def fetchall(self):
        return self._next

    def close(self): pass


def _proc(out):
    return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", fd.DSN)

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(\s*f[\"']")
        self.assertIn("channel_id = ANY(%s)", SRC)

    def test_ytdlp_is_argv_not_shell(self):
        self.assertNotIn("shell=True", SRC)
        rec = MagicMock(return_value=_proc("UCx\n"))
        with patch.object(fd.subprocess, "run", rec):
            fd.resolve_channel_id("https://yt.test/@x; touch pwned")
        self.assertEqual(rec.call_args[0][0][-1], "https://yt.test/@x; touch pwned")   # one argv element


class TestPerformance(unittest.TestCase):
    def test_looks_relevant_10k(self):
        titles = ["cooking with grandma"] * 50
        t0 = time.perf_counter()
        for _ in range(10_000):
            fd.looks_relevant(titles)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertLessEqual(fd.MAX_CANDIDATES_PER_RUN, 50)   # per-run work is bounded


class TestRetry(unittest.TestCase):
    def test_resolve_failure_returns_none(self):
        # RETRY GAP: resolve_channel_id — one yt-dlp attempt; failure returns None
        m = MagicMock(side_effect=subprocess.TimeoutExpired("yt-dlp", 60))
        with patch.object(fd.subprocess, "run", m):
            self.assertIsNone(fd.resolve_channel_id("x"))
        self.assertEqual(m.call_count, 1)

    def test_channel_info_failure_returns_empty(self):
        # RETRY GAP: channel_info — one attempt; failure -> (None, []) and the digest still posts
        self.assertEqual(fd.channel_info("UCz"), (None, []))

    def test_unresolved_tracked_channel_is_not_cached(self):
        cur = _Cur(cached=["a"])
        with patch.object(fd, "resolve_channel_id", return_value=None) as r:
            fd.tracked_channel_ids(cur)
        r.assert_called_once_with("https://yt.test/@b")
        self.assertFalse(any(s.startswith("INSERT") for s, _ in cur.sql))


class TestUnit(unittest.TestCase):
    def test_looks_relevant(self):
        self.assertTrue(fd.looks_relevant(["My new ROLEX Daytona"]))
        self.assertFalse(fd.looks_relevant([]))
        self.assertFalse(fd.looks_relevant(["baking bread"]))

    def test_channel_info_parses(self):
        with patch.object(fd.subprocess, "run", return_value=_proc("Uzi Watches\tVid 1\nUzi Watches\tVid 2\n\n")):
            self.assertEqual(fd.channel_info("UCu"), ("Uzi Watches", ["Vid 1", "Vid 2"]))
        with patch.object(fd.subprocess, "run", return_value=_proc("")):
            self.assertEqual(fd.channel_info("UCu"), (None, []))

    def test_resolve_takes_first_line(self):
        with patch.object(fd.subprocess, "run", return_value=_proc("UC123\nUC999\n")):
            self.assertEqual(fd.resolve_channel_id("x"), "UC123")


class TestIntegration(unittest.TestCase):
    def test_tracked_list_comes_from_ingest_watch(self):
        self.assertIn("from nova_yt_ingest_watch import CHANNELS", SRC)

    def test_tracked_ids_resolve_and_cache(self):
        cur = _Cur(cached=["a"], tracked=["UCa", "UCb"])
        with patch.object(fd, "resolve_channel_id", return_value="UCb"):
            ids = fd.tracked_channel_ids(cur)
        self.assertEqual(ids, {"UCa", "UCb"})
        ins = [p for s, p in cur.sql if s.startswith("INSERT")]
        self.assertEqual(ins, [("b", "UCb")])


class TestFunctional(unittest.TestCase):
    def test_digest_posted_and_marked_reviewed(self):
        cand = [("UCq", "Quinn", 12, 2, 40.0, ["watchnicholas"])]
        cur = _Cur(cached=["a", "b"], candidates=cand)
        conn = MagicMock(); conn.cursor.return_value = cur
        fd.nova_config.post_both.reset_mock()
        with patch.object(fd, "_db", return_value=conn), \
             patch.object(fd, "channel_info", return_value=("Quinn Times", ["vintage watch haul"])):
            fd.main()
        text = fd.nova_config.post_both.call_args[0][0]
        self.assertIn("Quinn Times", text)
        self.assertIn("2 superchat(s) ($40)", text)
        self.assertIn("looks relevant", text)
        upd = [p for s, p in cur.sql if s.startswith("UPDATE fishbowl_commenters")]
        self.assertEqual(upd, [(["UCq"],)])

    def test_no_candidates_posts_nothing(self):
        cur = _Cur(cached=["a", "b"]); conn = MagicMock(); conn.cursor.return_value = cur
        fd.nova_config.post_both.reset_mock()
        with patch.object(fd, "_db", return_value=conn):
            fd.main()
        fd.nova_config.post_both.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help/--selftest: running the script goes straight to PG + yt-dlp, so import is the smoke test
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_fishbowl_channel_discovery; print('ok')"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
