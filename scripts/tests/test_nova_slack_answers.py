#!/usr/bin/env python3
"""Tests for nova_slack_answers.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_slack_answers.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sa = _load("sa", SCRIPT)
SRC = SCRIPT.read_text()
JORDAN = next(iter(sa.HUMANS))


class _Resp:
    """A urlopen() response: context manager + .read() of JSON bytes."""
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Cur:
    """Cursor stub: answers fetchone/fetchall by substring of the last SQL, records every execute."""
    def __init__(self, routes=None):
        self.routes = routes or []; self.sql = []; self.params = []; self._last = ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val() if callable(val) else val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur


def _fake_slack(calls, replies=None, history=None, post=None):
    """urlopen stand-in that dispatches on the Slack method in the URL."""
    def urlopen(req, timeout=None):
        url = req.full_url
        calls.append(url)
        if "conversations.replies" in url:
            return _Resp(replies if replies is not None else {"ok": True, "messages": []})
        if "reactions.get" in url:
            return _Resp({"ok": False, "error": "missing_scope"})
        if "conversations.history" in url:
            return _Resp(history if history is not None else {"ok": True, "messages": []})
        if "chat.postMessage" in url:
            calls.append(json.loads(req.data.decode()))
            return _Resp(post if post is not None else {"ok": True, "ts": "222.2"})
        return _Resp({"ok": False})
    return urlopen


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/_-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("xoxb-", SRC)

    def test_token_comes_from_keychain_or_fleet_store(self):
        self.assertIn("find-generic-password", SRC)
        self.assertIn("nova_secrets.get_secret", SRC)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        # the fallback write carries a hostile answer as a bound parameter, never in the SQL text
        broken = mock.Mock(record_answer=mock.Mock(side_effect=RuntimeError("no pg")))
        evil = "x'); DROP TABLE reflection_questions; --"
        with mock.patch.dict(sys.modules, {"nova_reflection": broken}):
            cur = _Cur(); sa.record_question(cur, "7", evil, dry=False)
        self.assertNotIn("DROP TABLE", cur.sql[0])
        self.assertEqual(cur.params[0], (evil, 7))

    def test_only_allowlisted_humans_can_decide(self):
        msgs = [{"user": "root", "text": "q"}, {"user": "U0GATEWAY", "text": "yes"},
                {"user": "U0STRANGER", "text": "approve"}]
        self.assertIsNone(sa.first_human_reply(msgs))
        self.assertIsNone(sa.reaction_verdict([{"name": "+1", "users": ["U0STRANGER"]}]))
        self.assertEqual(sa.reaction_verdict([{"name": "+1", "users": [JORDAN]}]), "yes")

    def test_coagency_cli_is_invoked_as_argv_not_shell(self):
        with mock.patch.object(sa.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="ok", stderr="")
            sa.decide_proposal("5", "yes", "yes; rm -rf /", dry=False)
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertFalse(kwargs.get("shell", False))
        self.assertIn("--note", args[0])


class TestPerformance(unittest.TestCase):
    def test_verdict_and_reply_scan_fast_on_10k(self):
        texts = ["Yes, go ahead", "nope", "the ex", "All approved", "👍", "keep it", "later maybe"] * 1500
        msgs = [{"user": "root", "text": "root"}] + [{"user": sa.BOT_USER, "text": "noise"}] * 10_000 \
            + [{"user": JORDAN, "text": "Tricia"}]
        t0 = time.perf_counter()
        for t in texts:
            sa.verdict(t)
        found = sa.first_human_reply(msgs)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(found["text"], "Tricia")


class TestRetry(unittest.TestCase):
    # RETRY GAP: slack() — one urlopen, no backoff; every caller must fail open instead.
    def test_read_answer_fails_open_when_slack_is_down(self):
        calls = []

        def boom(req, timeout=None):
            calls.append(1); raise OSError("slack down")
        with mock.patch.object(sa.subprocess, "check_output", return_value=b"tok"), \
             mock.patch.object(sa.urllib.request, "urlopen", boom):
            self.assertEqual(sa.read_answer("C", "1.1"), (None, None))
        self.assertEqual(len(calls), 2)       # replies, then reactions — each tried exactly once

    def test_confirm_and_blanket_swallow_errors(self):
        def boom(req, timeout=None):
            raise OSError("slack down")
        with mock.patch.object(sa.subprocess, "check_output", return_value=b"tok"), \
             mock.patch.object(sa.urllib.request, "urlopen", boom):
            sa.confirm("C", "1.1", "hi", dry=False)              # no exception escapes
            self.assertEqual(sa.blanket_approvals(_Cur(), dry=False), 0)

    # RETRY GAP: decide_proposal() — one subprocess.run of nova_coagency.py; a non-zero rc is reported as False.
    def test_decide_proposal_reports_failure_not_exception(self):
        with mock.patch.object(sa.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="", stderr="boom")):
            self.assertFalse(sa.decide_proposal("5", "no", "no", dry=False))


class TestUnit(unittest.TestCase):
    def test_selftest_passes(self):
        with redirect_stdout(io.StringIO()):
            sa.demo()

    def test_verdict_edge_cases(self):
        self.assertIsNone(sa.verdict(None))
        self.assertIsNone(sa.verdict(""))
        self.assertEqual(sa.verdict("  OK, fine"), "yes")
        self.assertEqual(sa.verdict("Nope."), "no")
        self.assertIsNone(sa.verdict("yesterday was fine"))   # 'yes' must end at a word boundary
        self.assertIsNone(sa.verdict("Nothing yet"))
        self.assertEqual(sa.verdict(":thumbsup:"), "yes")
        self.assertEqual(sa.verdict("don't"), "no")

    def test_blanket_regex_is_top_level_phrase_only(self):
        self.assertTrue(sa.BLANKET_RE.search("All approved, thanks"))
        self.assertTrue(sa.BLANKET_RE.search("approve all"))
        self.assertIsNone(sa.BLANKET_RE.search("yes"))
        self.assertIsNone(sa.BLANKET_RE.search("All of these need work"))

    def test_first_human_reply_skips_root_bots_subtypes_and_blank(self):
        msgs = [{"user": JORDAN, "text": "root is never an answer"},
                {"user": JORDAN, "text": "   "},
                {"user": JORDAN, "bot_id": "B1", "text": "app"},
                {"user": JORDAN, "subtype": "channel_join", "text": "joined"},
                {"user": JORDAN, "text": "real"}]
        self.assertEqual(sa.first_human_reply(msgs)["text"], "real")
        self.assertIsNone(sa.first_human_reply([]))
        self.assertIsNone(sa.first_human_reply([msgs[0]]))

    def test_reaction_verdict_handles_none_and_mixed(self):
        self.assertIsNone(sa.reaction_verdict(None))
        self.assertIsNone(sa.reaction_verdict([]))
        both = [{"name": "+1", "users": [JORDAN]}, {"name": "-1", "users": [JORDAN]}]
        self.assertEqual(sa.reaction_verdict(both), "yes")   # yes wins a tie, deterministically
        self.assertEqual(sa.reaction_verdict([{"name": "thumbsdown", "users": [JORDAN]}]), "no")

    def test_dry_run_paths_write_nothing(self):
        cur = _Cur()
        with redirect_stdout(io.StringIO()) as out:
            sa.record_question(cur, "3", "Tricia", dry=True)
            sa.confirm("C", "1.1", "x", dry=True)
            self.assertTrue(sa.decide_proposal("9", "yes", "yes", dry=True))
        self.assertEqual(cur.sql, [])
        self.assertIn("Q#3 -> 'Tricia'", out.getvalue())
        self.assertIn("proposal #9 -> approve", out.getvalue())


class TestIntegration(unittest.TestCase):
    def test_slack_builds_post_for_chat_and_get_otherwise(self):
        seen = []

        def urlopen(req, timeout=None):
            seen.append(req); return _Resp({"ok": True})
        with mock.patch.object(sa.subprocess, "check_output", return_value=b"tok\n"), \
             mock.patch.object(sa.urllib.request, "urlopen", urlopen):
            sa.slack("chat.postMessage", channel="C", text="hi")
            sa.slack("conversations.replies", channel="C", ts="1.1")
        self.assertEqual(seen[0].get_method(), "POST")
        self.assertEqual(json.loads(seen[0].data.decode())["text"], "hi")
        self.assertEqual(seen[1].get_method(), "GET")
        self.assertIn("conversations.replies?channel=C&ts=1.1", seen[1].full_url)
        self.assertEqual(seen[1].get_header("Authorization"), "Bearer tok")

    def test_record_question_uses_nova_reflection_then_falls_back_to_sql(self):
        rec = mock.Mock()
        with mock.patch.dict(sys.modules, {"nova_reflection": mock.Mock(record_answer=rec)}):
            cur = _Cur(); sa.record_question(cur, "12", "Tricia", dry=False)
        rec.assert_called_once_with(12, "Tricia")
        self.assertEqual(cur.sql, [])
        broken = mock.Mock(record_answer=mock.Mock(side_effect=RuntimeError("no pg")))
        with mock.patch.dict(sys.modules, {"nova_reflection": broken}):
            cur = _Cur(); sa.record_question(cur, "12", "Tricia", dry=False)
        self.assertIn("UPDATE reflection_questions", cur.sql[0])
        self.assertEqual(cur.params[0], ("Tricia", 12))

    def test_read_answer_chains_reply_into_verdict(self):
        replies = {"ok": True, "messages": [{"user": JORDAN, "text": "root"}, {"user": JORDAN, "text": " Yes please "}]}
        with mock.patch.object(sa.subprocess, "check_output", return_value=b"tok"), \
             mock.patch.object(sa.urllib.request, "urlopen", _fake_slack([], replies=replies)):
            self.assertEqual(sa.read_answer("C", "1.1"), ("Yes please", "yes"))

    def test_tables_and_ledgers_named(self):
        for t in ("slack_prompts", "reflection_questions", "coagency_proposals", "directive_conflicts"):
            self.assertIn(t, SRC)


class TestFunctional(unittest.TestCase):
    def _run_main(self, cur, urlopen, argv=("nova_slack_answers.py",)):
        with mock.patch.object(sa.psycopg2, "connect", return_value=_Conn(cur)), \
             mock.patch.object(sa.subprocess, "check_output", return_value=b"tok"), \
             mock.patch.object(sa.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="approved", stderr="")) as run, \
             mock.patch.object(sa.urllib.request, "urlopen", urlopen), \
             mock.patch.object(sa, "POST_HOURS", range(0, 24)), \
             mock.patch.object(sa.sys, "argv", list(argv)), \
             redirect_stdout(io.StringIO()) as out:
            rc = sa.main()
        return rc, out.getvalue(), run

    def test_golden_path_closes_question_and_proposal_and_posts_pending(self):
        cur = _Cur([
            ("SELECT id, kind, ref_id, channel, ts FROM slack_prompts",
             [(1, "question", "7", "C", "111.1"), (2, "proposal", "40", "C", "111.2")]),
            ("SELECT count(*) FROM slack_prompts", (0,)),
            ("FROM coagency_proposals", [(42, "tinker", "restart X", "because it squeaks")]),
        ])
        replies = {"ok": True, "messages": [{"user": JORDAN, "text": "root"}, {"user": JORDAN, "text": "Yes"}]}
        calls = []
        rec = mock.Mock()
        with mock.patch.dict(sys.modules, {"nova_reflection": mock.Mock(record_answer=rec)}):
            rc, out, run = self._run_main(cur, _fake_slack(calls, replies=replies))
        self.assertEqual(rc, 0)
        rec.assert_called_once_with(7, "Yes")
        self.assertIn("--mode", run.call_args[0][0]); self.assertIn("approve", run.call_args[0][0])
        updates = [p for s, p in zip(cur.sql, cur.params) if s.startswith("UPDATE slack_prompts SET resolved_at")]
        self.assertEqual([p[1] for p in updates], [1, 2])
        inserts = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO slack_prompts" in s]
        self.assertEqual(inserts, [("42", sa.CHANNEL, "222.2")])
        posted = [c for c in calls if isinstance(c, dict)]
        self.assertTrue(any("Recorded your answer to Q#7" in c["text"] for c in posted))
        self.assertTrue(any("approved #40" in c["text"] for c in posted))
        self.assertTrue(any(c["text"].startswith("Proposal #42 (tinker)") for c in posted))
        self.assertIn("closed 2 prompt(s)", out)

    def test_dry_run_records_nothing(self):
        cur = _Cur([("SELECT id, kind, ref_id, channel, ts FROM slack_prompts", [(1, "question", "7", "C", "111.1")]),
                    ("SELECT count(*) FROM slack_prompts", (0,)), ("FROM coagency_proposals", [])])
        replies = {"ok": True, "messages": [{"user": JORDAN, "text": "root"}, {"user": JORDAN, "text": "Tricia"}]}
        rc, out, run = self._run_main(cur, _fake_slack([], replies=replies), argv=("x", "--dry-run"))
        self.assertEqual(rc, 0)
        self.assertIn("Q#7 -> 'Tricia'", out)
        self.assertFalse(any(s.startswith("UPDATE") for s in cur.sql))
        run.assert_not_called()

    def test_error_path_slack_down_closes_nothing(self):
        cur = _Cur([("SELECT id, kind, ref_id, channel, ts FROM slack_prompts", [(1, "question", "7", "C", "111.1")]),
                    ("SELECT count(*) FROM slack_prompts", (0,)), ("FROM coagency_proposals", [])])

        def boom(req, timeout=None):
            raise OSError("slack down")
        rc, out, _ = self._run_main(cur, boom)
        self.assertEqual(rc, 0)
        self.assertIn("closed 0 prompt(s)", out)
        self.assertFalse(any(s.startswith("UPDATE") for s in cur.sql))


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all slack-answers assertions passed", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch.object(sa.psycopg2, "connect", side_effect=AssertionError("main ran")):
            _load("sa_again", SCRIPT)


if __name__ == "__main__":
    unittest.main()
