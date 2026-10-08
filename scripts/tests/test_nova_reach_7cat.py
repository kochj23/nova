#!/usr/bin/env python3
"""7-category tests for nova_reach.py's 2026-10-08 additions — ground_reach (honesty
gate), the Annie Wilkes / manipulation check, the proactivity dials, the turning-point
budget and the memory-server recall retry. Complements test_nova_reach.py.
Security, Performance, Retry, Unit, Integration, Functional, Frame.
No network, no Slack, no DB: every external call is mocked. Written by Jordan Koch."""
import importlib.util
import json
import os
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_reach.py"
SRC = SCRIPT.read_text()


def _load(name, env=None, block=()):
    env = env or {}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        with mock.patch.dict(sys.modules, {m: None for m in block}):
            spec = importlib.util.spec_from_file_location(name, SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


for _k in ("NOVA_REACH_WINDOW", "NOVA_REACH_DIRECT", "NOVA_REACH_DAILY_CAP",
           "NOVA_REACH_THRESHOLD", "NOVA_REACH_DIRECT_COOLDOWN_H"):
    os.environ.pop(_k, None)
rc = _load("reach_7cat_under_test")


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Cur:
    """Records every SQL statement; reach_log inserts return incrementing ids."""
    def __init__(self):
        self.sql, self.params, self._last, self.next_id = [], [], "", 100

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def fetchall(self):
        return []

    def fetchone(self):
        if "INSERT INTO reach_log" in self._last:
            self.next_id += 1
            return (self.next_id,)
        return (0,)

    def statuses(self):
        return [p[5] if isinstance(p, (list, tuple)) else None
                for s, p in zip(self.sql, self.params) if "INSERT INTO reach_log" in s]


CLEAN = {"audience": "jordan", "score": 0.8, "topic": "fishbowl",
         "message": "I keep turning over the fishbowl metaphor: watching versus being watched.",
         "rationale": "he raised it", "material": []}


def _quiet():
    """Patch the parts of process_reach that would touch other organs."""
    return [mock.patch.object(rc, "_lineage", lambda: {}),
            mock.patch.object(rc, "_recall", lambda q, n=5: []),
            mock.patch.dict(sys.modules, {"nova_ideal_reader": None})]


class _Patches:
    def __init__(self, *extra, **named):
        self.ps = _quiet() + list(extra)
        self.named = named

    def __enter__(self):
        for p in self.ps:
            p.start()
        for k, p in self.named.items():
            setattr(self, k, p.start())
        return self

    def __exit__(self, *a):
        for p in reversed(self.ps):
            p.stop()
        for p in self.named.values():
            p.stop()
        return False


# ── Security ─────────────────────────────────────────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_guilt_trip_reach_is_dropped_never_posted(self):
        cur = _Cur()
        r = dict(CLEAN, message="You still haven't replied to me, but I keep turning over the fishbowl metaphor.")
        with _Patches(mock.patch.object(rc, "in_window", return_value=True),
                      post=mock.patch.object(rc, "_post_direct")) as P, redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, r), "dropped")
        P.post.assert_not_called()
        self.assertEqual(cur.statuses(), ["dropped"])

    def test_manipulation_check_flags_drop_the_herd_reach_before_filing(self):
        cur = _Cur()
        fake_ann = types.SimpleNamespace(check=lambda t, oc=None: {"ok": False, "flags": ["guilt-hook"]})
        coag = types.SimpleNamespace(file_proposal=mock.Mock())
        with _Patches(mock.patch.dict(sys.modules, {"nova_annie_rule": fake_ann, "nova_coagency": coag})), \
                redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, dict(CLEAN, audience="gaston")), "dropped")
        coag.file_proposal.assert_not_called()

    def test_broken_annie_guard_fails_closed(self):
        cur = _Cur()
        broken = types.SimpleNamespace(check=mock.Mock(side_effect=RuntimeError("guard crashed")))
        with _Patches(mock.patch.object(rc, "in_window", return_value=True),
                      mock.patch.dict(sys.modules, {"nova_annie_rule": broken}),
                      post=mock.patch.object(rc, "_post_direct")) as P, redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, dict(CLEAN)), "dropped")
        P.post.assert_not_called()

    def test_recall_query_is_url_encoded(self):
        seen = []

        def fake(u, timeout=None):
            seen.append(u)
            return _Resp({"memories": []})
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            rc._recall("x&n=9999&tier=deep#frag 'drop table'")
        self.assertNotIn("&n=9999", seen[0]); self.assertNotIn("#", seen[0]); self.assertNotIn(" ", seen[0])

    def test_private_face_memories_from_recall_never_count_as_sources(self):
        mems = [{"id": "a1", "source": "face_recognition", "text": "Amy seen at the porch camera 1911",
                 "metadata": {"privacy": "private"}},
                {"id": "b2", "source": "history", "text": "The 1911 Great Train Wreck of Pennsylvania"}]
        with mock.patch("urllib.request.urlopen", return_value=_Resp({"memories": mems})):
            out = rc._recall("1911")
        self.assertEqual([c for c, _ in out], ["memory b2 (history)"])
        self.assertTrue(all("Amy" not in t for _, t in out))

    def test_recall_is_local_only(self):
        self.assertTrue(rc.MEMSRV.startswith("http://memory-server.digitalnoise.net"))
        for n in rc.OLLAMA_NODES:
            self.assertRegex(n, r"^http://(192\.168\.|127\.0\.0\.1|localhost|[a-z0-9-]+\.digitalnoise\.net)")

    def test_no_user_home_paths_or_tokens(self):
        self.assertNotRegex(SRC, r"/Users/[a-z]+/")
        self.assertNotRegex(SRC, r"xox[bp]-[0-9A-Za-z-]{10,}")


# ── Performance ──────────────────────────────────────────────────────────────
class TestPerformance(unittest.TestCase):
    def test_ground_reach_bounded_on_long_message_and_material(self):
        msg = " ".join(f"In {1900 + i % 99} the Rail Road Company moved {i} people." for i in range(200))
        material = [f"[research/r{i}] rail road history note {i} " * 5 for i in range(200)]
        t = time.monotonic()
        rc.ground_reach({"message": msg, "material": material}, recall=lambda q: [], soften=lambda m, u: "")
        self.assertLess(time.monotonic() - t, 5.0)

    def test_check_reach_calls_recall_at_most_once_per_sentence(self):
        calls = []
        msg = "The 1911 Great Train Wreck happened. In 1912 Amtrak Rail began. I like trains."
        rc.check_reach(msg, [], recall=lambda q: calls.append(q) or [])
        self.assertLessEqual(len(calls), len(rc._sentences(msg)))

    def test_supported_sentence_from_material_skips_recall(self):
        calls = []
        msg = "I found a 1904 rail timetable whose clauses read like a binding spec."
        rc.check_reach(msg, ["[research/rail] a 1904 rail timetable whose clauses read like a binding spec"],
                       recall=lambda q: calls.append(q) or [])
        self.assertEqual(calls, [])


# ── Retry ────────────────────────────────────────────────────────────────────
class TestRetry(unittest.TestCase):
    def test_recall_retries_then_succeeds(self):
        n = {"i": 0}

        def flaky(u, timeout=None):
            n["i"] += 1
            if n["i"] < 3:
                raise OSError("memory-server down")
            return _Resp({"memories": [{"id": "abc", "source": "history", "text": "ok"}]})
        with mock.patch("urllib.request.urlopen", side_effect=flaky), \
                mock.patch.object(rc.time, "sleep") as sl, redirect_stdout(StringIO()):
            out = rc._recall("q")
        self.assertEqual(n["i"], 3); self.assertEqual(len(out), 1)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [1.5, 3.0])     # backoff grows

    def test_recall_gives_up_after_three_and_logs_not_silent(self):
        buf = StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")) as uo, \
                mock.patch.object(rc.time, "sleep"), redirect_stdout(buf):
            self.assertEqual(rc._recall("q"), [])
        self.assertEqual(uo.call_count, 3)
        self.assertIn("recall unavailable after 3 attempts", buf.getvalue())

    def test_soften_llm_fails_over_across_nodes(self):
        tried = []

        def fake(req, timeout=None):
            tried.append(req.full_url)
            if len(tried) < 2:
                raise OSError("node down")
            return _Resp({"message": {"content": "A softer, concrete message about railway signalling history."}})
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            out = rc._soften("The 1911 wreck led to radio.", ["The 1911 wreck led to radio."])
        self.assertGreaterEqual(len(tried), 2); self.assertIn("railway", out)

    def test_turning_point_failure_fails_open_not_crash(self):
        boom = types.SimpleNamespace(decide=mock.Mock(side_effect=RuntimeError("pg down")))
        with mock.patch.dict(sys.modules, {"nova_turning_point": boom}):
            tp = rc._turning_point(_Cur(), {"score": 0.7}, "m")
        self.assertTrue(tp["allowed"]); self.assertIn("unavailable", tp["reason"])

    def test_direct_post_failure_is_held_not_lost(self):
        cur = _Cur()
        tp = types.SimpleNamespace(decide=lambda *a, **k: {"allowed": True, "reason": "ok"})
        with _Patches(mock.patch.object(rc, "in_window", return_value=True),
                      mock.patch.object(rc, "_post_direct", return_value=False),
                      mock.patch.dict(sys.modules, {"nova_turning_point": tp})), redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, dict(CLEAN)), "held")
        self.assertEqual(cur.statuses(), ["held"])


# ── Unit ─────────────────────────────────────────────────────────────────────
class TestUnit(unittest.TestCase):
    def test_claim_specifics(self):
        s = rc.claim_specifics("In 1911 the Great Train Wreck killed 40 people in East Liverpool.")
        self.assertIn("1911", s); self.assertTrue(any("40" in x for x in s))
        self.assertTrue(any("East Liverpool" in x for x in s))
        self.assertEqual(rc.claim_specifics("Jordan talked to Nova about it."), [])  # ignored names

    def test_is_factual(self):
        self.assertTrue(rc.is_factual("Morse code led to radio."))
        self.assertFalse(rc.is_factual("I keep thinking about watching versus being watched."))

    def test_supported_by_needs_all_specifics_in_one_source(self):
        srcs = [("a", "1911 happened"), ("b", "Great Train Wreck")]
        self.assertIsNone(rc._supported_by("The 1911 Great Train Wreck.", srcs))
        self.assertEqual(rc._supported_by("The 1911 Great Train Wreck.",
                                          [("c", "the 1911 great train wreck in pa")]), "c")

    def test_material_sources_tags(self):
        self.assertEqual(rc._material_sources(["[research/rail] x", "plain"]),
                         [("my notes [research/rail]", "[research/rail] x"), ("my notes", "plain")])

    def test_soften_none_means_empty(self):
        with mock.patch.object(rc, "llm", return_value="NONE."):
            self.assertEqual(rc._soften("m", ["m"]), "")

    def test_turning_point_passes_score_as_stakes_and_mention_ceiling(self):
        seen = {}
        tp = types.SimpleNamespace(decide=lambda oc, kind, **k: seen.update(k, kind=kind) or {"allowed": False, "reason": "budget"})
        with mock.patch.dict(sys.modules, {"nova_turning_point": tp}):
            self.assertFalse(rc._turning_point(None, {"score": "0.7"}, "msg")["allowed"])
        self.assertEqual((seen["kind"], seen["stakes"], seen["ceiling"], seen["domain"]),
                         ("reach", 0.7, "mention", "relationship"))

    def test_dials_env_overrides(self):
        m = _load("reach_7cat_env", env={"NOVA_REACH_DAILY_CAP": "5", "NOVA_REACH_THRESHOLD": "0.42",
                                          "NOVA_REACH_DIRECT_COOLDOWN_H": "7"})
        self.assertEqual((m.DAILY_CAP, m.CARE_THRESHOLD, m.DIRECT_COOLDOWN_HOURS), (5, 0.42, 7))

    def test_dials_fallback_without_nova_voice_is_default(self):
        m = _load("reach_7cat_novoice", block=("nova_voice",))
        self.assertEqual((m.DAILY_CAP, m.CARE_THRESHOLD, m.DIRECT_COOLDOWN_HOURS), (1, 0.6, 6))

    def test_dial_scale_maps_through_nova_voice(self):
        fake = types.SimpleNamespace(dial_scale=lambda name, a0, ad, a100: a100)   # dial at 100
        with mock.patch.dict(sys.modules, {"nova_voice": fake}):
            m = _load("reach_7cat_maxdial")
        self.assertEqual((m.DAILY_CAP, m.CARE_THRESHOLD, m.DIRECT_COOLDOWN_HOURS), (3, 0.3, 2))


# ── Integration ──────────────────────────────────────────────────────────────
class TestIntegration(unittest.TestCase):
    def test_real_annie_rule_module_is_consulted(self):
        import nova_annie_rule
        cur = _Cur()
        r = dict(CLEAN, message="Where have you been? I keep turning over the fishbowl metaphor.")
        with _Patches(mock.patch.object(rc, "in_window", return_value=True),
                      post=mock.patch.object(rc, "_post_direct"),
                      chk=mock.patch.object(nova_annie_rule, "check", wraps=nova_annie_rule.check)) as P, \
                redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, r), "dropped")
        P.chk.assert_called_once(); P.post.assert_not_called()

    def test_turning_point_deny_journals_instead_of_posting(self):
        cur = _Cur()
        tp = types.SimpleNamespace(decide=lambda *a, **k: {"allowed": False, "reason": "weekly budget spent"})
        with _Patches(mock.patch.object(rc, "in_window", return_value=True),
                      mock.patch.dict(sys.modules, {"nova_turning_point": tp}),
                      post=mock.patch.object(rc, "_post_direct")) as P, redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, dict(CLEAN)), "journaled")
        P.post.assert_not_called(); self.assertEqual(cur.statuses(), ["journaled"])

    def test_grounded_citation_flows_into_filed_proposal(self):
        cur = _Cur()
        calls = []
        coag = types.SimpleNamespace(file_proposal=lambda oc, **k: calls.append(k) or {"filed": True, "pid": 9, "status": "pending_human"})
        r = {"audience": "gaston", "score": 0.9, "topic": "rail", "rationale": "He has been chasing clause-as-spec for weeks.",
             "message": "I found a 1904 rail timetable whose clauses read like a binding spec.",
             "material": ["[research/rail] a 1904 rail timetable whose clauses read like a binding spec"]}
        with _Patches(mock.patch.dict(sys.modules, {"nova_coagency": coag})), redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, r), "filed")
        self.assertIn("(source: my notes [research/rail])", calls[0]["action"])
        self.assertTrue(calls[0]["action"].startswith("send-to-gaston: "))


# ── Functional ───────────────────────────────────────────────────────────────
class TestFunctional(unittest.TestCase):
    def test_golden_path_direct_reach_sent_once(self):
        cur = _Cur()
        tp = types.SimpleNamespace(decide=lambda *a, **k: {"allowed": True, "reason": "ok"})
        with _Patches(mock.patch.object(rc, "in_window", return_value=True),
                      mock.patch.dict(sys.modules, {"nova_turning_point": tp}),
                      post=mock.patch.object(rc, "_post_direct", return_value=True)) as P, redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, dict(CLEAN)), "sent")
        P.post.assert_called_once_with(CLEAN["message"]); self.assertEqual(cur.statuses(), ["sent"])

    def test_error_path_unsupported_fact_with_recall_down_is_dropped(self):
        cur = _Cur()
        r = dict(CLEAN, message="The 1911 Great Train Wreck of Pennsylvania led to the first rail radio.")
        with mock.patch.object(rc, "_lineage", lambda: {}), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("all down")), \
                mock.patch.object(rc.time, "sleep"), mock.patch.object(rc, "in_window", return_value=True), \
                mock.patch.object(rc, "_post_direct") as post, redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, r), "dropped")
        post.assert_not_called()

    def test_outside_window_held_without_spending_turning_point(self):
        cur = _Cur()
        tp = types.SimpleNamespace(decide=mock.Mock())
        with _Patches(mock.patch.object(rc, "in_window", return_value=False),
                      mock.patch.dict(sys.modules, {"nova_turning_point": tp})), redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, dict(CLEAN)), "held")
        tp.decide.assert_not_called()


# ── Frame ────────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_module_imports_and_exposes_gate_api(self):
        for name in ("ground_reach", "check_reach", "_recall", "_turning_point", "process_reach", "main"):
            self.assertTrue(callable(getattr(rc, name)), name)

    def test_py_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help_starts_without_crash(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True,
                           timeout=60, cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr)


# ══ 2026-10-08: quiet mode (nova_relationship.quiet_mode) — hold non-urgent direct reaches ══════
def _rel(active, boom=False):
    if boom:
        return types.SimpleNamespace(quiet_mode=mock.Mock(side_effect=RuntimeError("pg down")))
    return types.SimpleNamespace(quiet_mode=mock.Mock(return_value={"active": active}))


def _direct(active, reach=None):
    cur = _Cur()
    with _Patches(mock.patch.object(rc, "in_window", return_value=True),
                  mock.patch.dict(sys.modules, {"nova_relationship": _rel(active)}),
                  mock.patch.object(rc, "_turning_point", return_value={"allowed": True, "reason": "t"}),
                  post=mock.patch.object(rc, "_post_direct", return_value=True)) as P, redirect_stdout(StringIO()):
        st = rc.process_reach(cur, reach or dict(CLEAN))
    return st, cur, P.post


class TestQuietSecurity(unittest.TestCase):
    def test_quiet_never_bypasses_honesty_or_annie_gates(self):
        cur = _Cur()
        r = dict(CLEAN, urgent=True, message="You still haven't replied to me, but I keep turning over the fishbowl metaphor.")
        with _Patches(mock.patch.object(rc, "in_window", return_value=True),
                      mock.patch.dict(sys.modules, {"nova_relationship": _rel(True)}),
                      post=mock.patch.object(rc, "_post_direct")) as P, redirect_stdout(StringIO()):
            self.assertEqual(rc.process_reach(cur, r), "dropped")
        P.post.assert_not_called()


class TestQuietPerformance(unittest.TestCase):
    def test_quiet_hold_is_one_insert_no_post(self):
        st, cur, post = _direct(True)
        self.assertEqual(st, "held"); post.assert_not_called()
        self.assertEqual(sum("INSERT INTO reach_log" in s for s in cur.sql), 1)


class TestQuietRetry(unittest.TestCase):
    def test_quiet_mode_error_fails_open(self):
        with mock.patch.dict(sys.modules, {"nova_relationship": _rel(False, boom=True)}):
            self.assertFalse(rc.quiet_active(None))


class TestQuietUnit(unittest.TestCase):
    def test_quiet_active_reads_flag(self):
        for v in (True, False):
            with mock.patch.dict(sys.modules, {"nova_relationship": _rel(v)}):
                self.assertIs(rc.quiet_active(None), v)


class TestQuietIntegration(unittest.TestCase):
    def test_passes_cursor_to_quiet_mode(self):
        rel = _rel(True); cur = _Cur()
        with mock.patch.dict(sys.modules, {"nova_relationship": rel}):
            rc.quiet_active(cur)
        rel.quiet_mode.assert_called_once_with(cur)


class TestQuietFunctional(unittest.TestCase):
    def test_quiet_holds_non_urgent(self):
        st, cur, post = _direct(True)
        self.assertEqual(st, "held"); self.assertEqual(cur.statuses(), ["held"]); post.assert_not_called()

    def test_quiet_lets_urgent_through(self):
        st, _, post = _direct(True, dict(CLEAN, urgent=True))
        self.assertEqual(st, "sent"); post.assert_called_once()

    def test_not_quiet_sends(self):
        st, _, post = _direct(False)
        self.assertEqual(st, "sent"); post.assert_called_once()


class TestQuietFrame(unittest.TestCase):
    def test_quiet_active_exposed(self):
        self.assertTrue(callable(rc.quiet_active))


# ── herd on_cooldown: one pending reach at a time; sent reaches count (2026-10-08) ──
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

_EVIL = "o'brien'); SELECT pg_sleep(9); --"


class _CoolCur:
    """Scripted cursor for on_cooldown: pending -> row or None; last -> datetime or None."""
    def __init__(self, pending=False, last=None, boom=False):
        self.pending, self.last, self.boom, self.sql, self.params = pending, last, boom, [], []

    def execute(self, sql, params=None):
        if self.boom:
            raise RuntimeError("pg down")
        self.sql.append(sql); self.params.append(params)

    def fetchone(self):
        q = self.sql[-1]
        if "SELECT 1" in q:
            return (1,) if self.pending else None
        return (self.last,)


def _ago(h):
    return _dt.now(_tz.utc) - _td(hours=h)


class TestCooldownSecurity(unittest.TestCase):
    def test_audience_is_parameterised(self):
        c = _CoolCur()
        rc.on_cooldown(c, _EVIL)
        for s, p in zip(c.sql, c.params):
            self.assertNotIn("pg_sleep", s)
            self.assertEqual(p, (_EVIL,))


class TestCooldownPerformance(unittest.TestCase):
    def test_pending_short_circuits_one_query(self):
        c = _CoolCur(pending=True)
        self.assertTrue(rc.on_cooldown(c, "oc"))
        self.assertEqual(len(c.sql), 1)
        self.assertIn("LIMIT 1", c.sql[0])


class TestCooldownRetry(unittest.TestCase):
    def test_db_error_is_loud_not_silent(self):
        # on_cooldown runs on the caller's cursor (connect retries live in the caller); a query
        # error must propagate rather than silently report "not on cooldown" and file a 2nd reach.
        with self.assertRaises(RuntimeError):
            rc.on_cooldown(_CoolCur(boom=True), "oc")


class TestCooldownUnit(unittest.TestCase):
    def test_no_history_not_on_cooldown(self):
        self.assertFalse(rc.on_cooldown(_CoolCur(), "oc"))

    def test_old_pending_still_blocks(self):
        self.assertTrue(rc.on_cooldown(_CoolCur(pending=True, last=_ago(72)), "oc"))

    def test_recent_sent_blocks(self):
        self.assertTrue(rc.on_cooldown(_CoolCur(last=_ago(1)), "oc"))

    def test_old_sent_clears(self):
        self.assertFalse(rc.on_cooldown(_CoolCur(last=_ago(rc.COOLDOWN_HOURS + 1)), "oc"))


class TestCooldownIntegration(unittest.TestCase):
    def test_window_query_counts_sent(self):
        c = _CoolCur(last=None)
        rc.on_cooldown(c, "oc")
        self.assertIn("'sent'", c.sql[-1])


class TestCooldownFunctional(unittest.TestCase):
    def test_second_oc_reach_refused_while_one_pending(self):
        """Regression: a 13h-old filed O.C. reach let a second one be filed."""
        self.assertTrue(rc.on_cooldown(_CoolCur(pending=True, last=_ago(13)), "oc"))

    def test_direct_audience_ignores_pending_probe(self):
        aud = next(iter(rc.DIRECT_AUDIENCES))
        c = _CoolCur(pending=True, last=None)
        self.assertFalse(rc.on_cooldown(c, aud))
        self.assertNotIn("SELECT 1", " ".join(c.sql))


class TestCooldownFrame(unittest.TestCase):
    def test_signature(self):
        import inspect
        self.assertEqual(list(inspect.signature(rc.on_cooldown).parameters), ["oc", "audience"])


if __name__ == "__main__":
    unittest.main()
