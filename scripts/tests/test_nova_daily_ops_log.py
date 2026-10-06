#!/usr/bin/env python3
"""Tests for nova_daily_ops_log.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_daily_ops_log.py").read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("nova_daily_ops_log_t", SCRIPTS / "nova_daily_ops_log.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ops = _load()
ops.LOG = Path(_TMP.name) / "daily_ops_log.log"   # never touch ~/.openclaw/logs
ops.notify = mock.MagicMock()


def _quiet():
    return mock.patch("sys.stdout", new_callable=io.StringIO)


def _cp(rc=0, out="", err=""):
    return mock.Mock(returncode=rc, stdout=out, stderr=err)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_static_and_presence_excluded(self):
        self.assertIsNone(re.search(r"\bq\(\s*\w+,\s*f[\"']", SRC))
        self.assertIsNone(re.search(r"FROM\s+[\w.]*presence", SRC, re.I))

    def test_sanitize_redacts_people_ips_macs_paths(self):
        s = ops._sanitize("Jordans-MacBook 192.168.1.43 aa:bb:cc:dd:ee:ff /etc/shadow Amys-iPhone Office-M4-2 Kitchen Bose")
        for leak in ("Jordans", "192.168.1.43", "aa:bb", "/etc/shadow", "Amys", "Office-M4"):
            self.assertNotIn(leak, s)
        self.assertIn("Kitchen Bose", s)


class TestPerformance(unittest.TestCase):
    def test_sanitize_10k_rows(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            ops._sanitize(f"host-{i} 192.168.1.{i % 255} ok")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_q_is_one_shot_and_fails_open(self):
        # RETRY GAP: q()/psql — one subprocess attempt; non-zero or exception -> []
        with mock.patch.object(ops.subprocess, "run", return_value=_cp(1, err="boom")) as run, _quiet():
            self.assertEqual(ops.q("dsn", "SELECT 1"), [])
        self.assertEqual(run.call_count, 1)
        with mock.patch.object(ops.subprocess, "run", side_effect=OSError("no psql")), _quiet():
            self.assertEqual(ops.q("dsn", "SELECT 1"), [])

    def test_gh_and_ingest_fail_open(self):
        # RETRY GAP: _gh_json / ingest_github_stats — single attempt, safe default
        with mock.patch.object(ops.subprocess, "run", side_effect=OSError("no gh")), _quiet():
            self.assertEqual(ops._gh_json(["x"], default={}), {})
        with mock.patch.object(ops.urllib.request, "urlopen", side_effect=OSError("down")) as uo, _quiet():
            self.assertFalse(ops.ingest_github_stats("s", {"date": "d"}))
        self.assertEqual(uo.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_q_parses_unit_separator_rows(self):
        with mock.patch.object(ops.subprocess, "run", return_value=_cp(0, "a\x1fb\n\nc\x1fd\n")):
            self.assertEqual(ops.q("dsn", "x"), [["a", "b"], ["c", "d"]])
        with mock.patch.object(ops, "q", return_value=[]):
            self.assertEqual(ops.scalar("dsn", "x", default="7"), "7")

    def test_sanitize_none_and_gh_empty_output(self):
        self.assertEqual(ops._sanitize(None), "")
        with mock.patch.object(ops.subprocess, "run", return_value=_cp(0, "  ")):
            self.assertEqual(ops._gh_json(["x"]), [])

    def test_generate_title_strips_quotes(self):
        with mock.patch.object(ops, "call_llm", return_value='  "Ten Thousand \"Logs\""  '):
            self.assertEqual(ops.generate_title("x"), "Ten Thousand Logs")


class TestIntegration(unittest.TestCase):
    def test_llm_and_vector_go_through_shared_helpers(self):
        with mock.patch.object(ops.nova_journal, "call_openrouter", return_value="ok") as co:
            self.assertEqual(ops.call_llm("s", "u", max_tokens=5), "ok")
        co.assert_called_once_with("s", "u", max_tokens=5)
        self.assertEqual(ops.MEMORY_REMEMBER_URL, ops.nova_config.VECTOR_URL)

    def test_github_stats_feed_operations_ingest(self):
        def gh(args, default=None):
            if args[:2] == ["repo", "list"]:
                return [{"name": "A"}, {"name": "B"}]
            if args[0] == "search":
                return [{"title": "t", "repository": {"nameWithOwner": "kochj23/A"}}]
            if args[1].endswith("A/traffic/clones"):
                return {"count": 9, "uniques": 3, "clones": [{"count": 2, "uniques": 1}]}
            if args[1].endswith("A/traffic/views"):
                return {"count": 20, "uniques": 5}
            return {}
        with mock.patch.object(ops, "_gh_json", side_effect=gh):
            summary, st = ops.github_daily_stats()
        self.assertEqual((st["repos_total"], st["repos_with_traffic"], st["repos_traffic_denied"]), (2, 1, 1))
        self.assertEqual((st["clones_14d"], st["clones_recent_day"], st["views_14d"]), (9, 2, 20))
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"{}"
        with mock.patch.object(ops.urllib.request, "urlopen", return_value=resp) as uo, _quiet():
            self.assertTrue(ops.ingest_github_stats(summary, st))
        body = json.loads(uo.call_args[0][0].data)
        self.assertEqual(body["source"], "operations")
        self.assertEqual(body["metadata"]["totals"]["clones_14d"], 9)


class TestFunctional(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.ps = [mock.patch.object(ops, "HUGO_ROOT", root),
                   mock.patch.object(ops, "CONTENT_DIR", root / "content" / "operations"),
                   mock.patch.object(ops, "q", return_value=[]),
                   mock.patch.object(ops, "github_daily_stats", return_value=("gh summary", {})),
                   mock.patch.object(ops, "call_llm", side_effect=["The body of the log.", "A Quiet Day"]),
                   mock.patch.object(ops.nova_voice, "system_prompt", side_effect=lambda s, **k: s),
                   mock.patch.object(ops.nova_journal, "grafana_panel_image", return_value=""),
                   mock.patch.object(ops, "make_cover", return_value=""),
                   mock.patch.object(ops, "notify"),
                   mock.patch.object(ops.subprocess, "run"),
                   mock.patch.object(ops.urllib.request, "urlopen", side_effect=OSError("offline")),
                   _quiet()]
        self.m = [p.start() for p in self.ps]
        self.notify, self.run = self.m[8], self.m[9]
        import nova_article_history
        self.ps.append(mock.patch.object(nova_article_history, "recent_articles_context", return_value=""))
        self.ps[-1].start()
        self.root = root

    def tearDown(self):
        for p in reversed(self.ps):
            p.stop()
        self.td.cleanup()

    def test_main_writes_post_commits_pushes_and_notifies(self):
        self.run.return_value = _cp(0)
        ops.main()
        posts = list((self.root / "content" / "operations").glob("*.md"))
        self.assertEqual(len(posts), 1)
        text = posts[0].read_text()
        self.assertIn('title: "A Quiet Day"', text)
        self.assertIn("The body of the log.", text)
        cmds = [c[0][0][:2] for c in self.run.call_args_list]
        self.assertIn(["git", "push"], cmds)
        self.assertIn("/operations/", self.notify.call_args[1]["meta"]["url"])

    def test_failed_rebase_aborts_without_push(self):
        def run(cmd, **k):
            return _cp(1, err="conflict") if cmd[:2] == ["git", "pull"] else _cp(0)
        self.run.side_effect = run
        ops.main()
        cmds = [c[0][0][:2] for c in self.run.call_args_list]
        self.assertIn(["git", "rebase"], cmds)
        self.assertNotIn(["git", "push"], cmds)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_daily_ops_log"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
