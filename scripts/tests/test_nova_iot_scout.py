#!/usr/bin/env python3
"""Tests for nova_iot_scout.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_iot_scout.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_iot_scout_t", SCRIPTS / "nova_iot_scout.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


iot = _load()
REPO = {"full_name": "acme/esphome-lux", "html_url": "https://github.com/acme/esphome-lux",
        "stargazers_count": 900, "language": "C++", "topics": ["esphome", "esp32"],
        "description": "lux sensor for ESPHome"}
TRENDING_HTML = """<html><article class="Box-row"><a href="/acme/esphome-lux/stargazers">x</a>
 1,234 stars today</article><article class="Box-row"><a href="/foo/llm-thing">y</a> 50 stars today</article>
<article class="Box-row"><a href="/trending/python">z</a></article></html>"""
LLM = "TITLE: \"Tiny Light, Big Opinions\"\nVERDICT: steal\n\nThe body text."


def _cp(rc=0, out="", err=""):
    return mock.Mock(returncode=rc, stdout=out, stderr=err)


class _Cur:
    def __init__(self, seen=()):
        self.sql, self.params, self.seen = [], [], list(seen)

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params)

    def fetchall(self):
        return [(s,) for s in self.seen]


class _NJ(unittest.TestCase):
    """Every nova_journal side effect (log file, Hugo, git, Slack, LLM, images) is mocked per test."""
    def setUp(self):
        self.p = {n: mock.patch.object(iot.nj, n) for n in
                  ("log", "publish_hugo", "git_push", "notify_slack", "generate_image", "get_image_prompt",
                   "call_openrouter")}
        self.m = {n: p.start() for n, p in self.p.items()}
        self.m["call_openrouter"].return_value = LLM
        self.m["generate_image"].return_value = "/tmp/cover.png"
        sp = mock.patch.object(iot.nova_voice, "system_prompt", side_effect=lambda c="", **k: c)  # real one reads PG
        sp.start(); self.addCleanup(sp.stop)
        self.addCleanup(lambda: [p.stop() for p in self.p.values()])


class TestSecurity(_NJ):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"gh[po]_[A-Za-z0-9]{20,}")

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(\s*f[\"']")
        self.assertIn("VALUES (%s,%s,%s,%s,%s,%s)", SRC)

    def test_gh_called_as_argv_list_no_shell(self):
        with mock.patch.object(iot.subprocess, "run", return_value=_cp(0, "{}")) as run:
            iot.gh_repo("evil/repo; echo pwned")
        self.assertEqual(run.call_args[0][0], ["gh", "api", "repos/evil/repo; echo pwned"])
        self.assertNotIn("shell", run.call_args[1])


class TestPerformance(_NJ):
    def test_wheelhouse_filter_10k(self):
        repos = [{"full_name": f"a/r{i}", "description": "a zigbee thing" if i % 2 else "llm", "topics": []}
                 for i in range(10_000)]
        t0 = time.perf_counter()
        hits = sum(iot._in_wheelhouse(r) for r in repos)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(hits, 5_000)


class TestRetry(_NJ):
    def test_gh_and_trending_fail_open(self):
        # RETRY GAP: gh_search / gh_repo / fetch_readme / fetch_trending — one attempt each; safe defaults
        with mock.patch.object(iot.subprocess, "run", side_effect=OSError("no gh")) as run:
            self.assertEqual(iot.gh_search("iot", "a", "b"), [])
            self.assertIsNone(iot.gh_repo("x/y"))
            self.assertEqual(iot.fetch_readme("x/y"), "")
        self.assertEqual(run.call_count, 3)
        with mock.patch.object(iot.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertEqual(iot.fetch_trending(), [])
        self.assertEqual(uo.call_count, 1)

    def test_llm_empty_aborts_without_publishing(self):
        self.m["call_openrouter"].return_value = ""
        conn = mock.Mock(); conn.cursor.return_value = _Cur()
        with mock.patch.object(iot.psycopg2, "connect", return_value=conn), \
                mock.patch.object(iot, "gh_repo", return_value=dict(REPO)), \
                mock.patch.object(iot, "fetch_readme", return_value=""):
            self.assertEqual(iot.run(force_repo="acme/esphome-lux"), 1)
        self.m["publish_hugo"].assert_not_called()


class TestUnit(_NJ):
    def test_trending_parser(self):
        r = mock.MagicMock()
        r.__enter__.return_value.read.return_value = TRENDING_HTML.encode()
        with mock.patch.object(iot.urllib.request, "urlopen", return_value=r):
            self.assertEqual(iot.fetch_trending(), [("acme/esphome-lux", 1234), ("foo/llm-thing", 50)])

    def test_adopted_and_wheelhouse(self):
        self.assertTrue(iot._is_adopted({"full_name": "blakeblackshear/frigate"}))
        self.assertFalse(iot._is_adopted({"full_name": "acme/frigate-addon"}))
        self.assertTrue(iot._in_wheelhouse(REPO))
        self.assertFalse(iot._in_wheelhouse({"full_name": "x/llm", "description": None, "topics": []}))

    def test_evaluate_parses_and_defaults(self):
        self.assertEqual(iot.evaluate(REPO, "readme"), ("Tiny Light, Big Opinions", "STEAL", "The body text."))
        self.m["call_openrouter"].return_value = "VERDICT: MAYBE\nbody"
        t, v, b = iot.evaluate(REPO, "")
        self.assertEqual((v, b), ("WATCH", "body"))
        self.assertIn("esphome-lux", t)


class TestIntegration(_NJ):
    def test_pick_repo_trending_skips_seen_and_off_topic(self):
        repos = {"acme/esphome-lux": dict(REPO), "foo/llm-thing": {"full_name": "foo/llm-thing", "topics": []}}
        cur = _Cur(seen=["old/one"])
        with mock.patch.object(iot, "fetch_trending", return_value=[("foo/llm-thing", 99), ("acme/esphome-lux", 5)]), \
                mock.patch.object(iot, "gh_repo", side_effect=lambda f: repos.get(f)):
            pick = iot.pick_repo(cur)
        self.assertEqual(pick["full_name"], "acme/esphome-lux")
        self.assertEqual(pick["_stars_today"], 5)
        self.assertEqual(cur.sql[0], "SELECT full_name FROM iot_scout_log")

    def test_search_fallback_picks_most_starred_non_adopted(self):
        items = [{"full_name": "a/frigate", "stargazers_count": 99999},
                 {"full_name": "b/zig", "stargazers_count": 500},
                 {"full_name": "c/fork", "stargazers_count": 9000, "fork": True}]
        with mock.patch.object(iot, "gh_search", return_value=items) as gs:
            self.assertEqual(iot._pick_by_search(set())["full_name"], "b/zig")
        self.assertEqual(gs.call_count, len(iot.THEMES))


class TestFunctional(_NJ):
    def _run(self, **kw):
        cur = _Cur()
        conn = mock.Mock(); conn.cursor.return_value = cur
        with mock.patch.object(iot.psycopg2, "connect", return_value=conn), \
                mock.patch.object(iot, "pick_repo", return_value=dict(REPO)), \
                mock.patch.object(iot, "fetch_readme", return_value="# readme"), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            rc = iot.run(**kw)
        return rc, cur, conn

    def test_publish_logs_pushes_and_notifies(self):
        rc, cur, conn = self._run()
        self.assertEqual(rc, 0)
        args = self.m["publish_hugo"].call_args
        self.assertEqual(args[0][:1], ("Tiny Light, Big Opinions",))
        self.assertIn("Verdict: STEAL", args[0][1])
        ins = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO iot_scout_log" in s]
        self.assertEqual(ins[0][0], "acme/esphome-lux")
        self.m["git_push"].assert_called_once()
        self.m["notify_slack"].assert_called_once()
        conn.close.assert_called_once()

    def test_dry_run_writes_but_never_logs_or_pushes(self):
        rc, cur, _ = self._run(dry_run=True)
        self.assertEqual(rc, 0)
        self.m["publish_hugo"].assert_called_once()
        self.assertFalse(any("INSERT" in s for s in cur.sql))
        self.m["git_push"].assert_not_called()
        self.m["notify_slack"].assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_iot_scout"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
