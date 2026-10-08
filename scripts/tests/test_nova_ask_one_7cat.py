#!/usr/bin/env python3
"""7-category tests for nova_ask_one.py's 2026-10-08 changes: quiet-mode adoption
(nova_relationship.quiet_mode — no daily question during a hard stretch) and the Slack/PG
retry with backoff. Complements test_nova_ask_one.py. No Slack, no PG: every external call
is mocked. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import subprocess
import sys
import types
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ask_one.py"
SRC = SCRIPT.read_text()

_spec = importlib.util.spec_from_file_location("ask1_7cat", SCRIPT)
ask = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ask)

ROWS = [(5, "What was the reason the printers went offline?", "bambu", None)]


class _Cur:
    def __init__(self, answers=()):
        self.answers = list(answers); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._last = next((v for k, v in self.answers if k in sql), None)

    def fetchone(self):
        return self._last[0] if isinstance(self._last, list) else self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else ([] if self._last is None else [self._last])


class _Conn:
    def __init__(self, cur): self._cur = cur; self.autocommit = False
    def cursor(self): return self._cur


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _rel(active):
    return types.SimpleNamespace(quiet_mode=lambda cur=None: {"active": active})


def _run_main(cur, active, urlopen=None):
    post = mock.MagicMock(return_value=_Resp({"ok": True, "ts": "1.2"}))
    with mock.patch.dict(sys.modules, {"nova_relationship": _rel(active)}), \
            mock.patch.object(ask.psycopg2, "connect", lambda *a, **k: _Conn(cur)), \
            mock.patch.object(subprocess, "check_output", lambda *a, **k: b"tok"), \
            mock.patch.object(ask.sys, "platform", "darwin"), \
            mock.patch.object(ask.urllib.request, "urlopen", urlopen or post) as u, \
            mock.patch.object(sys, "argv", ["nova_ask_one.py"]), redirect_stdout(io.StringIO()) as out:
        rc = ask.main()
    return rc, u, out.getvalue()


def _cur():
    return _Cur([("count(*) FROM slack_prompts", (0,)), ("FROM reflection_questions", ROWS)])


class TestSecurity(unittest.TestCase):
    def test_quiet_mode_failure_fails_open_not_crash(self):
        bad = types.SimpleNamespace(quiet_mode=mock.Mock(side_effect=RuntimeError("x")))
        with mock.patch.dict(sys.modules, {"nova_relationship": bad}):
            self.assertFalse(ask.quiet_active(None))

    def test_no_token_or_dsn_password_in_source(self):
        self.assertNotRegex(SRC, r"xox[bp]-[A-Za-z0-9]")
        self.assertNotIn("password=", SRC)


class TestPerformance(unittest.TestCase):
    def test_quiet_check_skips_question_scan(self):
        cur = _cur()
        _run_main(cur, active=True)
        self.assertFalse(any("reflection_questions" in s for s, _ in cur.sql))


class TestRetry(unittest.TestCase):
    def test_slack_503_retried_then_posts(self):
        with mock.patch.object(ask, "_slack_post_once") as once, mock.patch("time.sleep") as sl:
            once.side_effect = [urllib.error.HTTPError("u", 503, "x", {}, None), "9.9"]
            self.assertEqual(ask.slack_post("q"), "9.9")
        self.assertEqual(once.call_count, 2); sl.assert_called_once()

    def test_slack_error_reply_not_retried(self):
        with mock.patch.object(ask, "_slack_post_once", side_effect=RuntimeError("channel_not_found")) as once:
            with self.assertRaises(RuntimeError):
                ask.slack_post("q")
        self.assertEqual(once.call_count, 1)

    def test_timeout_never_resent(self):
        with mock.patch.object(ask, "_slack_post_once",
                               side_effect=urllib.error.URLError(TimeoutError("read"))) as once:
            with self.assertRaises(urllib.error.URLError):
                ask.slack_post("q")
        self.assertEqual(once.call_count, 1)

    def test_retry_bounded_and_raises(self):
        with mock.patch.object(ask, "_slack_post_once", side_effect=urllib.error.URLError("down")) as once, \
                mock.patch("time.sleep"):
            with self.assertRaises(urllib.error.URLError):
                ask.slack_post("q")
        self.assertEqual(once.call_count, 3)

    def test_pg_connect_retries(self):
        OP = ask.psycopg2.OperationalError
        with mock.patch.object(ask.psycopg2, "connect", side_effect=[OP("a"), OP("b"), "conn"]) as c, \
                mock.patch("time.sleep"), redirect_stdout(io.StringIO()):
            self.assertEqual(ask._pg_connect(), "conn")
        self.assertEqual(c.call_count, 3)


class TestUnit(unittest.TestCase):
    def test_quiet_active_reads_flag(self):
        for v in (True, False):
            with mock.patch.dict(sys.modules, {"nova_relationship": _rel(v)}):
                self.assertIs(ask.quiet_active(None), v)


class TestIntegration(unittest.TestCase):
    def test_real_quiet_mode_with_stale_row_reads_inactive(self):
        import nova_relationship
        from datetime import datetime, timedelta, timezone
        cur = mock.Mock()
        cur.fetchone.return_value = ({"active": True}, datetime.now(timezone.utc) - timedelta(hours=12))
        with mock.patch.dict(sys.modules, {"nova_relationship": nova_relationship}):
            self.assertFalse(ask.quiet_active(cur))
        cur.fetchone.return_value = ({"active": True}, datetime.now(timezone.utc))
        with mock.patch.dict(sys.modules, {"nova_relationship": nova_relationship}):
            self.assertTrue(ask.quiet_active(cur))


class TestFunctional(unittest.TestCase):
    def test_quiet_skips_the_question(self):
        rc, u, out = _run_main(_cur(), active=True)
        self.assertEqual(rc, 0); u.assert_not_called(); self.assertIn("quiet mode", out)

    def test_not_quiet_posts_and_records(self):
        cur = _cur()
        rc, u, _ = _run_main(cur, active=False)
        self.assertEqual(rc, 0); u.assert_called_once()
        self.assertTrue(any("INSERT INTO slack_prompts" in s for s, _ in cur.sql))


class TestFrame(unittest.TestCase):
    def test_selftest_runs(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True,
                           timeout=60, cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_api_present(self):
        for n in ("quiet_active", "slack_post", "_slack_post_once", "_pg_connect", "main"):
            self.assertTrue(callable(getattr(ask, n)), n)


if __name__ == "__main__":
    unittest.main()
