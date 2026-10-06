#!/usr/bin/env python3
"""Tests for nova_self_improve.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The journal tree, LOG/STATE/LESSONS files live in a tempdir (dated relative to today at call time),
OpenRouter/Ollama/Keychain are mocked, and the module's nova_config is a local proxy."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_self_improve.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


si = _load("nova_self_improve_t", SCRIPT)
si.nova_config = types.SimpleNamespace(post_both=MagicMock())
CRITIQUE = ("# Nova's Writing Lessons (auto-updated weekly)\nLast updated: 1999-01-01\n\n## Dreams\n- Vary endings.\n"
            "## Essays\n- Lead with the claim.\n## Avoid\n- \"something between\"\n- \"a quiet hum\"\n## Other\n- x\n")


def _resp(obj):
    r = MagicMock()
    r.read.return_value = json.dumps(obj).encode()
    return r


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        boom = MagicMock(side_effect=AssertionError("unmocked outbound"))
        for p in (patch.object(si, "LOG_FILE", root / "log.txt"), patch.object(si, "STATE_FILE", root / "state.json"),
                  patch.object(si, "LESSONS_FILE", root / "lessons.md"),
                  patch.object(si, "DREAMS_DIR", root / "dreams"), patch.object(si, "ESSAYS_DIR", root / "essays"),
                  patch.object(si, "OPINIONS_DIR", root / "opinions"),
                  patch("urllib.request.urlopen", boom), patch.object(si.subprocess, "run", boom)):
            p.start()
            self.addCleanup(p.stop)
        for d in ("dreams", "essays", "opinions"):
            (root / d).mkdir()
        si.nova_config.post_both.reset_mock()
        si.nova_config.post_both.side_effect = None
        self.root = root
        self.out = io.StringIO()
        r = redirect_stdout(self.out)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)

    def seed(self):
        today = date.today()
        (self.root / "dreams" / f"{today.isoformat()}.md").write_text("A dream of glass tides.")
        (self.root / "essays" / f"{(today - timedelta(days=2)).isoformat()}-orbits.md").write_text("Essay on orbits.")
        (self.root / "opinions" / f"{(today - timedelta(days=9)).isoformat()}-old.md").write_text("too old")


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('"nova-openrouter-api-key"', SRC)

    def test_missing_keychain_item_raises_not_leaks(self):
        with patch.object(si.subprocess, "run", return_value=subprocess.CompletedProcess([], 44, "", "")):
            with self.assertRaises(RuntimeError):
                si.get_openrouter_key()


class TestPerformance(_Base):
    def test_prompt_build_and_summary_fast(self):
        pieces = [{"date": "d", "file": f"f{i}", "content": "word " * 1000} for i in range(300)]
        big = "\n".join(f"- lesson {i}" for i in range(10_000))
        t0 = time.perf_counter()
        _, user = si.build_critique_prompt(pieces, pieces, pieces)
        si.build_slack_summary("## Avoid\n" + big, 1, 1, 1)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(user.count("--- Essay d:"), 300)


class TestRetry(_Base):
    def test_falls_through_openrouter_then_ollama_chain(self):
        ollama = MagicMock(side_effect=[OSError("busy"), "", CRITIQUE])
        with patch.object(si, "generate_via_openrouter", side_effect=RuntimeError("402")), \
                patch.object(si, "generate_via_ollama", ollama):
            self.assertEqual(si.generate_critique("s", "u"), CRITIQUE)
        self.assertEqual([c.args[2] for c in ollama.call_args_list], [si.OLLAMA_MODEL] + si.FALLBACK_MODELS)

    def test_all_backends_down_returns_none(self):
        with patch.object(si, "generate_via_openrouter", side_effect=RuntimeError("x")), \
                patch.object(si, "generate_via_ollama", side_effect=OSError("y")):
            self.assertIsNone(si.generate_critique("s", "u"))


class TestUnit(_Base):
    def test_past_week_dates(self):
        d = si.get_past_week_dates()
        self.assertEqual((len(d), d[0]), (7, date.today().isoformat()))

    def test_short_critique_rejected(self):
        with patch.object(si, "generate_via_openrouter", return_value="meh"):
            self.assertIsNone(si.generate_critique("s", "u"))

    def test_save_lessons_header_and_date(self):
        si.save_lessons(CRITIQUE)
        text = si.LESSONS_FILE.read_text()
        self.assertIn(f"Last updated: {date.today().isoformat()}", text)
        si.save_lessons("- bare lesson")
        self.assertTrue(si.LESSONS_FILE.read_text().startswith("# Nova's Writing Lessons"))

    def test_slack_summary_avoid_section(self):
        msg = si.build_slack_summary(CRITIQUE, 1, 2, 3)
        self.assertIn('- "something between"', msg)
        self.assertNotIn("- x", msg.split("Top things to stop doing:*")[1])
        self.assertIn("No crutch phrases identified.", si.build_slack_summary("## Dreams\n- a", 0, 0, 0))


class TestIntegration(_Base):
    def test_collectors_window_and_index_skip(self):
        self.seed()
        (self.root / "essays" / "_index.md").write_text("x")
        dates = si.get_past_week_dates()
        self.assertEqual(len(si.collect_dreams(dates)), 1)
        self.assertEqual([e["file"] for e in si.collect_essays(dates)], [f"{dates[2]}-orbits.md"])
        self.assertEqual(si.collect_opinions(dates), [])

    def test_openrouter_request_shape(self):
        with patch.object(si, "get_openrouter_key", return_value="k"), \
                patch("urllib.request.urlopen", return_value=_resp({"choices": [{"message": {"content": " ok "}}]})) as uo:
            self.assertEqual(si.generate_via_openrouter("sys", "usr"), "ok")
        req = uo.call_args.args[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer k")
        self.assertEqual(json.loads(req.data)["model"], si.MODEL)


class TestFunctional(_Base):
    def test_golden_path_saves_lessons_posts_and_records_state(self):
        self.seed()
        with patch.object(si, "generate_critique", return_value=CRITIQUE):
            si.main()
        self.assertTrue(si.LESSONS_FILE.exists())
        msg, kw = si.nova_config.post_both.call_args.args[0], si.nova_config.post_both.call_args.kwargs
        self.assertIn("Reviewed: 1 dreams, 1 essays, 0 opinions", msg)
        self.assertEqual(kw["slack_channel"], si.SLACK_CHANNEL)
        state = json.loads(si.STATE_FILE.read_text())
        self.assertEqual((state["run_count"], state["last_run"]["essays_reviewed"]), (1, 1))

    def test_nothing_written_aborts_without_llm(self):
        with patch.object(si, "generate_critique") as gc:
            si.main()
        gc.assert_not_called()
        si.nova_config.post_both.assert_not_called()
        self.assertIn("ABORT: No writing found", self.out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # running the script calls the LLM and posts to Slack, so the smoke is an import in a child
        r = subprocess.run([sys.executable, "-c", "import nova_self_improve as m; print(m.SLACK_CHANNEL)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), si.SLACK_CHANNEL)
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
