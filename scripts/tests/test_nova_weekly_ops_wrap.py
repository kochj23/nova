#!/usr/bin/env python3
"""Tests for nova_weekly_ops_wrap.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
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
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_weekly_ops_wrap.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="wrap-test-"))
STRUCTURE = "STRUCTURE (~700-1000 words, loose; funny section headers welcome):"


def _wk():
    wk = types.ModuleType("nova_weekly_ops_report")
    wk.DB = "host=stub dbname=nova_ops user=kochj"
    wk.SYSTEM = "You are Nova.\n" + STRUCTURE + "\n- intro"
    wk.q = MagicMock(return_value=[("07-01 | fix | x | did a thing -> ok",), ("07-02 | deploy | y\nz | pushed -> ok",)])
    wk._sanitize = MagicMock(side_effect=lambda s: s.replace("\n", " "))
    wk.log = MagicMock()
    wk.gather = MagicMock(return_value={"k": 1})
    wk.fmt = MagicMock(return_value="INFRA BRIEF")
    wk.call_llm = MagicMock(return_value="word " * 200)
    wk.generate_title = MagicMock(return_value="The Week That Was")
    wk.publish = MagicMock(return_value="https://nova.digitalnoise.net/operations/wrap/")
    return wk


def _load(name, path, wk):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_weekly_ops_report": wk}):
        spec.loader.exec_module(mod)
    return mod


WK = _wk()
ww = _load("weekly_wrap_under_test", SCRIPT, WK)
ww.HUGO_OPS = TMP / "operations"
ww.HUGO_OPS.mkdir(parents=True, exist_ok=True)


def _article(days_ago, slug, title, body_lines):
    d = (date.today() - timedelta(days=days_ago)).isoformat()
    text = "---\n" f'title: "{title}"\n' f"date: {d}\n" "draft: false\n" "tags: [ops]\n" "cover:\n" "  image: x\n" "---\n\n" \
           "# Heading\n\n" + "\n\n".join(body_lines) + "\n"
    (ww.HUGO_OPS / f"{d}-{slug}.md").write_text(text)


def _git(stdout="feat: a\nfix: b\n", raise_for=None):
    calls = []

    def run(cmd, capture_output=False, text=False, timeout=None):
        calls.append(cmd)
        if raise_for and raise_for in cmd[2]:
            raise OSError("no git")
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")
    run.calls = calls
    return run


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_git_is_argv_with_timeout_and_ledger_sql_is_static(self):
        self.assertNotIn("shell=True", SRC)
        run = _git()
        with patch.object(ww.subprocess, "run", run):
            ww.git_commits()
        self.assertTrue(all(c[:2] == ["git", "-C"] and "--since=7.days" in c for c in run.calls))
        WK.q.reset_mock(); ww.action_ledger()
        sql = WK.q.call_args[0][1]
        self.assertNotIn("%s", sql); self.assertIsNone(re.search(r'q\(wk\.DB,\s*f["\']', SRC))
        self.assertIn("FROM claude_actions WHERE ts > now() - interval '7 days'", sql)

    def test_ledger_rows_are_sanitized_before_the_llm_sees_them(self):
        WK._sanitize.reset_mock()
        text = ww.action_ledger()
        self.assertEqual(WK._sanitize.call_count, 2)
        self.assertNotIn("\n", text.split("\n")[1])          # the embedded newline was scrubbed


class TestPerformance(unittest.TestCase):
    def test_articles_and_commits_scale(self):
        for i in range(300):
            _article(i % 6, f"perf{i}", f"T{i}", ["para one " * 20, "para two " * 20, "para three"])
        run = _git("\n".join(f"commit {i}" for i in range(10_000)))
        t0 = time.perf_counter()
        a = ww.articles_this_week()
        with patch.object(ww.subprocess, "run", run):
            g = ww.git_commits()
        self.assertLess(time.perf_counter() - t0, 1.5)
        self.assertEqual(a.count("\n") + 1, 300)
        self.assertEqual(g.count("  · "), 80)                      # capped at 40 per repo
        for f in ww.HUGO_OPS.glob("*-perf*.md"):
            f.unlink()


class TestRetry(unittest.TestCase):
    def test_git_failure_fails_open_per_repo(self):
        # RETRY GAP: git_commits()/subprocess.run — one call per repo; a failing repo is skipped silently
        run = _git(raise_for="nova-journal")
        with patch.object(ww.subprocess, "run", run):
            out = ww.git_commits()
        self.assertEqual(len(run.calls), 2)
        self.assertNotIn("journal (nova.digitalnoise.net)", out); self.assertIn("nova platform (.openclaw): 2 commits", out)
        with patch.object(ww.subprocess, "run", MagicMock(side_effect=OSError("x"))):
            self.assertEqual(ww.git_commits(), "(no commits)")

    def test_short_llm_output_aborts_without_publishing(self):
        # RETRY GAP: main()/wk.call_llm — a single generation; a thin result aborts (no retry, no publish)
        WK.call_llm.reset_mock(); WK.publish.reset_mock(); WK.log.reset_mock()
        WK.call_llm.return_value = "too short"
        with patch.object(ww.subprocess, "run", _git()), redirect_stdout(io.StringIO()):
            ww.main()
        WK.call_llm.return_value = "word " * 200
        self.assertEqual(WK.call_llm.call_count, 1); self.assertFalse(WK.publish.called)
        self.assertIn("aborting", WK.log.call_args[0][0])


class TestUnit(unittest.TestCase):
    def test_articles_this_week_filters_and_truncates(self):
        for f in ww.HUGO_OPS.glob("*.md"):
            f.unlink()
        self.assertEqual(ww.articles_this_week(), "(no operations articles found this week)")
        _article(0, "fresh", "Fresh Title", ["First real paragraph.", "Second real paragraph.", "Third ignored."])
        _article(8, "old", "Old Title", ["stale"])
        _article(3, "long", "Long", ["x" * 400])
        out = ww.articles_this_week().splitlines()
        self.assertEqual(len(out), 2)
        self.assertTrue(any('"Fresh Title": First real paragraph. Second real paragraph.' in l for l in out))
        self.assertFalse(any("Third ignored" in l or "Old Title" in l for l in out))
        self.assertTrue(all(len(l.split(": ", 1)[1]) <= 280 for l in out))
        for f in ww.HUGO_OPS.glob("*.md"):
            f.unlink()

    def test_action_ledger_empty(self):
        WK.q.return_value = []
        self.assertEqual(ww.action_ledger(), "(no logged actions)")
        WK.q.return_value = [("07-01 | fix | x | did a thing -> ok",), ("07-02 | deploy | y\nz | pushed -> ok",)]


class TestIntegration(unittest.TestCase):
    def test_reuses_weekly_report_helpers_instead_of_reimplementing(self):
        self.assertIn("import nova_weekly_ops_report as wk", SRC)
        for name in ("def gather", "def fmt", "def publish", "def call_llm", "def _sanitize", "def q("):
            self.assertNotIn(name, SRC)
        self.assertIn("This is a SPECIAL, LONGER week-in-review", ww.SYSTEM)
        self.assertNotIn(STRUCTURE, ww.SYSTEM)                         # the loose structure line was swapped out
        self.assertIn("STRUCTURE (~1200-1800 words", ww.SYSTEM)
        self.assertTrue(ww.SYSTEM.startswith("You are Nova."))        # the rest of wk.SYSTEM is preserved

    def test_ledger_query_targets_the_ops_db(self):
        WK.q.reset_mock(); ww.action_ledger()
        self.assertEqual(WK.q.call_args[0][0], WK.DB)


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_and_prints_url(self):
        for m in (WK.call_llm, WK.publish, WK.generate_title, WK.gather, WK.fmt):
            m.reset_mock()
        _article(1, "g", "Golden", ["Body para.", "More."])
        with patch.object(ww.subprocess, "run", _git()), redirect_stdout(io.StringIO()) as out:
            ww.main()
        self.assertEqual(out.getvalue().strip(), "https://nova.digitalnoise.net/operations/wrap/")
        system, mega = WK.call_llm.call_args[0]
        self.assertEqual(system, ww.SYSTEM); self.assertEqual(WK.call_llm.call_args[1], {"max_tokens": 6500})
        self.assertTrue(mega.startswith("INFRA BRIEF"))
        for header in ("ARTICLES I PUBLISHED THIS WEEK", "DETAILED ACTION LEDGER", "SHIPPED CODE (git commits this week)"):
            self.assertIn(header, mega)
        self.assertIn('"Golden": Body para. More.', mega); self.assertIn("· feat: a", mega)
        WK.publish.assert_called_once_with("The Week That Was", ("word " * 200).strip())
        WK.fmt.assert_called_once_with({"k": 1})
        for f in ww.HUGO_OPS.glob("*.md"):
            f.unlink()

    def test_empty_llm_output_is_an_error_path(self):
        WK.call_llm.return_value = ""; WK.publish.reset_mock()
        with patch.object(ww.subprocess, "run", _git()), redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(ww.main())
        WK.call_llm.return_value = "word " * 200
        self.assertFalse(WK.publish.called); self.assertEqual(out.getvalue(), "")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys, types; from unittest.mock import MagicMock\n"
                "wk = types.ModuleType('nova_weekly_ops_report'); wk.SYSTEM = 'S'; wk.DB = 'd'\n"
                "for n in ('q','_sanitize','log','gather','fmt','call_llm','generate_title','publish'): setattr(wk, n, MagicMock(side_effect=AssertionError(n)))\n"
                "sys.modules['nova_weekly_ops_report'] = wk\n"
                "import nova_weekly_ops_wrap as w; print(w.HUGO_OPS.name)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "operations")


if __name__ == "__main__":
    unittest.main()
