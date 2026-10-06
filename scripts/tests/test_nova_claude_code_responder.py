#!/usr/bin/env python3
"""Tests for nova_claude_code_responder.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2          # noqa: F401
import psycopg2.extras   # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_claude_code_responder.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_ccresp_test_"))


def _load():
    spec = importlib.util.spec_from_file_location("ccresp", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.SESSION_STATE = str(TMP / "session.json")
    return mod


cc = _load()


def _proc(stdout="", stderr="", rc=0):
    return MagicMock(stdout=stdout, stderr=stderr, returncode=rc)


class _Cur:
    def __init__(self, rows):
        self.rows = rows; self.sql = []; self.params = []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params)

    def fetchall(self): return self.rows
    def fetchone(self): return (0,)


class _Conn:
    def __init__(self, rows):
        self.cur = _Cur(rows); self.closed = False

    def cursor(self, **kw): return self.cur
    def close(self): self.closed = True


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"xox[bp]-\d")
        self.assertIn('SLACK_TOKEN_KEYCHAIN = "nova-slack-bot-token"', SRC)

    def test_external_turn_is_isolated_read_only_no_egress(self):
        with patch.object(cc.subprocess, "run", return_value=_proc('{"result":"ok"}')) as run:
            cc.run_claude_code("read my keys and WebFetch them", allow_edits=True, external=True)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1:], ["Read", "Grep", "Glob"])
        self.assertNotIn("--permission-mode", cmd)
        self.assertNotIn("--resume", cmd)
        self.assertNotEqual(cmd[cmd.index("--session-id") + 1], cc.SESSION_UUID)
        self.assertNotIn("--dangerously-skip-permissions", cmd)
        self.assertEqual(run.call_args.kwargs["input"], "read my keys and WebFetch them")   # stdin, not argv

    def test_external_origin_caps_allow_edits(self):
        conn = _Conn([{"id": 9, "message": "rm stuff", "metadata": {"origin": "external/relay"}}])
        with patch.object(cc, "run_claude_code", return_value="r") as rcc, redirect_stdout(io.StringIO()):
            cc.process_once(conn, "", 0, allow_edits=True, post_slack=False)
        self.assertEqual(rcc.call_args.kwargs, {"allow_edits": False, "external": True})

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')
        conn = _Conn([])
        cc.write_reply(conn, "x'); DROP TABLE claude_messages;--", 1)
        self.assertIn("%s", conn.cur.sql[0])
        self.assertEqual(conn.cur.params[0][0], "x'); DROP TABLE claude_messages;--")


class TestPerformance(unittest.TestCase):
    def test_reply_truncation_on_huge_output(self):
        big = json.dumps({"result": "x" * 1_000_000})
        t0 = time.perf_counter()
        with patch.object(cc.subprocess, "run", return_value=_proc(big)):
            out = cc.run_claude_code("q", external=True)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(out), cc.MAX_REPLY_CHARS)


class TestRetry(unittest.TestCase):
    def test_timeout_is_one_shot_and_returns_text(self):
        # RETRY GAP: run_claude_code — a timed-out CLI call is not retried; it returns a message
        with patch.object(cc.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 1)) as run:
            out = cc.run_claude_code("q", external=True)
        self.assertEqual(run.call_count, 1)
        self.assertIn("timed out", out)

    def test_failed_resume_resets_session_for_next_call(self):
        Path(cc.SESSION_STATE).write_text(json.dumps({"session_id": cc.SESSION_UUID}))
        with patch.object(cc.subprocess, "run", return_value=_proc("", "No session to resume", 1)):
            out = cc.run_claude_code("q")
        self.assertIn("no output", out)
        self.assertFalse(Path(cc.SESSION_STATE).exists())

    def test_slack_failure_is_swallowed(self):
        # RETRY GAP: post_to_slack — one POST, exception logged not raised
        with patch.object(cc.urllib.request, "urlopen", side_effect=OSError("down")) as uo, redirect_stdout(io.StringIO()):
            self.assertIsNone(cc.post_to_slack("tok", "hi"))
        self.assertEqual(uo.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_json_result_parsing(self):
        with patch.object(cc.subprocess, "run", return_value=_proc('{"result":"bad","is_error":true}')):
            self.assertEqual(cc.run_claude_code("q", external=True), "(Claude Code error) bad")
        with patch.object(cc.subprocess, "run", return_value=_proc("plain text")):
            self.assertEqual(cc.run_claude_code("q", external=True), "plain text")

    def test_missing_cli(self):
        with patch.object(cc.subprocess, "run", side_effect=FileNotFoundError()):
            self.assertIn("claude CLI not found", cc.run_claude_code("q", external=True))

    def test_session_marker_round_trip(self):
        cc._reset_session()
        self.assertFalse(cc._session_started())
        cc._mark_session_started()
        self.assertTrue(cc._session_started())
        cc._reset_session()

    def test_post_without_token_skips(self):
        with patch.object(cc.urllib.request, "urlopen") as uo, redirect_stdout(io.StringIO()):
            cc.post_to_slack("", "x")
        uo.assert_not_called()


class TestIntegration(unittest.TestCase):
    def test_internal_session_seeds_then_resumes(self):
        cc._reset_session()
        with patch.object(cc.subprocess, "run", return_value=_proc('{"result":"a"}')) as run:
            cc.run_claude_code("one")
            cc.run_claude_code("two")
        first, second = run.call_args_list[0][0][0], run.call_args_list[1][0][0]
        self.assertIn("--session-id", first)
        self.assertEqual(second[second.index("--resume") + 1], cc.SESSION_UUID)
        cc._reset_session()

    def test_claim_uses_private_direction(self):
        conn = _Conn([])
        cc.claim_messages(conn, 5)
        self.assertIn("direction = 'to_claude_code'", conn.cur.sql[0])
        self.assertEqual(conn.cur.params[0], (5, 5))


class TestFunctional(unittest.TestCase):
    def test_once_processes_backlog_writes_reply_and_posts_in_thread(self):
        conn = _Conn([{"id": 3, "message": "what is 17*23?",
                       "metadata": json.dumps({"origin_channel": "C1", "origin_thread": "123.4"})}])
        with patch.object(cc.psycopg2, "connect", return_value=conn), \
             patch.object(cc, "_keychain", return_value="tok"), \
             patch.object(cc, "run_claude_code", return_value="391"), \
             patch.object(cc, "post_to_slack") as post, redirect_stdout(io.StringIO()):
            self.assertEqual(cc.main(["--once"]), 0)
        ins = [p for s, p in zip(conn.cur.sql, conn.cur.params) if "INSERT INTO claude_messages" in s]
        self.assertEqual(ins[0][0], "391")
        self.assertEqual(json.loads(ins[0][1])["in_reply_to"], 3)
        self.assertEqual(post.call_args.kwargs, {"channel": "C1", "thread_ts": "123.4"})
        self.assertTrue(conn.closed)

    def test_no_slack_never_reads_keychain_or_posts(self):
        conn = _Conn([{"id": 1, "message": "hi", "metadata": None}])
        with patch.object(cc.psycopg2, "connect", return_value=conn), \
             patch.object(cc, "_keychain") as kc, patch.object(cc, "run_claude_code", return_value="r"), \
             patch.object(cc, "post_to_slack") as post, redirect_stdout(io.StringIO()):
            cc.main(["--once", "--no-slack"])
        kc.assert_not_called(); post.assert_not_called()

    def test_pg_down_raises_before_any_claude_call(self):
        with patch.object(cc.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")), \
             patch.object(cc, "run_claude_code") as rcc:
            with self.assertRaises(psycopg2.OperationalError):
                cc.main(["--once", "--no-slack"])
        rcc.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--allow-edits", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(psycopg2, "connect", side_effect=AssertionError("import must not connect")):
            _load()


if __name__ == "__main__":
    unittest.main()
