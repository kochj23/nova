#!/usr/bin/env python3
"""7-category gap tests for nova_slack_answers.py — slack() retry/backoff (and its no-double-post
rule), decide_proposal spawn retry, and the proposal-posting wiring (PROPOSALS_PER_DAY dial,
Annie Wilkes rule, turning point). Complements tests/test_nova_slack_answers.py. Offline:
urlopen, the keychain call, subprocess.run and time.sleep are mocked; nothing posts.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import subprocess
import sys
import time
import types
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_slack_answers.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_slack_answers_7cat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._QUIET = False   # quiet mode is covered by the TestQuiet* classes
    return mod


sa = _load()


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http(code):
    return urllib.error.HTTPError("https://slack.com/api/x", code, "err", {}, None)


class _Env(unittest.TestCase):
    def setUp(self):
        self.p = [mock.patch.object(sa.subprocess, "check_output", return_value=b"xoxb-test"),
                  mock.patch.object(sa.time, "sleep"),
                  # the legacy proposal-posting mechanism is kept but OFF since 2026-10-15; these
                  # tests exercise the mechanism itself, so they switch it on
                  mock.patch.object(sa, "ASK_JORDAN_PROPOSALS", True)]
        self.tok, self.sleep, _ = [x.start() for x in self.p]
        self._out = redirect_stdout(io.StringIO()); self.out = self._out.__enter__()

    def tearDown(self):
        self._out.__exit__(None, None, None)
        for x in self.p:
            x.stop()

    def urlopen(self, *effects):
        return mock.patch.object(sa.urllib.request, "urlopen", side_effect=list(effects))


class _Cur:
    def __init__(self, posted_today=0, rows=()):
        self.posted_today = posted_today; self.rows = list(rows); self.sql = []; self._last = ""

    def execute(self, sql, params=None):
        self.sql.append((sql, params)); self._last = sql

    def fetchone(self):
        return (self.posted_today,)

    def fetchall(self):
        return self.rows


class TestSecurity(_Env):
    def test_chat_write_not_retried_after_timeout(self):
        with self.urlopen(TimeoutError("read timed out")) as u:
            with self.assertRaises(TimeoutError):
                sa.slack("chat.postMessage", channel="C", text="t")
        self.assertEqual(u.call_count, 1)                               # never double-post

    def test_auth_errors_not_retried(self):
        with self.urlopen(_http(401)) as u:
            with self.assertRaises(urllib.error.HTTPError):
                sa.slack("conversations.replies", channel="C", ts="1")
        self.assertEqual(u.call_count, 1)

    def test_token_never_logged_on_failure(self):
        with self.urlopen(*[urllib.error.URLError("down")] * 3):
            with self.assertRaises(urllib.error.URLError):
                sa.slack("conversations.history", channel="C")
        self.assertNotIn("xoxb-test", self.out.getvalue())

    def test_annie_failing_proposal_not_posted(self):
        cur = _Cur(rows=[(5, "coagency", "Ping him every hour until he answers", "why")])
        with mock.patch.object(sa, "datetime", mock.Mock(now=lambda: datetime(2026, 10, 8, 11))), \
                mock.patch.object(sa, "_annie_ok", return_value=False), \
                mock.patch.object(sa, "slack") as sl:
            sa.post_pending_proposals(cur, dry=False)
        sl.assert_not_called()


class TestPerformance(_Env):
    def test_daily_allowance_bounds_the_query(self):
        cur = _Cur(posted_today=1)
        with mock.patch.object(sa, "datetime", mock.Mock(now=lambda: datetime(2026, 10, 8, 11))):
            sa.post_pending_proposals(cur, dry=True)
        self.assertEqual(cur.sql[1][1], (sa.PROPOSALS_PER_DAY - 1,))

    def test_allowance_spent_skips_proposal_scan(self):
        cur = _Cur(posted_today=sa.PROPOSALS_PER_DAY)
        with mock.patch.object(sa, "datetime", mock.Mock(now=lambda: datetime(2026, 10, 8, 11))):
            sa.post_pending_proposals(cur, dry=False)
        self.assertEqual(len(cur.sql), 1)

    def test_off_hours_touch_nothing(self):
        cur = _Cur()
        with mock.patch.object(sa, "datetime", mock.Mock(now=lambda: datetime(2026, 10, 8, 3))):
            sa.post_pending_proposals(cur, dry=False)
        self.assertEqual(cur.sql, [])

    def test_retry_is_bounded(self):
        with self.urlopen(*[urllib.error.URLError("down")] * 10) as u:
            with self.assertRaises(urllib.error.URLError):
                sa.slack("conversations.replies", channel="C", ts="1")
        self.assertEqual(u.call_count, 3)


class TestRetry(_Env):
    def test_transient_then_success_with_backoff(self):
        with self.urlopen(urllib.error.URLError("dns"), _http(503), _Resp({"ok": True})) as u:
            self.assertEqual(sa.slack("conversations.replies", channel="C", ts="1"), {"ok": True})
        self.assertEqual(u.call_count, 3)
        self.assertEqual([c[0][0] for c in self.sleep.call_args_list], [1.5, 3.0])

    def test_rate_limit_retried(self):
        with self.urlopen(_http(429), _Resp({"ok": True, "ts": "9"})) as u:
            self.assertEqual(sa.slack("chat.postMessage", channel="C", text="t")["ts"], "9")
        self.assertEqual(u.call_count, 2)

    def test_keychain_hiccup_retried(self):
        self.tok.side_effect = [subprocess.CalledProcessError(1, "security"), b"tok"]
        with self.urlopen(_Resp({"ok": True})):
            self.assertTrue(sa.slack("reactions.get", channel="C", timestamp="1")["ok"])
        self.assertEqual(self.tok.call_count, 2)

    def test_failures_are_logged_not_silent(self):
        with self.urlopen(urllib.error.URLError("x"), _Resp({"ok": True})):
            sa.slack("conversations.replies", channel="C", ts="1")
        self.assertIn("attempt 1/3", self.out.getvalue())

    def test_decide_proposal_spawn_failure_retried(self):
        ok = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(sa.subprocess, "run", side_effect=[OSError("fork"), ok]) as r:
            self.assertTrue(sa.decide_proposal("5", "yes", "yes", dry=False))
        self.assertEqual(r.call_count, 2)

    def test_decide_proposal_spawn_exhausted_returns_false(self):
        with mock.patch.object(sa.subprocess, "run", side_effect=OSError("fork")) as r:
            self.assertFalse(sa.decide_proposal("5", "yes", "yes", dry=False))
        self.assertEqual(r.call_count, 3)

    def test_decide_proposal_nonzero_rc_not_retried(self):
        with mock.patch.object(sa.subprocess, "run", return_value=mock.Mock(returncode=2, stdout="", stderr="already")) as r:
            self.assertFalse(sa.decide_proposal("5", "no", "no", dry=False))
        self.assertEqual(r.call_count, 1)                                # a decision is not idempotent-safe


class TestUnit(_Env):
    def test_turning_point_stakes_and_ceiling(self):
        fake = types.SimpleNamespace(decide=mock.MagicMock(return_value={"allowed": True, "reason": ""}))
        with mock.patch.dict(sys.modules, {"nova_turning_point": fake}):
            sa._turning_point("cur", "t", 3)
            sa._turning_point("cur", "t", 40)
        (a1, k1), (_, k2) = fake.decide.call_args_list
        self.assertEqual(a1, ("cur", "proposal"))
        self.assertAlmostEqual(k1["stakes"], 0.80)
        self.assertEqual(k2["stakes"], 1.0)
        self.assertEqual(k1["ceiling"], "recommend")

    def test_guards_fail_open_when_modules_missing(self):
        with mock.patch.dict(sys.modules, {"nova_turning_point": None, "nova_annie_rule": None}):
            self.assertTrue(sa._turning_point("c", "t", 1)["allowed"])
            self.assertTrue(sa._annie_ok("t"))

    def test_dial_range(self):
        self.assertIn(sa.PROPOSALS_PER_DAY, range(1, 7))                 # dial_scale('proactivity', 1, 3, 6)


class TestIntegration(_Env):
    def test_posted_proposal_recorded_in_slack_prompts(self):
        cur = _Cur(rows=[(11, "coagency", "Rotate the backup key", "it is 400 days old")])
        with mock.patch.object(sa, "datetime", mock.Mock(now=lambda: datetime(2026, 10, 8, 11))), \
                mock.patch.object(sa, "_annie_ok", return_value=True), \
                mock.patch.object(sa, "_turning_point", return_value={"allowed": True, "reason": ""}), \
                self.urlopen(_Resp({"ok": True, "ts": "123.4"})) as u:
            sa.post_pending_proposals(cur, dry=False)
        body = json.loads(u.call_args[0][0].data.decode())
        self.assertEqual(body["channel"], sa.CHANNEL)
        self.assertIn("Proposal #11", body["text"])
        sql, params = cur.sql[-1]
        self.assertIn("INSERT INTO slack_prompts", sql)
        self.assertEqual(params, ("11", sa.CHANNEL, "123.4"))


class TestFunctional(_Env):
    def test_turning_point_hold_stops_the_batch(self):
        cur = _Cur(rows=[(1, "o", "a", "w"), (2, "o", "b", "w")])
        with mock.patch.object(sa, "datetime", mock.Mock(now=lambda: datetime(2026, 10, 8, 11))), \
                mock.patch.object(sa, "_annie_ok", return_value=True), \
                mock.patch.object(sa, "_turning_point", return_value={"allowed": False, "reason": "budget"}) as tp, \
                mock.patch.object(sa, "slack") as sl:
            sa.post_pending_proposals(cur, dry=False)
        sl.assert_not_called()
        self.assertEqual(tp.call_count, 1)
        self.assertIn("held", self.out.getvalue())

    def test_slack_down_during_post_raises_without_recording(self):
        cur = _Cur(rows=[(1, "o", "a", "w")])
        with mock.patch.object(sa, "datetime", mock.Mock(now=lambda: datetime(2026, 10, 8, 11))), \
                mock.patch.object(sa, "_annie_ok", return_value=True), \
                mock.patch.object(sa, "_turning_point", return_value={"allowed": True, "reason": ""}), \
                self.urlopen(*[urllib.error.URLError("down")] * 3):
            with self.assertRaises(urllib.error.URLError):
                sa.post_pending_proposals(cur, dry=False)
        self.assertFalse(any("INSERT" in s for s, _ in cur.sql))


class TestFrame(unittest.TestCase):
    def test_import_has_no_side_effects(self):
        with mock.patch("psycopg2.connect") as c, mock.patch("urllib.request.urlopen") as u:
            _load()
        c.assert_not_called(); u.assert_not_called()

    def test_entrypoints(self):
        for name in ("main", "demo", "slack", "_slack_once", "decide_proposal", "post_pending_proposals"):
            self.assertTrue(callable(getattr(sa, name)), name)
        self.assertIn('if __name__ == "__main__":', SRC)


# ══ 2026-10-08: quiet mode — hold proposal prompts (nags) and shorten thread confirmations ═══════
class _QuietEnv(unittest.TestCase):
    def setUp(self):
        self._saved = sa._QUIET

    def tearDown(self):
        sa._QUIET = self._saved


class TestQuietSecurity(_QuietEnv):
    def test_quiet_never_auto_approves_anything(self):
        sa._QUIET = True
        cur = _Cur()
        with mock.patch.object(sa, "datetime", mock.Mock(now=lambda: datetime(2026, 10, 8, 11))), \
                mock.patch.object(sa, "slack") as s, redirect_stdout(io.StringIO()):
            sa.post_pending_proposals(cur, dry=False)
        s.assert_not_called()


class TestQuietPerformance(_QuietEnv):
    def test_quiet_skips_all_proposal_queries(self):
        sa._QUIET = True
        cur = _Cur()
        with mock.patch.object(sa, "datetime", mock.Mock(now=lambda: datetime(2026, 10, 8, 11))), \
                redirect_stdout(io.StringIO()):
            sa.post_pending_proposals(cur, dry=False)
        self.assertEqual(cur.sql, [])


class TestQuietRetry(_QuietEnv):
    def test_quiet_mode_error_fails_open(self):
        bad = types.SimpleNamespace(quiet_mode=mock.Mock(side_effect=RuntimeError("pg")))
        with mock.patch.dict(sys.modules, {"nova_relationship": bad}):
            self.assertFalse(sa.quiet_active(None))


class TestQuietUnit(_QuietEnv):
    def test_quiet_active_reads_flag(self):
        for v in (True, False):
            rel = types.SimpleNamespace(quiet_mode=lambda cur=None, v=v: {"active": v})
            with mock.patch.dict(sys.modules, {"nova_relationship": rel}):
                self.assertIs(sa.quiet_active(None), v)


class TestQuietIntegration(_QuietEnv):
    def test_confirm_is_terse_while_quiet(self):
        sa._QUIET = True
        with mock.patch.object(sa, "slack") as s:
            sa.confirm("C", "1.0", "Recorded: approved #5 via your reply. Handed to Claude.", dry=False)
        self.assertEqual(s.call_args.kwargs["text"], "Recorded.")


class TestQuietFunctional(_QuietEnv):
    def test_not_quiet_confirm_unchanged(self):
        sa._QUIET = False
        with mock.patch.object(sa, "slack") as s:
            sa.confirm("C", "1.0", "Recorded: Q#3.", dry=False)
        self.assertEqual(s.call_args.kwargs["text"], "Recorded: Q#3.")


class TestQuietFrame(_QuietEnv):
    def test_quiet_api_present(self):
        self.assertTrue(callable(sa.quiet_active)); self.assertTrue(hasattr(sa, "_QUIET"))



# ═════════════════════════════════════════════════════════════════════════════
# 2026-10-15 — Nova is her own customer: she stops asking Jordan to decide proposals.
# ═════════════════════════════════════════════════════════════════════════════
class _ConflictCur:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))


class TestOwnCustomer(unittest.TestCase):
    def test_security_default_never_puts_proposals_to_jordan(self):
        self.assertFalse(sa.ASK_JORDAN_PROPOSALS)
        cur = mock.MagicMock()
        with mock.patch.object(sa, "POST_HOURS", range(0, 24)), mock.patch.object(sa, "slack") as sl:
            sa.post_pending_proposals(cur, dry=False)
        sl.assert_not_called()
        cur.execute.assert_not_called()

    def test_performance_disabled_path_touches_nothing(self):
        cur = mock.MagicMock()
        t = time.perf_counter()
        for _ in range(1000):
            sa.post_pending_proposals(cur, dry=False)
        self.assertLess(time.perf_counter() - t, 1.0)
        cur.execute.assert_not_called()

    def test_retry_organ_unavailable_still_records_his_conflict_decision(self):
        cur = _ConflictCur()
        with mock.patch.dict(sys.modules, {"nova_directive_conflict": None}), \
             mock.patch.object(sa, "read_answer", return_value=("A wins", None)), \
             mock.patch.object(sa, "confirm"):
            self._run_conflict(cur)
        self.assertTrue(any("UPDATE directive_conflicts" in s_ and p[1] == 9 for s_, p in cur.sql if p))

    def test_unit_flag_is_a_module_constant(self):
        self.assertIsInstance(sa.ASK_JORDAN_PROPOSALS, bool)

    def test_integration_conflict_reply_goes_through_organ_decide(self):
        dc = mock.MagicMock(); dc.decide = mock.MagicMock(return_value=True)
        cur = _ConflictCur()
        with mock.patch.dict(sys.modules, {"nova_directive_conflict": dc}), \
             mock.patch.object(sa, "read_answer", return_value=("both apply", None)), \
             mock.patch.object(sa, "confirm"):
            self._run_conflict(cur)
        kw = dc.decide.call_args.kwargs
        self.assertEqual((kw["conflict_id"], kw["decision"], kw["decided_by"]), (9, "both apply", "jordan"))
        self.assertFalse(any("UPDATE directive_conflicts" in s_ for s_, _ in cur.sql))

    def test_functional_blanket_yes_on_denied_proposal_is_not_confirmed(self):
        hist = {"ok": True, "messages": [{"user": next(iter(sa.HUMANS)), "ts": "1000.0", "text": "All approved"}]}
        cur = mock.MagicMock(); cur.fetchall.return_value = [(5, "168", "999.0")]
        with mock.patch.object(sa, "slack", return_value=hist), \
             mock.patch.object(sa, "decide_proposal", return_value=False) as dp, \
             mock.patch.object(sa, "confirm") as cf, redirect_stdout(io.StringIO()):
            n = sa.blanket_approvals(cur, dry=False)
        dp.assert_called_once()
        cf.assert_not_called()
        self.assertEqual(n, 0)

    def _run_conflict(self, cur):
        """Drive main()'s harvest loop for one open conflict prompt (id 9)."""
        class _C(_ConflictCur):
            def fetchall(inner):
                last = inner.sql[-1][0]
                return [(1, "conflict", "9", "C", "1.1")] if "FROM slack_prompts WHERE resolved_at IS NULL" in last else []

            def fetchone(inner):
                return (0,)
        c = _C(); c.sql = cur.sql
        conn = mock.MagicMock(); conn.cursor.return_value = c
        with mock.patch.object(sa.psycopg2, "connect", return_value=conn), \
             mock.patch.object(sa, "quiet_active", return_value=False), \
             mock.patch.object(sa, "blanket_approvals", return_value=0), \
             mock.patch.object(sa.sys, "argv", ["nova_slack_answers.py"]), redirect_stdout(io.StringIO()):
            self.assertEqual(sa.main(), 0)

    def test_frame_module_still_imports_and_exposes_entrypoints(self):
        for n in ("main", "post_pending_proposals", "blanket_approvals", "decide_proposal"):
            self.assertTrue(callable(getattr(sa, n)))


if __name__ == "__main__":
    unittest.main()
