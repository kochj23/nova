#!/usr/bin/env python3
"""Tests for nova_opinion_fishbowl.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). PG, the LLM, image gen, Hugo publish/git push and Slack are all
mocked. Written by Jordan Koch (via Claude)."""
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
SCRIPT = SCRIPTS / "nova_opinion_fishbowl.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_opinion_fishbowl_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


of = _load()
import nova_config  # noqa: E402


def _nj(raw="TITLE: The Bezel Wars Escalate Again\n\nBody of the column.", published=True):
    return types.SimpleNamespace(
        log=MagicMock(), call_openrouter=MagicMock(return_value=raw), today_str=lambda: "2026-01-01",
        get_image_prompt=MagicMock(return_value="ip"), generate_image=MagicMock(return_value="/tmp/i.webp"),
        publish_hugo=MagicMock(return_value=published), git_push=MagicMock(), notify_slack=MagicMock())


def _run(n_rows=5, nj=None, history=""):
    nj = nj or _nj()
    oc, mc = MagicMock(), MagicMock()
    oc.fetchall.side_effect = [[("Nick", "yt", "the host")], [("Guest", "yt", "a caller")]]
    mc.fetchall.return_value = [(f"stream item {i}", None, {}) for i in range(n_rows)]
    conns = [MagicMock(cursor=MagicMock(return_value=oc)), MagicMock(cursor=MagicMock(return_value=mc))]
    fakes = {"nova_fishbowl_summaries": types.SimpleNamespace(source_links=lambda rows: "- [s](https://x)"),
             "nova_article_history": types.SimpleNamespace(recent_articles_context=lambda s: history)}
    with patch.object(of.psycopg2, "connect", side_effect=conns), patch.object(of, "nj", nj), \
         patch.object(of.nova_voice, "system_prompt", side_effect=lambda c: "SYS:" + c[:40]), \
         patch.object(nova_config, "post_both") as pb, patch.dict(sys.modules, fakes):
        rc = of.main()
    return rc, nj, pb, oc, mc


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")

    def test_self_written_dossiers_excluded_from_feed(self):
        _, _, _, _, mc = _run()
        sql = mc.execute.call_args[0][0]
        self.assertIn("<> 'person_summary'", sql)
        self.assertIn("NOT LIKE '[Fishbowl dossier%'", sql)
        self.assertIn("source='fishbowl'", sql)

    def test_queries_have_no_user_interpolation(self):
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')


class TestPerformance(unittest.TestCase):
    def test_large_feed_bounded_prompt(self):
        nj = _nj()
        t0 = time.perf_counter()
        _run(n_rows=30, nj=nj)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertIn("LIMIT 30", SRC)
        self.assertIn("LIMIT 15", SRC)


class TestRetry(unittest.TestCase):
    def test_llm_empty_aborts_rc1(self):
        # RETRY GAP: main/call_openrouter — one LLM call; empty -> rc 1 so the scheduler retries the run
        rc, nj, _, _, _ = _run(nj=_nj(raw=""))
        self.assertEqual(rc, 1)
        nj.publish_hugo.assert_not_called()

    def test_image_failure_is_non_fatal(self):
        nj = _nj()
        nj.generate_image.side_effect = RuntimeError("swarm down")
        rc, nj, _, _, _ = _run(nj=nj)
        self.assertEqual(rc, 0)
        self.assertIsNone(nj.publish_hugo.call_args.kwargs["image_path"])


class TestUnit(unittest.TestCase):
    def test_degenerate_title_replaced(self):
        rc, nj, _, _, _ = _run(nj=_nj(raw="TITLE: wow wow wow wow wow wow wow wow\n\nbody"))
        self.assertEqual(nj.publish_hugo.call_args[0][0], "The Fishbowl, Reviewed — 2026-01-01")

    def test_missing_title_line(self):
        rc, nj, _, _, _ = _run(nj=_nj(raw="just a body with no title"))
        self.assertEqual(nj.publish_hugo.call_args[0][0], "The Fishbowl, Reviewed — 2026-01-01")
        self.assertTrue(nj.publish_hugo.call_args[0][1].startswith("just a body"))


class TestIntegration(unittest.TestCase):
    def test_dossiers_and_history_reach_prompt(self):
        rc, nj, _, oc, _ = _run(history="RECENT: avoid repeating X")
        user = nj.call_openrouter.call_args[0][1]
        self.assertIn("### Nick (yt)", user)
        self.assertIn("### Guest (yt)", user)
        self.assertIn("RECENT: avoid repeating X", user)
        self.assertIn("FROM fishbowl_people", oc.execute.call_args_list[0][0][0])

    def test_sources_block_appended(self):
        rc, nj, _, _, _ = _run()
        self.assertIn("## Sources — what this column is about", nj.publish_hugo.call_args[0][1])


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_pushes_notifies(self):
        rc, nj, pb, _, _ = _run()
        self.assertEqual(rc, 0)
        self.assertEqual(nj.publish_hugo.call_args[0][:3], ("The Bezel Wars Escalate Again", nj.publish_hugo.call_args[0][1], "opinions"))
        nj.git_push.assert_called_once_with("opinions", "The Bezel Wars Escalate Again")
        nj.notify_slack.assert_called_once()
        pb.assert_not_called()

    def test_thin_feed_suppressed(self):
        rc, nj, pb, _, _ = _run(n_rows=2)
        self.assertEqual(rc, 0)
        nj.call_openrouter.assert_not_called()
        self.assertIn("Suppressed", pb.call_args[0][0])

    def test_quality_guard_rejection_rc1(self):
        rc, nj, _, _, _ = _run(nj=_nj(published=False))
        self.assertEqual(rc, 1)
        nj.notify_slack.assert_not_called()
        nj.git_push.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_opinion_fishbowl; print('ok')"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "ok")


if __name__ == "__main__":
    unittest.main()
