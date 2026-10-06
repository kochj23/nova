#!/usr/bin/env python3
"""Tests for nova_opinion_fishbowl_roster.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
psycopg2.connect, the LLM (nj.call_openrouter), image gen, Hugo publish, git push and Slack are all mocked."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_opinion_fishbowl_roster.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_opinion_fishbowl_roster_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fr = _load()
import nova_article_history  # noqa: E402

CAST = [("Mookie", "ch1", "a dossier"), ("Curly", "ch2", "another")]
GUESTS = [("Guesty", "ch3", "guest dossier")]


def _pg():
    ops, mem = MagicMock(), MagicMock()
    ops.cursor.return_value.fetchall.side_effect = [CAST, GUESTS]
    mem.cursor.return_value.fetchall.side_effect = [[("tomato snip",)], []]
    return ops, mem


class _Run:
    """Patch every side effect of main(); expose the mocks."""
    def __init__(self, raw="TITLE: \"Report Card\"\n\nbody text", image_exc=None):
        self.raw, self.image_exc = raw, image_exc

    def __enter__(self):
        self.st = ExitStack()
        self.ops, self.mem = _pg()
        self.connect = self.st.enter_context(patch.object(fr.psycopg2, "connect", side_effect=[self.ops, self.mem]))
        p = lambda n, **kw: self.st.enter_context(patch.object(fr.nj, n, **kw))
        self.llm = p("call_openrouter", return_value=self.raw)
        self.prompt = p("get_image_prompt", return_value="ip")
        self.img = p("generate_image", side_effect=self.image_exc, return_value="/tmp/x.webp")
        self.pub = p("publish_hugo")
        self.push = p("git_push")
        self.slack = p("notify_slack")
        self.log = p("log")
        p("today_str", return_value="2026-01-01")
        self.st.enter_context(patch.object(fr.nova_voice, "system_prompt", side_effect=lambda c: "SYS:" + c[:20]))
        self.st.enter_context(patch.object(nova_article_history, "recent_articles_context", return_value=""))
        return self

    def __exit__(self, *a):
        self.st.close()


class TestSecurity(unittest.TestCase):
    def test_no_credentials_and_static_sql(self):
        self.assertIsNone(re.search(r"password\s*=", SRC, re.I))
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))

    def test_jj_is_never_fabricated(self):
        with _Run() as r:
            fr.main()
        user = r.llm.call_args[0][1]
        self.assertIn("ZERO mentions", user)
        self.assertIn("Do not fabricate", user)


class TestPerformance(unittest.TestCase):
    def test_10k_line_llm_output_parses_fast(self):
        raw = "TITLE: T\n" + "\n".join(f"line {i}" for i in range(10_000))
        t0 = time.perf_counter()
        with _Run(raw) as r:
            self.assertEqual(fr.main(), 0)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(r.pub.call_args[0][1].count("\n"), 9_999)


class TestRetry(unittest.TestCase):
    def test_empty_llm_aborts_before_publish(self):
        # RETRY GAP: main()/nj.call_openrouter — single call; empty -> return 1, nothing published
        with _Run(raw="") as r:
            self.assertEqual(fr.main(), 1)
        self.assertEqual(r.llm.call_count, 1)
        r.pub.assert_not_called(); r.push.assert_not_called(); r.slack.assert_not_called()

    def test_image_failure_is_non_fatal(self):
        with _Run(image_exc=RuntimeError("gpu")) as r:
            self.assertEqual(fr.main(), 0)
        self.assertIsNone(r.pub.call_args.kwargs["image_path"])


class TestUnit(unittest.TestCase):
    def test_title_parsed_and_quotes_stripped(self):
        with _Run() as r:
            fr.main()
        self.assertEqual(r.pub.call_args[0][0], "Report Card")
        self.assertEqual(r.pub.call_args[0][1], "body text")

    def test_missing_title_falls_back_to_dated(self):
        with _Run(raw="just a body") as r:
            fr.main()
        self.assertEqual(r.pub.call_args[0][0], "The Fishbowl Roster, Rated — 2026-01-01")

    def test_thin_file_placeholder_when_no_snips(self):
        with _Run() as r:
            fr.main()
        self.assertIn("(no raw mentions found)", r.llm.call_args[0][1])   # Ali-Reza had none


class TestIntegration(unittest.TestCase):
    def test_reads_both_databases(self):
        with _Run() as r:
            fr.main()
        dsns = [c[0][0] for c in r.connect.call_args_list]
        self.assertEqual(dsns, [fr.OPS_DSN, fr.MEM_DSN])
        self.assertIn("fishbowl_people", r.ops.cursor.return_value.execute.call_args_list[0][0][0])
        self.assertIn("source='fishbowl'", r.mem.cursor.return_value.execute.call_args_list[0][0][0])

    def test_uses_shared_voice_and_journal_helpers(self):
        self.assertIn("nova_voice.system_prompt(ctx)", SRC)
        self.assertNotIn("def call_openrouter", SRC)


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_pushes_and_notifies(self):
        with _Run() as r:
            self.assertEqual(fr.main(), 0)
        title, body, section, tags = r.pub.call_args[0][:4]
        self.assertEqual(section, "opinions")
        self.assertIn("fishbowl", tags)
        self.assertIn("### Mookie (ch1)", r.llm.call_args[0][1])
        r.push.assert_called_once_with("opinions", "Report Card")
        self.assertEqual(r.slack.call_args[0][0], "opinions")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_opinion_fishbowl_roster"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
