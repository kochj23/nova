#!/usr/bin/env python3
"""Tests for nova_ledger_of_changed_minds.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import datetime as dt
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
SCRIPT = SCRIPTS / "nova_ledger_of_changed_minds.py"
SRC = SCRIPT.read_text()


def _stubs():
    nj = types.ModuleType("nova_journal")
    nj.log = MagicMock(); nj.call_openrouter = MagicMock(return_value="TITLE: Minds, Changed\n\nbody text")
    nj.today_str = MagicMock(return_value="2026-10-05"); nj.get_image_prompt = MagicMock(return_value="prompt")
    nj.generate_image = MagicMock(return_value="/tmp/img.webp"); nj.publish_hugo = MagicMock(return_value=True)
    nj.git_push = MagicMock(); nj.notify_slack = MagicMock()
    nv = types.ModuleType("nova_voice"); nv.system_prompt = MagicMock(side_effect=lambda ctx: "SYS:" + ctx)
    return {"nova_journal": nj, "nova_voice": nv}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stubs()):      # journal/LLM/git/Slack stubbed at import; restored after
        spec.loader.exec_module(mod)
    return mod


lg = _load("lg_mod", SCRIPT)


class _Cur:
    def __init__(self, revisions, new_beliefs=()):
        self.answers = [list(revisions), list(new_beliefs)]; self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))

    def fetchall(self):
        return self.answers.pop(0)


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.autocommit = False

    def cursor(self):
        return self.cur


def _rev(i=1, slug="evidence-slug"):
    return (f"topic {i}", f"old stance {i}", f"new stance {i}", dt.date(2026, 8, 1), dt.date(2026, 9, 20), slug)


def _reset():
    for fn in ("log", "call_openrouter", "get_image_prompt", "generate_image", "publish_hugo", "git_push", "notify_slack"):
        getattr(lg.nj, fn).reset_mock()
    lg.nj.call_openrouter.return_value = "TITLE: Minds, Changed\n\nbody text"
    lg.nj.publish_hugo.return_value = True
    lg.nj.generate_image.side_effect = None


def _main(revisions, new_beliefs=()):
    _reset()
    cur = _Cur(revisions, new_beliefs)
    with patch.object(lg.psycopg2, "connect", return_value=_Conn(cur)):
        rc = lg.main()
    return rc, cur


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", lg.OPS_DSN)

    def test_read_only_and_no_fstring_sql(self):
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", SRC))
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))

    def test_belief_text_goes_to_llm_as_data_not_sql(self):
        rc, cur = _main([_rev(i, slug="x'; select 1; --") for i in range(3)])
        for s in cur.sql:
            self.assertNotIn("select 1", s)
        self.assertIn("x'; select 1; --", lg.nj.call_openrouter.call_args[0][1])


class TestPerformance(unittest.TestCase):
    def test_block_rendering_for_many_revisions_under_bound(self):
        revs = [_rev(i) for i in range(10_000)]
        t0 = time.perf_counter()
        rc, _ = _main(revs, [("t", "s", dt.date(2026, 9, 1))] * 25)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(rc, 0)
        self.assertEqual(lg.nj.call_openrouter.call_args[0][1].count("-> revised"), 10_000)


class TestRetry(unittest.TestCase):
    def test_llm_empty_aborts_rc1_without_publishing(self):
        # RETRY GAP: main()/nj.call_openrouter — one attempt; empty response aborts with rc=1 (monthly task reruns)
        _reset(); lg.nj.call_openrouter.return_value = ""
        cur = _Cur([_rev(i) for i in range(3)])
        with patch.object(lg.psycopg2, "connect", return_value=_Conn(cur)):
            self.assertEqual(lg.main(), 1)
        self.assertEqual(lg.nj.call_openrouter.call_count, 1)
        lg.nj.publish_hugo.assert_not_called()

    def test_image_failure_is_non_fatal(self):
        _reset(); lg.nj.generate_image.side_effect = RuntimeError("sd down")
        cur = _Cur([_rev(i) for i in range(3)])
        with patch.object(lg.psycopg2, "connect", return_value=_Conn(cur)):
            self.assertEqual(lg.main(), 0)
        self.assertIsNone(lg.nj.publish_hugo.call_args[1]["image_path"])
        self.assertTrue(any("image gen failed" in str(c) for c in lg.nj.log.call_args_list))

    def test_pg_failure_propagates(self):
        # RETRY GAP: main()/psycopg2.connect — single attempt, exception escapes to the scheduler
        _reset()
        with patch.object(lg.psycopg2, "connect", side_effect=OSError("pg down")):
            with self.assertRaises(OSError):
                lg.main()
        lg.nj.call_openrouter.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_thin_ledger_skips(self):
        rc, cur = _main([_rev(1), _rev(2)])
        self.assertEqual(rc, 0)
        lg.nj.call_openrouter.assert_not_called(); lg.nj.publish_hugo.assert_not_called()
        self.assertIn("only 2 revision(s)", lg.nj.log.call_args[0][0])
        self.assertEqual(lg.MIN_REVISIONS, 3)

    def test_title_parsing_and_fallback(self):
        _main([_rev(i) for i in range(3)])
        self.assertEqual(lg.nj.publish_hugo.call_args[0][0], "Minds, Changed")
        self.assertEqual(lg.nj.publish_hugo.call_args[0][1], "body text")
        _reset(); lg.nj.call_openrouter.return_value = "no title line here\nmore"
        cur = _Cur([_rev(i) for i in range(3)])
        with patch.object(lg.psycopg2, "connect", return_value=_Conn(cur)):
            lg.main()
        self.assertEqual(lg.nj.publish_hugo.call_args[0][0], "Ledger of Changed Minds — 2026-10-05")

    def test_blocks_render_missing_slug_and_no_new_beliefs(self):
        _main([_rev(i, slug="") for i in range(3)])
        user = lg.nj.call_openrouter.call_args[0][1]
        self.assertIn("(evidence: n/a)", user)
        self.assertIn("--- NEWLY FORMED ---\n(none)", user)


class TestIntegration(unittest.TestCase):
    def test_reads_beliefs_table_from_nova_ops_and_uses_shared_journal_helpers(self):
        rc, cur = _main([])
        self.assertIn("FROM beliefs old JOIN beliefs new ON old.superseded_by = new.id", cur.sql[0])
        self.assertIn("WHERE active AND first_held > now() - interval '31 days'", cur.sql[1])
        self.assertIn("nova_ops", lg.OPS_DSN)
        self.assertIn("import nova_journal as nj", SRC)
        self.assertNotIn("def publish_hugo", SRC)

    def test_voice_prompt_feeds_llm(self):
        _main([_rev(i) for i in range(3)])
        system, user = lg.nj.call_openrouter.call_args[0]
        self.assertTrue(system.startswith("SYS:Write this month's LEDGER OF CHANGED MINDS"))
        self.assertEqual(lg.nj.call_openrouter.call_args[1], {"max_tokens": 2600, "temperature": 0.8})
        self.assertIn("- topic 1: held since 2026-08-01 “old stance 1” -> revised 2026-09-20 “new stance 1” (evidence: evidence-slug)", user)


class TestFunctional(unittest.TestCase):
    def test_golden_path_publishes_pushes_notifies(self):
        rc, cur = _main([_rev(i) for i in range(4)], [("fresh", "stance", dt.date(2026, 9, 25))])
        self.assertEqual(rc, 0)
        lg.nj.generate_image.assert_called_once_with("prompt", width=1024, height=768, section="operations")
        args, kw = lg.nj.publish_hugo.call_args
        self.assertEqual(args[2], "operations"); self.assertIn("opinion-drift", args[3])
        self.assertEqual(kw, {"image_path": "/tmp/img.webp", "emoji": "⚖️"})
        lg.nj.git_push.assert_called_once_with("operations", "Minds, Changed")
        lg.nj.notify_slack.assert_called_once_with("operations", "⚖️ Minds, Changed", "Nova's monthly Ledger of Changed Minds.")
        self.assertIn("PUBLISHED: Minds, Changed", lg.nj.log.call_args[0][0])

    def test_guard_rejection_returns_1_and_never_pushes(self):
        _reset(); lg.nj.publish_hugo.return_value = False
        cur = _Cur([_rev(i) for i in range(3)])
        with patch.object(lg.psycopg2, "connect", return_value=_Conn(cur)):
            self.assertEqual(lg.main(), 1)
        lg.nj.git_push.assert_not_called(); lg.nj.notify_slack.assert_not_called()
        self.assertIn("NOT PUBLISHED", lg.nj.log.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        snippet = ("import sys, types; from unittest.mock import MagicMock\n"
                   "for n in ('nova_journal', 'nova_voice'): sys.modules[n] = types.ModuleType(n)\n"
                   "import psycopg2; psycopg2.connect = MagicMock(side_effect=AssertionError('pg at import'))\n"
                   "import nova_ledger_of_changed_minds as m; assert m.MIN_REVISIONS == 3")
        r = subprocess.run([sys.executable, "-c", snippet], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
