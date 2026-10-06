#!/usr/bin/env python3
"""Tests for nova_fishbowl_daily.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
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
SCRIPT = SCRIPTS / "nova_fishbowl_daily.py"
SRC = SCRIPT.read_text()

import nova_article_history  # noqa: E402  (imported inside main(); pre-import so we can patch it)
import nova_fishbowl_summaries  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("nfd_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fb = _load()
# stub every outbound side effect at module load (LLM, Hugo publish, git push, Slack, image gen)
fb.nj = MagicMock()
fb.nj.today_str.return_value = "2026-01-01"
fb.nova_voice = MagicMock()
fb.nova_voice.system_prompt.side_effect = lambda ctx: "SYS:" + ctx

STREAM = (1, "Betty yelled at the chat again over a $5 superchat", "t",
          {"type": "fishbowl_stream", "url": "https://yt/x", "channel": "Chan", "title": "Ep 1"})


class _Cur:
    def __init__(self, script):
        self.script = list(script); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params)); self._last = self.script.pop(0) if self.script else []

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last


def _conns(cast=(("Betty", "chan", "dossier " * 100),), guests=(), rows=(STREAM,), fresh=3, total=99):
    ops = MagicMock(); ops.cursor.return_value = _Cur([list(cast), list(guests)])
    mem_script = [list(rows)] + ([] if rows else [[]]) + [(fresh,), (total,)]
    mem = MagicMock(); mem.cursor.return_value = _Cur(mem_script)
    return [ops, mem]


class _Base(unittest.TestCase):
    def setUp(self):
        fb.nj.reset_mock(return_value=False, side_effect=False)
        fb.nj.today_str.return_value = "2026-01-01"
        p = patch.object(nova_article_history, "recent_articles_context", return_value="")
        p.start(); self.addCleanup(p.stop)

    def run_main(self, raw="TITLE: Betty Melts Down Over Superchats\n\nbody text", publish=True, **kw):
        fb.nj.call_openrouter.return_value = raw
        fb.nj.publish_hugo.return_value = publish
        conns = _conns(**kw)
        with patch.object(fb.psycopg2, "connect", side_effect=conns):
            rc = fb.main()
        return rc, conns


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", fb.MEM_DSN + fb.OPS_DSN)

    def test_sql_has_no_interpolation(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))

    def test_read_only_against_both_dbs(self):
        self.assertIsNone(re.search(r"\b(INSERT|UPDATE|DELETE)\b", SRC))


class TestPerformance(_Base):
    def test_prompt_is_bounded_with_huge_dossiers(self):
        cast = [(f"p{i}", "c", "x" * 10_000) for i in range(12)]
        rows = [(i, "y" * 10_000, "t", {"type": "fishbowl_stream"}) for i in range(12)]
        t0 = time.perf_counter()
        self.run_main(cast=cast, rows=rows)
        self.assertLess(time.perf_counter() - t0, 1.0)
        user = fb.nj.call_openrouter.call_args[0][1]
        self.assertLess(len(user), 20_000)          # 12x450 + 12x500 + framing, not 240K
        self.assertIn("LIMIT 12", SRC); self.assertIn("LIMIT 8", SRC)


class TestRetry(_Base):
    def test_llm_empty_aborts_without_publish(self):
        # RETRY GAP: main()/nj.call_openrouter — one attempt; empty output aborts with rc=1, nothing published
        rc, _ = self.run_main(raw="")
        self.assertEqual(rc, 1)
        self.assertEqual(fb.nj.call_openrouter.call_count, 1)
        fb.nj.publish_hugo.assert_not_called()

    def test_image_failure_is_non_fatal(self):
        fb.nj.generate_image.side_effect = RuntimeError("gpu busy")
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIsNone(fb.nj.publish_hugo.call_args.kwargs["image_path"])


class TestUnit(_Base):
    def test_degenerate_title_falls_back(self):
        self.run_main(raw="TITLE: Betty. Betty. Betty. Betty. Betty. Betty. Betty. Betty.\nbody")
        self.assertEqual(fb.nj.publish_hugo.call_args[0][0], "The Fishbowl — Daily Dispatch, 2026-01-01")

    def test_missing_title_falls_back_and_body_kept(self):
        self.run_main(raw="just a body with no title line")
        title, body = fb.nj.publish_hugo.call_args[0][:2]
        self.assertTrue(title.startswith("The Fishbowl"))
        self.assertIn("just a body", body)

    def test_good_title_parsed(self):
        self.run_main()
        self.assertEqual(fb.nj.publish_hugo.call_args[0][0], "Betty Melts Down Over Superchats")


class TestIntegration(_Base):
    def test_uses_shared_source_links_and_stable_slug(self):
        self.assertIn("from nova_fishbowl_summaries import source_links", SRC)
        self.run_main()
        args, kw = fb.nj.publish_hugo.call_args
        self.assertEqual(args[2], "opinions")
        self.assertEqual(kw["stable_slug"], "the-fishbowl")
        self.assertEqual(kw["cited_memory_ids"], [1])
        self.assertIn("https://yt/x", args[1])                  # Sources block appended

    def test_falls_back_to_any_fishbowl_memory(self):
        _, conns = self.run_main(rows=())
        sqls = [s for s, _ in conns[1].cursor.return_value.sql]
        self.assertIn("fishbowl_stream", sqls[0])
        self.assertNotIn("fishbowl_stream", sqls[1])


class TestFunctional(_Base):
    def test_golden_path_publishes_pushes_notifies(self):
        rc, _ = self.run_main()
        self.assertEqual(rc, 0)
        fb.nj.git_push.assert_called_once_with("opinions", "Betty Melts Down Over Superchats")
        self.assertEqual(fb.nj.notify_slack.call_args[0][0], "fishbowl")
        self.assertIn("(3)", fb.nova_voice.system_prompt.call_args[0][0])

    def test_guard_rejection_fails_run(self):
        rc, _ = self.run_main(publish=False)
        self.assertEqual(rc, 1)
        fb.nj.git_push.assert_not_called()
        fb.nj.notify_slack.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help: any invocation runs main() against PG + the LLM, so the frame check is an import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_fishbowl_daily"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
