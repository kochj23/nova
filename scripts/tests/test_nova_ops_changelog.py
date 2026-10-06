#!/usr/bin/env python3
"""Tests for nova_ops_changelog.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ops_changelog.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="ops_changelog_"))

import nova_config  # noqa: E402


@contextmanager
def _stubbed(mods):
    """Set sys.modules keys for the duration and restore ONLY those keys afterwards."""
    old = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _wk():
    wk = types.ModuleType("nova_weekly_ops_report")
    wk.DB, wk.MEMDB = "nova_ops", "nova_memories"
    wk.q = MagicMock(return_value=[("row-a",), ("row-b",)])
    wk.call_llm = MagicMock(return_value="")
    wk.log = MagicMock()
    wk._sanitize = lambda s: s.replace("192.168.1.99", "[ip]")
    wk.fmt = lambda d: "memory context"
    wk.gather = lambda: {}
    return wk


def _load():
    wrap = types.ModuleType("nova_weekly_ops_wrap")
    wrap.articles_this_week = lambda: "articles"; wrap.action_ledger = lambda: "ledger"
    with _stubbed({"nova_weekly_ops_report": _wk(), "nova_weekly_ops_wrap": wrap}):
        spec = importlib.util.spec_from_file_location("ops_changelog", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    mod.HUGO = TMP / "nova-journal"
    mod.MD = mod.HUGO / "content" / "operations" / f"{mod.STEM}.md"
    mod.IMG = mod.HUGO / "static" / "images" / "operations" / f"{mod.STEM}.webp"
    mod.nova_voice = types.SimpleNamespace(system_prompt=lambda s, section="": "SYS")   # real one reads PG
    return mod


oc = _load()
BODY = "Release notes! " * 60
HIST = types.ModuleType("nova_article_history"); HIST.recent_articles_context = lambda s: "history"


def _reset():
    oc.wk = _wk()
    oc.MD.parent.mkdir(parents=True, exist_ok=True)
    oc.MD.write_text("old wrap")


def _git(rcs):
    """subprocess.run stand-in keyed by the git verb."""
    calls = []

    def run(cmd, **kw):
        if cmd[3:4] != ["log"]:                 # git_log() reads history; record only the write-side verbs
            calls.append(cmd)
        verb = cmd[3] if cmd[:1] == ["git"] else cmd[0]
        return MagicMock(returncode=rcs.get(verb, 0), stdout="", stderr="conflict" if rcs.get(verb) else "")
    return run, calls


def _main(rcs=None):
    run, calls = _git(rcs or {})
    with _stubbed({"nova_article_history": HIST}), patch.object(oc.subprocess, "run", run), \
         patch.object(oc, "regenerate_cover"), patch.object(nova_config, "post_both") as pb, redirect_stdout(io.StringIO()):
        oc.main()
    return calls, pb


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_has_no_interpolated_values(self):
        for m in re.finditer(r"wk\.q\(wk\.\w+,\s*(f?)\"", SRC):
            self.assertEqual(m.group(1), "", "f-string SQL")

    def test_migration_rows_are_sanitized(self):
        _reset()
        oc.wk.q = MagicMock(return_value=[("moved 192.168.1.99 to primary",)])
        self.assertEqual(oc.migration_timeline(), "moved [ip] to primary")

    def test_title_quotes_cannot_break_frontmatter(self):
        _reset()
        oc.wk.call_llm = MagicMock(side_effect=[BODY, 'Evil "title"\ninjected: true'])
        _main()
        fm = oc.MD.read_text().split("---")[1]
        self.assertIn('title: "Evil title', fm)
        self.assertEqual(fm.count('"Evil'), 2)              # title + alt, no stray quote


class TestPerformance(unittest.TestCase):
    def test_git_log_caps_at_60_lines(self):
        out = "\n".join(f"2026-01-01 | c{i}" for i in range(10_000))
        with patch.object(oc.subprocess, "run", return_value=MagicMock(stdout=out)):
            t0 = time.perf_counter()
            r = oc.git_log(TMP, 7, "platform")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(r.count("\n"), 60)


class TestRetry(unittest.TestCase):
    def test_rebase_failure_aborts_push(self):
        # RETRY GAP: main/git pull --rebase — one attempt; a conflict aborts the rebase and never pushes
        _reset()
        oc.wk.call_llm = MagicMock(side_effect=[BODY, "Title"])
        calls, _ = _main({"pull": 1})
        verbs = [c[3] for c in calls if c[0] == "git"]
        self.assertEqual(verbs, ["add", "commit", "pull", "rebase"])
        self.assertNotIn("push", verbs)

    def test_git_log_fails_open(self):
        with patch.object(oc.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 20)):
            self.assertEqual(oc.git_log(TMP, 7, "platform"), "platform: (unavailable)")


class TestUnit(unittest.TestCase):
    def test_gen_title_fallback_and_strip(self):
        oc.wk = _wk()
        self.assertEqual(oc.gen_title("b"), "What Shipped This Week")
        oc.wk.call_llm = MagicMock(return_value=' "Four Days, Four Airwaves" ')
        self.assertEqual(oc.gen_title("b"), "Four Days, Four Airwaves")

    def test_empty_queries_have_placeholders(self):
        oc.wk = _wk(); oc.wk.q = MagicMock(return_value=[])
        self.assertEqual((oc.airwave_timeline(), oc.fleet_hardware(), oc.service_map()),
                         ("(none)", "(no node data)", "(no service data)"))


class TestIntegration(unittest.TestCase):
    def test_queries_hit_the_right_dbs(self):
        oc.wk = _wk()
        oc.airwave_timeline(); oc.fleet_hardware()
        self.assertEqual(oc.wk.q.call_args_list[0].args[0], "nova_memories")
        self.assertIn("FROM memories", oc.wk.q.call_args_list[0].args[1])
        self.assertEqual(oc.wk.q.call_args_list[1].args[0], "nova_ops")
        self.assertIn("FROM node_status", oc.wk.q.call_args_list[1].args[1])


class TestFunctional(unittest.TestCase):
    def test_golden_path_rewrites_in_place_pushes_and_posts(self):
        _reset()
        oc.wk.call_llm = MagicMock(side_effect=[BODY, "What Shipped"])
        calls, pb = _main()
        md = oc.MD.read_text()
        self.assertTrue(md.startswith('---\ntitle: "What Shipped"'))
        self.assertTrue(md.endswith(BODY.strip()))
        self.assertEqual([c[3] for c in calls if c[0] == "git"], ["add", "commit", "pull", "push"])
        self.assertIn(oc.URL, pb.call_args.args[0])

    def test_short_generation_aborts_without_writing(self):
        _reset()
        oc.wk.call_llm = MagicMock(return_value="too short")
        calls, pb = _main()
        self.assertEqual(oc.MD.read_text(), "old wrap")
        self.assertEqual(calls, []); pb.assert_not_called()

    def test_missing_target_post_is_a_noop(self):
        _reset(); oc.MD.unlink()
        with patch.object(oc, "fleet_hardware") as fh:
            oc.main()
        fh.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        code = ("import sys, types\n"
                "wk = types.ModuleType('nova_weekly_ops_report'); wr = types.ModuleType('nova_weekly_ops_wrap')\n"
                "sys.modules.update({'nova_weekly_ops_report': wk, 'nova_weekly_ops_wrap': wr})\n"
                "import nova_ops_changelog as m\nprint(m.URL.startswith('https://nova.digitalnoise.net/operations/'))\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
