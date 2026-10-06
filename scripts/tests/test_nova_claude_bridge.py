#!/usr/bin/env python3
"""Tests for nova_claude_bridge.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_claude_bridge.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("claude_bridge", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._post_slack_claude = MagicMock()          # never reach Slack from a test
    return mod


cb = _load()


class _Sinks:
    """Patch pg_query / redis_cmd / http_request on the module and record every call."""
    def __init__(self, pg_answers=None, redis_answers=None, http_answers=None):
        self.pg = []; self.redis = []; self.http = []
        self.pg_answers = list(pg_answers or []); self.redis_answers = list(redis_answers or [])
        self.http_answers = list(http_answers or [])

    def __enter__(self):
        self._p = [patch.object(cb, "pg_query", side_effect=self._pg),
                   patch.object(cb, "redis_cmd", side_effect=self._redis),
                   patch.object(cb, "http_request", side_effect=self._http)]
        for p in self._p:
            p.start()
        cb._post_slack_claude.reset_mock()
        return self

    def __exit__(self, *a):
        for p in self._p:
            p.stop()

    def _pg(self, sql, params=(), fetchall=True):
        self.pg.append((sql, params))
        return self.pg_answers.pop(0) if (fetchall and self.pg_answers) else []

    def _redis(self, *args):
        self.redis.append(args)
        return self.redis_answers.pop(0) if self.redis_answers else "OK"

    def _http(self, url, method="GET", data=None, timeout=10):
        self.http.append(url)
        return self.http_answers.pop(0) if self.http_answers else {}


def _out(fn, args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(args)
    return buf.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_slack_token_from_keychain(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/\-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("xoxb-", SRC)
        self.assertIn('"security", "find-generic-password", "-s", "nova-slack-bot-token"', SRC)

    def test_psql_fallback_escapes_quotes(self):
        with patch.object(cb.subprocess, "run", return_value=MagicMock(returncode=0, stdout="")) as run:
            cb._pg_query_subprocess("SELECT * FROM t WHERE a=%s AND b=%s AND c=%s", ("x'); --injected", None, 3))
        q = run.call_args.args[0][-1]
        self.assertEqual(q, "SELECT * FROM t WHERE a='x''); --injected' AND b=NULL AND c=3")

    def test_user_text_only_travels_as_params(self):
        with _Sinks() as s:
            _out(cb.cmd_send, types.SimpleNamespace(message=["x'); --injected"]))
        for sql, params in s.pg:
            self.assertNotIn("--injected", sql)
        self.assertTrue(any("--injected" in str(p) for _, p in s.pg))


class TestPerformance(unittest.TestCase):
    def test_psql_fallback_parses_10k_rows_fast(self):
        stdout = "\n".join(f"{i}\x1fname{i}" for i in range(10_000))
        with patch.object(cb.subprocess, "run", return_value=MagicMock(returncode=0, stdout=stdout)):
            t0 = time.perf_counter()
            rows = cb._pg_query_subprocess("SELECT a, b FROM t")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(rows), 10_000)
        self.assertEqual(rows[-1], ["9999", "name9999"])


class TestRetry(unittest.TestCase):
    def test_http_request_one_shot_returns_error_dict(self):
        # RETRY GAP: http_request/urlopen — one attempt, errors become {"error": ...}
        with patch.object(cb.urllib.request, "urlopen", side_effect=urllib.error.URLError("refused")) as u:
            r = cb.http_request("http://127.0.0.1:1/x")
        self.assertEqual(u.call_count, 1)
        self.assertEqual(r, {"error": "Connection failed: refused"})

    def test_trigger_falls_back_to_queue_when_scheduler_down(self):
        with _Sinks(http_answers=[{"error": "Connection failed: refused"}]) as s:
            out = _out(cb.cmd_trigger, types.SimpleNamespace(task_id="daily_essay"))
        self.assertIn("queued via claude_queue", out)
        self.assertEqual(len(s.http), 1)
        self.assertTrue(any(p and p[3:4] == ("trigger:daily_essay",) for _, p in s.pg))

    def test_slack_post_is_fire_and_forget(self):
        spec = importlib.util.spec_from_file_location("claude_bridge_raw", SCRIPT)
        raw = importlib.util.module_from_spec(spec); spec.loader.exec_module(raw)
        with patch.object(raw.subprocess, "run", side_effect=OSError("no keychain")), \
             patch.object(raw.urllib.request, "urlopen") as u:
            self.assertIsNone(raw._post_slack_claude("hi"))
        u.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_decisions_rejects_bad_since(self):
        for bad in ("xh", "5y"):
            with _Sinks(), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                cb.cmd_decisions(types.SimpleNamespace(since=bad))

    def test_decisions_cutoff_uses_ms(self):
        with _Sinks() as s:
            t0 = int(time.time())
            out = _out(cb.cmd_decisions, types.SimpleNamespace(since="2h"))
        cutoff = s.pg[0][1][0]
        self.assertAlmostEqual(cutoff, (t0 - 7200) * 1000, delta=5000)
        self.assertIn("No scheduler runs", out)

    def test_redis_cmd_raises_on_failure(self):
        with patch.object(cb.subprocess, "run", return_value=MagicMock(returncode=1, stderr="conn refused")):
            with self.assertRaises(RuntimeError):
                cb.redis_cmd("GET", "k")

    def test_scratch_read_missing_exits(self):
        with _Sinks(redis_answers=[""]), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            cb.cmd_scratch(types.SimpleNamespace(scratch_action="read", key="nope", value=[], ttl=None))


class TestIntegration(unittest.TestCase):
    def test_send_writes_messages_then_queue_and_posts_slack(self):
        with _Sinks() as s:
            _out(cb.cmd_send, types.SimpleNamespace(message=["hello", "nova"]))
        tables = [re.search(r"INSERT INTO (\w+)", q).group(1) for q, _ in s.pg if "INSERT INTO" in q]
        self.assertEqual(tables, ["claude_sessions", "claude_messages", "claude_queue"])
        cb._post_slack_claude.assert_called_once_with("*Claude Code:* hello nova")

    def test_test_env_tags_message(self):
        with _Sinks() as s, patch.dict(os.environ, {"NOVA_BRIDGE_TEST": "1"}):
            _out(cb.cmd_send, types.SimpleNamespace(message=["ping"]))
        meta = [json.loads(p[3]) for q, p in s.pg if "INSERT INTO claude_messages" in q][0]
        self.assertTrue(meta["test"])

    def test_editing_lock_uses_prefix_and_ttl(self):
        with _Sinks() as s:
            _out(cb.cmd_editing, types.SimpleNamespace(filepath="/tmp/x.py"))
        args = s.redis[0]
        self.assertEqual(args[:2], ("SET", "nova:editing:/tmp/x.py"))
        self.assertEqual(args[-2:], ("EX", "1800"))


class TestFunctional(unittest.TestCase):
    def test_handoff_writes_queue_redis_and_message(self):
        with _Sinks() as s:
            out = _out(cb.cmd_handoff, types.SimpleNamespace(summary=["fixed", "bug"]))
        self.assertIn("OK — handoff queued", out)
        self.assertEqual(s.redis[0], ("SET", cb.HANDOFF_KEY, "fixed bug", "EX", "172800"))
        self.assertTrue(any(p and p[3:4] == ("HANDOFF: fixed bug",) for _, p in s.pg))

    def test_receive_prints_rows(self):
        rows = [[{"message": "hi claude", "created_at": "t1"}], [{"description": "d", "outcome": "done", "completed_at": "t2"}]]
        with _Sinks(pg_answers=rows):
            out = _out(cb.cmd_receive, types.SimpleNamespace(limit=5))
        self.assertIn("[t1] hi claude", out)
        self.assertIn("-> done", out)

    def test_review_without_diff_exits(self):
        with _Sinks() as s, patch.object(cb.subprocess, "run", return_value=MagicMock(returncode=0, stdout="")), \
             redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            cb.cmd_review(types.SimpleNamespace(filepath="x.py"))
        self.assertEqual(s.pg, [])

    def test_main_routes_unhandled_errors_to_exit_1(self):
        with patch.object(sys, "argv", ["x", "editing-status"]), patch.object(cb, "redis_cmd", side_effect=RuntimeError("down")), \
             redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cb.main()
        self.assertEqual(cm.exception.code, 1)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("handoff", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)


if __name__ == "__main__":
    unittest.main()
