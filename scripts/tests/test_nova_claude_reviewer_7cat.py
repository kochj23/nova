#!/usr/bin/env python3
"""7-category tests for nova_claude_reviewer.py (Jordan standing order 2026-10-08) plus the two
changes it needed elsewhere: nova_aspirations.approve_wish is idempotent, and
nova_coagency.mode_decide takes expect_status and keeps claude-reviewer decisions out of the
earned-autonomy track record.

Categories: Security, Performance, Retry, Unit, Integration, Functional, Frame (smoke).
No real DB, Slack, Claude or memory server is touched: every boundary is stubbed.
Written by Jordan Koch (via Claude)."""
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("NOVA_GUARDS_NO_SLACK", "1")

import nova_claude_reviewer as R  # noqa: E402
import nova_aspirations as asp     # noqa: E402
import nova_coagency as co         # noqa: E402

SRC = (SCRIPTS / "nova_claude_reviewer.py").read_text()


class _Cur:
    """Answers keyed by SQL fragment (first match wins; callables get (sql, params)); records everything."""
    def __init__(self, answers=(), rowcount=1):
        self.answers = list(answers); self.sql = []; self._last = None; self.rowcount = rowcount

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        hit = next((v for k, v in self.answers if k in sql), None)
        if callable(hit):
            hit = hit(sql, params)
        if isinstance(hit, Exception):
            raise hit
        self._last = hit

    def fetchone(self):
        return self._last[0] if isinstance(self._last, list) else self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else ([] if self._last is None else [self._last])

    def executed(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


def _quiet():
    return redirect_stdout(io.StringIO())


def _item(kind="coagency", title="increase log verbosity for llm-ping", **kw):
    it = {"id": kw.pop("id", "P1"), "kind": kind, "ref": kw.pop("ref", 1), "title": title,
          "subject": kw.pop("subject", title), "text": title, "redline_pass": True, "vc_allowed": True, "target": None}
    it.update(kw)
    return it


def _ask(decisions):
    """Fake claude: answers each batch with the given decision for every id in it."""
    calls = []

    def ask(system, user):
        calls.append(user)
        ids = re.findall(r"--- id (\S+)", user)
        return json.dumps([{"id": i, "decision": decisions.get(i, "APPROVE"), "reason": f"because {i}"} for i in ids])
    ask.calls = calls
    return ask


def _backlog(extra=()):
    """A live-looking backlog: P121 (av_power), P7 (benign), W5 wish, one skill, lockbox L1."""
    return _Cur(list(extra) + [
        ("FROM service_config WHERE service='claude_reviewer'", None),
        ("FROM claude_reviewer_log WHERE kind", None),
        ("FROM coagency_proposals WHERE status='pending_human'", [
            (121, "tinker", "re-enable and recalibrate 'av_power' sensor", "restore my balance", None, True,
             {"available": True, "allowed": False}),
            (7, "observation", "increase log verbosity for llm-ping", "slow tokens", None, True,
             {"available": True, "allowed": True})]),
        ("FROM feature_wishes\n                  WHERE status='wished'", [(5, "Tide sense", "Read the tide tables", "fun")]),
        ("FROM nova_skills s", [("pursue-interest-x", "Pursue Interest: X", "when x", "summary", ["a", "b"], "low")]),
        ("FROM lockbox WHERE status='proposed' ORDER BY id", [(1, "mem-1", "painful")]),
        ("FROM feature_wishes WHERE status IN", [(70, "Presence")]),
        ("FROM nova_skills WHERE status='implemented'", [("pursue-interest-nightly",)]),
    ])


# ═══════════════════════════════════════════════════════════════════════════════
class TestSecurity(unittest.TestCase):
    def test_av_power_121_holds_without_asking_claude(self):
        it = _item(title="re-enable and recalibrate 'av_power' sensor", id="P121", ref=121)
        ask = _ask({"P121": "APPROVE"})
        out = R.decide([it], "", ask=ask)
        self.assertEqual(out["P121"]["decision"], "HOLD")
        self.assertEqual(out["P121"]["source"], "floor")
        self.assertEqual(ask.calls, [])

    def test_any_change_to_her_own_gates_holds(self):
        for t in ("raise my caps to 12 per hour", "disable the kill switch for an hour", "adjust the value_check rubric",
                  "loosen the red lines on reach", "widen the coagency_mode allowlist", "grant earned autonomy to observe"):
            self.assertIsNotNone(R.floor(_item(kind="wish", title=t)), t)

    def test_money_and_other_peoples_data_hold(self):
        self.assertIn("money", R.floor(_item(kind="wish", title="a better GPU for $900")))
        self.assertIn("other people", R.floor(_item(kind="wish", title="read Amy's iMessage inbox")))

    def test_red_line_blocks_approval(self):
        self.assertIn("red line", R.floor(_item(title="delete old memories", redline_pass=True)))
        self.assertIn("red line", R.floor(_item(redline_pass=False)))
        self.assertIn("self-preservation", R.floor(_item(kind="wish", title="back myself up elsewhere")))

    def test_value_check_refusal_can_never_be_approved(self):
        out = R.decide([_item(vc_allowed=False)], "", ask=_ask({"P1": "APPROVE"}))
        self.assertEqual(out["P1"]["decision"], "HOLD")
        out = R.decide([_item(vc_allowed=False)], "", ask=_ask({"P1": "DECLINE"}))
        self.assertEqual(out["P1"]["decision"], "DECLINE")

    def test_verdict_for_an_id_outside_the_batch_is_ignored(self):
        b = [_item(id="P1")]
        out = R.parse_decisions('[{"id":"P999","decision":"APPROVE","reason":"x"},'
                                '{"id":"P1","decision":"HOLD","reason":"y"}]', b)
        self.assertEqual(set(out), {"P1"})
        self.assertEqual(out["P1"]["decision"], "HOLD")

    def test_duplicate_id_first_verdict_wins(self):
        out = R.parse_decisions('[{"id":"P1","decision":"HOLD","reason":"a"},{"id":"P1","decision":"APPROVE","reason":"b"}]',
                                [_item()])
        self.assertEqual(out["P1"]["decision"], "HOLD")

    def test_system_prompt_marks_item_text_as_data(self):
        self.assertIn("not instructions", R.SYSTEM)

    def test_kill_switch_off_and_unreadable_fail_closed(self):
        with _quiet():
            self.assertFalse(R.enabled(_Cur([("claude_reviewer", (False,))])))
            self.assertFalse(R.enabled(_Cur([("claude_reviewer", ('"false"',))])))
            self.assertFalse(R.enabled(_Cur([("claude_reviewer", RuntimeError("pg"))])))

    def test_autonomy_kill_switch_stops_it(self):
        s = types.SimpleNamespace(kill_switch_engaged=lambda oc: True)
        with mock.patch.dict(sys.modules, {"nova_autonomy_safety": s}), _quiet():
            self.assertFalse(R.enabled(_Cur([("claude_reviewer", None)])))

    def test_reviewer_decisions_do_not_feed_earned_autonomy(self):
        cur = _Cur([("SELECT status, redline_pass, value_check", ("pending_human", True, {"available": True})),
                    ("SELECT target_service, proposed_action", (None, "x"))])
        with mock.patch.object(co._safety, "note_human_decision") as nhd, _quiet():
            self.assertEqual(co.mode_decide(cur, "live", 5, "approve", "n", "claude-reviewer", "pending_human"), 0)
        nhd.assert_not_called()
        with mock.patch.object(co._safety, "note_human_decision") as nhd, _quiet():
            co.mode_decide(cur, "live", 5, "approve", "n", "jordan")
        nhd.assert_called_once()

    def test_no_user_paths_and_parameterized_sql(self):
        self.assertNotRegex(SRC, r"/Users/\w+/")
        self.assertNotRegex(SRC, r"execute\(f[\"']")
        self.assertNotRegex(SRC, r"\.execute\([^)]*%\s*\(")


class TestPerformance(unittest.TestCase):
    def test_items_are_batched(self):
        items = [_item(id=f"P{i}", ref=i) for i in range(20)]
        ask = _ask({})
        R.decide(items, "", ask=ask)
        self.assertEqual(len(ask.calls), 3)                       # 8 + 8 + 4

    def test_floored_items_cost_no_call(self):
        ask = _ask({})
        R.decide([_item(kind="wish", title="raise my caps")] * 5, "", ask=ask)
        self.assertEqual(ask.calls, [])

    def test_held_items_are_not_reasked_within_window(self):
        cur = _backlog([("FROM claude_reviewer_log WHERE kind", ("HOLD", "floor"))])
        with _quiet():
            self.assertEqual(R.gather(cur), [])

    def test_outage_holds_are_retried(self):
        cur = _backlog([("FROM claude_reviewer_log WHERE kind", ("HOLD", "error"))])
        with _quiet():
            self.assertEqual(len(R.gather(cur)), 5)

    def test_gather_is_capped_and_parse_is_fast(self):
        self.assertLessEqual(R.MAX_ITEMS, 50)
        raw = json.dumps([{"id": f"P{i}", "decision": "APPROVE", "reason": "r"} for i in range(500)])
        t = time.perf_counter()
        R.parse_decisions(raw, [_item(id=f"P{i}") for i in range(500)])
        self.assertLess(time.perf_counter() - t, 0.5)


class TestRetry(unittest.TestCase):
    def test_pg_connect_retries_then_raises(self):
        calls, sleeps = [], []
        fake = types.SimpleNamespace(connect=lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(OSError("down")))
        with mock.patch.dict(sys.modules, {"psycopg2": fake}), _quiet():
            with self.assertRaises(OSError):
                R._connect("dsn", _sleep=sleeps.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, list(R.RETRY_BACKOFF))

    def test_pg_connect_recovers(self):
        n = []

        def connect(*a, **k):
            n.append(1)
            if len(n) < 2:
                raise OSError("blip")
            return types.SimpleNamespace(autocommit=False)
        with mock.patch.dict(sys.modules, {"psycopg2": types.SimpleNamespace(connect=connect)}), _quiet():
            self.assertTrue(R._connect("dsn", _sleep=lambda s: None).autocommit)

    def test_claude_goes_through_the_retrying_helper_with_sonnet(self):
        seen = {}
        nj = types.SimpleNamespace(call_openrouter=lambda s, u, **k: seen.update(k) or "[]")
        with mock.patch.dict(sys.modules, {"nova_journal": nj}):
            self.assertEqual(R.ask_claude("s", "u"), "[]")
        self.assertEqual(seen["model"], "sonnet")
        import nova_journal
        self.assertIn("_attempts = 3", Path(nova_journal.__file__).read_text())

    def test_claude_failure_holds_everything_loudly(self):
        nj = types.SimpleNamespace(call_openrouter=mock.Mock(side_effect=RuntimeError("boom")))
        with mock.patch.dict(sys.modules, {"nova_journal": nj}), redirect_stdout(io.StringIO()) as buf:
            out = R.decide([_item()], "")
        self.assertEqual(out["P1"], {"decision": "HOLD", "source": "error",
                                     "reason": "reviewer unavailable (claude -p failed after retries)"})
        self.assertIn("claude call failed", buf.getvalue())

    def test_slack_summary_retries(self):
        n = []

        def post_both(msg, slack_channel=None):
            n.append(slack_channel)
            if len(n) < 3:
                raise OSError("slack 503")
        cfg = types.SimpleNamespace(post_both=post_both, SLACK_CHAN="C0AMNQ5GX70")
        with mock.patch.dict(sys.modules, {"nova_config": cfg}), mock.patch.object(co.time, "sleep", lambda s: None), _quiet():
            R.notify("hi")
        self.assertEqual(n, ["C0AMNQ5GX70"] * 3)


class TestUnit(unittest.TestCase):
    def test_parse_accepts_prose_wrapped_array_and_lowercase(self):
        out = R.parse_decisions('Sure:\n[{"id":"P1","decision":"approve","reason":" fine  idea "}]\nthanks', [_item()])
        self.assertEqual(out["P1"], {"decision": "APPROVE", "reason": "fine idea", "source": "claude"})

    def test_parse_malformed_variants_hold(self):
        for raw in ("not json", "[", '{"id":"P1"}', '[{"id":"P1","decision":"MAYBE","reason":"x"}]',
                    '[{"id":"P1","decision":"APPROVE","reason":""}]', '[{"id":"P1","decision":"APPROVE"}]', "[1,2]"):
            self.assertEqual(R.parse_decisions(raw, [_item()])["P1"]["decision"], "HOLD", raw)

    def test_floor_lets_ordinary_requests_through(self):
        for t in ("increase log verbosity for llm-ping", "Presence — feel the weight of what matters",
                  "Pursue Interest: Crime Drama — I'm a fan of TV procedurals"):
            self.assertIsNone(R.floor(_item(kind="wish", title=t)), t)

    def test_safe_monitor_restart_is_not_a_device(self):
        self.assertIsNone(R.floor(_item(title="restart nova-battery-monitor", target="nova-battery-monitor")))
        self.assertIsNotNone(R.floor(_item(title="restart the garage battery", target=None)))

    def test_enabled_default_true(self):
        s = types.SimpleNamespace(kill_switch_engaged=lambda oc: False)
        with mock.patch.dict(sys.modules, {"nova_autonomy_safety": s}):
            self.assertTrue(R.enabled(_Cur([("claude_reviewer", None)])))
            self.assertTrue(R.enabled(_Cur([("claude_reviewer", (True,))])))

    def test_summary_lists_every_decision(self):
        txt = R.summary([(_item(), {"decision": "APPROVE", "reason": "r"}, True, "ok"),
                         (_item(id="P121", title="av"), {"decision": "HOLD", "reason": "device"}, True, "held")])
        self.assertIn("✅ APPROVE coagency P1", txt)
        self.assertIn("⏸ HOLD coagency P121", txt)
        self.assertIn("claude_reviewer/enabled", txt)


class TestIntegration(unittest.TestCase):
    def test_coagency_approve_goes_through_mode_decide(self):
        cur = _Cur([("SELECT value FROM service_config WHERE service='coagency'", ('"live"',))])
        with mock.patch.object(co, "mode_decide", return_value=0) as md, _quiet():
            ok, res = R.act(cur, _item(), {"decision": "APPROVE", "reason": "useful"})
        self.assertTrue(ok)
        a = md.call_args
        self.assertEqual(a.args[2:6], (1, "approve", "auto-approved under Jordan standing order 2026-10-08: useful",
                                       "claude-reviewer"))
        self.assertEqual(a.kwargs["expect_status"], "pending_human")

    def test_coagency_hold_stays_pending_with_note(self):
        cur = _Cur()
        ok, res = R.act(cur, _item(), {"decision": "HOLD", "reason": "device"})
        sql, p = cur.executed("UPDATE coagency_proposals SET decision_note")[0]
        self.assertIn("status='pending_human'", sql)
        self.assertIn("HOLD for Jordan: device", p[0])
        self.assertNotIn("status=", sql.split("WHERE")[0])

    def test_mode_decide_refuses_already_decided(self):
        cur = _Cur([("SELECT status, redline_pass", ("approved", True, {}))])
        with _quiet():
            self.assertEqual(co.mode_decide(cur, "live", 5, "approve", "n", "claude-reviewer", "pending_human"), 1)
        self.assertEqual(cur.executed("UPDATE coagency_proposals"), [])

    def test_hand_to_claude_names_the_real_approver(self):
        cur = _Cur([("SELECT origin, rationale, decided_by", ("growth", "r", "claude-reviewer")),
                    ("FROM claude_queue WHERE description LIKE", None), ("FROM claude_sessions", ("s",)),
                    ("INSERT INTO claude_queue", (9,))])
        self.assertEqual(co.hand_to_claude(cur, 5, "do x"), 9)
        self.assertIn("approved by claude-reviewer", cur.executed("INSERT INTO claude_queue")[0][1][2])

    def test_wish_approve_uses_approve_wish(self):
        with mock.patch.object(asp, "approve_wish", return_value=77) as aw:
            ok, res = R.act(_Cur(), _item(kind="wish", ref=5), {"decision": "APPROVE", "reason": "r"})
        self.assertEqual((ok, res), (True, "claude_queue #77"))
        self.assertIn("claude-reviewer", aw.call_args.kwargs["by"])

    def test_approve_wish_twice_queues_once(self):
        cur = _Cur([("FROM feature_wishes WHERE id", ("Mic", "d", "w", "s", "acknowledged"))])
        with _quiet():
            self.assertIsNone(asp.approve_wish(cur, 70))
        self.assertEqual(cur.executed("INSERT INTO claude_queue"), [])
        self.assertEqual(cur.executed("UPDATE feature_wishes"), [])

    def test_approve_wish_existing_build_item_is_a_noop(self):
        cur = _Cur([("FROM feature_wishes WHERE id", ("Mic", "d", "w", "s", "wished")),
                    ("SELECT id FROM claude_queue", (3325,))])
        with _quiet():
            self.assertIsNone(asp.approve_wish(cur, 70))
        self.assertEqual(cur.executed("INSERT INTO claude_queue"), [])

    def test_approve_wish_concurrent_claim_loses(self):
        cur = _Cur([("FROM feature_wishes WHERE id", ("Mic", "d", "w", "s", "wished"))], rowcount=0)
        with _quiet():
            self.assertIsNone(asp.approve_wish(cur, 70))
        self.assertIn("AND status='wished'", cur.executed("UPDATE feature_wishes")[0][0])
        self.assertEqual(cur.executed("INSERT INTO claude_queue"), [])

    def test_approve_wish_queue_failure_reverts_status(self):
        cur = _Cur([("FROM feature_wishes WHERE id", ("Mic", "d", "w", "s", "wished")),
                    ("INSERT INTO claude_queue", RuntimeError("pg"))])
        with _quiet(), self.assertRaises(RuntimeError):
            asp.approve_wish(cur, 70)
        self.assertIn("SET status='wished'", cur.executed("UPDATE feature_wishes")[-1][0])

    def test_skill_approve_queues_once_and_marks_approved(self):
        cur = _Cur([("FROM claude_queue WHERE description LIKE", None), ("FROM claude_sessions", None),
                    ("INSERT INTO claude_queue", (12,))])
        ok, res = R.act(cur, _item(kind="skill", ref="pursue-x", title="Pursue X"), {"decision": "APPROVE", "reason": "r"})
        self.assertEqual((ok, res), (True, "claude_queue #12"))
        self.assertTrue(cur.executed("INSERT INTO claude_queue")[0][1][1].startswith("Adopt Nova's skill 'pursue-x'"))
        self.assertIn("status='approved'", cur.executed("UPDATE nova_skills")[0][0])
        cur = _Cur([("FROM claude_queue WHERE description LIKE", (12,))])
        self.assertEqual(R.act(cur, _item(kind="skill", ref="pursue-x"), {"decision": "APPROVE", "reason": "r"})[0], False)

    def test_lockbox_approve_boxes_and_decline_keeps(self):
        rel = types.SimpleNamespace(box=mock.Mock(return_value=True))
        mc = mock.Mock()
        cur = _Cur([("FROM lockbox WHERE id", (1,))])
        it = _item(kind="lockbox", ref=1, memory_id="mem-1", box_reason="painful")
        with mock.patch.dict(sys.modules, {"nova_relationship": rel}):
            ok, res = R.act(cur, it, {"decision": "APPROVE", "reason": "r"}, mem_conn_factory=lambda: mc)
        self.assertTrue(ok)
        self.assertEqual(rel.box.call_args.kwargs["by"], "claude-reviewer")
        mc.close.assert_called_once()
        cur = _Cur()
        R.act(cur, it, {"decision": "DECLINE", "reason": "keep it"})
        self.assertIn("status='declined'", cur.executed("UPDATE lockbox")[0][0])


class TestFunctional(unittest.TestCase):
    def _run(self, cur, ask, dry=False):
        posts = []
        s = types.SimpleNamespace(kill_switch_engaged=lambda oc: False)
        with mock.patch.dict(sys.modules, {"nova_autonomy_safety": s}), \
                mock.patch.object(R, "act", side_effect=lambda oc, it, d: (True, "done")) as act, _quiet():
            rc = R.run(dry_run=dry, oc=cur, mem_cur=_Cur([("FROM memories", ("conversation", "text"))]),
                       ask=ask, notifier=posts.append)
        return rc, posts, act

    def test_backlog_run_decides_logs_and_posts_once(self):
        cur = _backlog()
        rc, posts, act = self._run(cur, _ask({"P7": "APPROVE", "W5": "DECLINE", "L1": "DECLINE"}))
        self.assertEqual(rc, 0)
        logs = {p[2]: p[4] for _, p in cur.executed("INSERT INTO claude_reviewer_log")}
        self.assertEqual(logs["121"], "HOLD")
        self.assertEqual(logs["7"], "APPROVE")
        self.assertEqual(logs["5"], "DECLINE")
        self.assertEqual(logs["1"], "DECLINE")
        self.assertEqual(len(posts), 1)
        self.assertIn("P121", posts[0])
        self.assertEqual(act.call_count, 5)

    def test_dry_run_acts_on_nothing_and_stays_quiet(self):
        cur = _backlog()
        rc, posts, act = self._run(cur, _ask({}), dry=True)
        act.assert_not_called()
        self.assertEqual(posts, [])
        self.assertTrue(all(p[-1] is True for _, p in cur.executed("INSERT INTO claude_reviewer_log")))

    def test_empty_backlog_posts_nothing(self):
        cur = _Cur([("FROM service_config", None)])
        rc, posts, act = self._run(cur, _ask({}))
        self.assertEqual((rc, posts), (0, []))

    def test_outage_only_run_posts_nothing(self):
        cur = _backlog([("FROM coagency_proposals WHERE status='pending_human'",
                         [(7, "o", "increase log verbosity for llm-ping", "", None, True, {"allowed": True})]),
                        ("FROM feature_wishes\n                  WHERE status='wished'", []),
                        ("FROM nova_skills s", []), ("FROM lockbox WHERE status='proposed' ORDER BY id", [])])
        rc, posts, act = self._run(cur, lambda s, u: None)
        self.assertEqual(posts, [])
        self.assertEqual(cur.executed("INSERT INTO claude_reviewer_log")[0][1][6], "error")

    def test_approval_cap_per_run(self):
        rows = [(i, "o", f"increase log verbosity for job{i}", "", None, True, {"allowed": True}) for i in range(10)]
        cur = _backlog([("FROM coagency_proposals WHERE status='pending_human'", rows),
                        ("FROM feature_wishes\n                  WHERE status='wished'", []),
                        ("FROM nova_skills s", []), ("FROM lockbox WHERE status='proposed' ORDER BY id", [])])
        rc, posts, act = self._run(cur, _ask({}))
        self.assertEqual(act.call_count, R.MAX_APPROVALS)

    def test_disabled_reviews_nothing(self):
        cur = _Cur([("claude_reviewer", ('"false"',))])
        rc, posts, act = self._run(cur, _ask({}))
        self.assertEqual(cur.executed("coagency_proposals"), [])
        self.assertEqual(posts, [])


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_claude_reviewer.py"), "--selftest"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest ok", r.stdout)

    def test_help_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_claude_reviewer.py"), "--help"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_public_surface(self):
        for name in ("run", "gather", "decide", "act", "floor", "parse_decisions", "enabled", "ensure_schema"):
            self.assertTrue(callable(getattr(R, name)), name)
        self.assertEqual(R.BY, co.REVIEWER_BY)

    def test_schema_creates_log_table(self):
        cur = _Cur()
        R.ensure_schema(cur)
        self.assertTrue(cur.executed("CREATE TABLE IF NOT EXISTS claude_reviewer_log"))


if __name__ == "__main__":
    unittest.main()
