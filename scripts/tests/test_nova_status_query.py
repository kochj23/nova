#!/usr/bin/env python3
"""Tests for nova_status_query.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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

import psycopg2  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_status_query.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nstatusq", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sq = _load()


def _fake_q(dsn, sql):
    if "GROUP BY 1 ORDER BY 2 DESC LIMIT 6" in sql:
        return [("scanner", 40), ("fishbowl", 3)]
    if "DISTINCT target" in sql:
        return [("/a/b/nova_new.py",), ("/x/nova_new.py",), (None,)]
    return [("file_write", 5), ("db_query", 2)]


def _voice_mods(reply):
    nj = types.ModuleType("nova_journal"); nj.call_openrouter = MagicMock(return_value=reply)
    nv = types.ModuleType("nova_voice"); nv.NOVA_VOICE_SHORT = "VOICE"
    return {"nova_journal": nj, "nova_voice": nv}


GIT = MagicMock(stdout="abc1234 **Bold** journal entry title\nabc1235 older\n")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_static_user_text_never_reaches_it(self):
        seen = []
        evil = "what changed today'; DROP TABLE claude_actions;--"
        with patch.object(sq, "_q", side_effect=lambda d, s: seen.append(s) or _fake_q(d, s)), \
             patch.object(sq.subprocess, "run", return_value=GIT):
            sq.answer(evil, voiced=False)
        self.assertTrue(seen)
        self.assertFalse(any("DROP" in s for s in seen))

    def test_voice_cannot_add_facts_beyond_rules(self):
        mods = _voice_mods("x" * 80)
        with patch.dict(sys.modules, mods):
            sq._voice("facts")
        self.assertIn("invent NOTHING", mods["nova_journal"].call_openrouter.call_args[0][0])


class TestPerformance(unittest.TestCase):
    def test_classifier_on_10k_messages(self):
        msgs = ["hey nova can you set a timer for the pasta please"] * 9_999 + ["what changed today?"]
        t0 = time.perf_counter()
        hits = sum(sq.is_status(m) for m in msgs)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(hits, 1)


class TestRetry(unittest.TestCase):
    def test_voice_failure_falls_back_to_raw_facts(self):
        # RETRY GAP: _voice/call_openrouter — one LLM call; failure or too-short reply falls back to the raw facts
        mods = _voice_mods("")
        mods["nova_journal"].call_openrouter.side_effect = RuntimeError("openrouter down")
        with patch.dict(sys.modules, mods), patch.object(sq, "_q", side_effect=_fake_q), \
             patch.object(sq.subprocess, "run", return_value=GIT):
            out = sq.answer("what changed today?")
        self.assertEqual(mods["nova_journal"].call_openrouter.call_count, 1)
        self.assertTrue(out.startswith("*What actually changed today"))

    def test_git_failure_is_swallowed(self):
        with patch.object(sq, "_q", side_effect=_fake_q), patch.object(sq.subprocess, "run", side_effect=OSError("no git")):
            out = sq.answer("status report", voiced=False)
        self.assertNotIn("Journal:", out)


class TestUnit(unittest.TestCase):
    def test_is_status(self):
        for t in ("what changed today?", "Any updates?", "what have you been working on",
                  "look in the ops db", "status update please", "what did you ship"):
            self.assertTrue(sq.is_status(t), t)
        for t in ("", None, "turn on the lights", "play some jazz"):
            self.assertFalse(sq.is_status(t), t)

    def test_non_status_returns_none_without_queries(self):
        with patch.object(sq, "_q") as q:
            self.assertIsNone(sq.answer("turn on the lights"))
        q.assert_not_called()

    def test_short_voice_reply_rejected(self):
        with patch.dict(sys.modules, _voice_mods("too short")):
            self.assertIsNone(sq._voice("facts"))


class TestIntegration(unittest.TestCase):
    def test_reads_actions_and_memories_dbs(self):
        dsns = []
        with patch.object(sq, "_q", side_effect=lambda d, s: dsns.append(d) or _fake_q(d, s)), \
             patch.object(sq.subprocess, "run", return_value=GIT):
            sq.answer("what changed today?", voiced=False)
        self.assertEqual(dsns.count(sq.OPS_DSN), 2)
        self.assertEqual(dsns.count(sq.MEM_DSN), 1)

    def test_q_closes_connection(self):
        conn = MagicMock(); conn.cursor.return_value.fetchall.return_value = [(1,)]
        with patch.object(sq.psycopg2, "connect", return_value=conn):
            self.assertEqual(sq._q("dsn", "SELECT 1"), [(1,)])
        conn.close.assert_called_once()


class TestFunctional(unittest.TestCase):
    def test_grounded_summary(self):
        with patch.object(sq, "_q", side_effect=_fake_q), patch.object(sq.subprocess, "run", return_value=GIT):
            out = sq.answer("what changed today?", voiced=False)
        self.assertIn("• 7 logged actions: 5 file write, 2 db query.", out)
        self.assertIn("New scripts written (1): nova_new.py", out)
        self.assertIn("Journal: 2 commits (latest: Bold journal entry title)", out)
        self.assertIn("scanner (40)", out)

    def test_voiced_reply_used_when_long_enough(self):
        reply = "Fine. Seven things happened, five of them file writes, because of course. " * 2
        with patch.dict(sys.modules, _voice_mods(reply)), patch.object(sq, "_q", side_effect=_fake_q), \
             patch.object(sq.subprocess, "run", return_value=GIT):
            self.assertEqual(sq.answer("what changed today?"), reply.strip())

    def test_pg_down_raises(self):
        with patch.object(sq.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")):
            with self.assertRaises(psycopg2.OperationalError):
                sq.answer("what changed today?", voiced=False)


class TestFrame(unittest.TestCase):
    def test_cli_non_status_question_needs_no_pg(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "turn", "on", "the", "lights"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "(not a status question)")

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(psycopg2, "connect", side_effect=AssertionError("import must not connect")):
            _load()


if __name__ == "__main__":
    unittest.main()
