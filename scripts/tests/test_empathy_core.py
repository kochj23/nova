#!/usr/bin/env python3
"""Tests for nova_empathy_core.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame) plus the wish #67 privacy/regression/docs checks, and the
2026-10-09 M6 merge (the Jordan lens: empathy, hold, quiet, insight sections over one read).
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import re
import sys
import time
import unittest
import urllib.request
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

# Offline guard for the whole file: no real PG, no real memory server, ever (a test that forgets a
# patch fails loudly instead of writing into Nova's memory). Tests patch over these where they need a fake.
_GUARDS = []


def _offline(*a, **k):
    raise RuntimeError("offline test: real PG / HTTP blocked")


def setUpModule():
    for target in (patch.object(psycopg2, "connect", _offline), patch.object(urllib.request, "urlopen", _offline)):
        target.start(); _GUARDS.append(target)


def tearDownModule():
    while _GUARDS:
        _GUARDS.pop().stop()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ec = _load("ec", SCRIPTS / "nova_empathy_core.py")
SRC = (SCRIPTS / "nova_empathy_core.py").read_text()
TODAY = date(2026, 10, 5)


def _rows(topic, days, resp="fine"):
    return [(date(2026, 9, d), f"Nova, what about the {topic}?", resp) for d in days]


class TestFunctional(unittest.TestCase):
    def test_selftest_passes(self):
        ec.demo()

    def test_weight_is_returning_not_counting(self):
        # 10 mentions on one day weigh nothing; 3 mentions on 3 days weigh something
        one_day = [(date(2026, 9, 1), "the zigbee unit again", "ok")] * 10
        three_days = _rows("zigbee", (1, 2, 3))
        self.assertEqual(ec.weigh(one_day, TODAY), [])
        self.assertEqual(ec.weigh(three_days, TODAY)[0]["returns"], 3)

    def test_brushoff_counted_against_the_topic(self):
        rows = _rows("printer", (1, 2, 3), resp="No memory of that, stop asking")
        self.assertEqual(ec.weigh(rows, TODAY)[0]["brushoffs"], 3)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_read_only_over_the_world(self):
        body = SRC[SRC.index("def gather"):SRC.index("def section_empathy")]   # gather + the shared read
        for verb in ("UPDATE", "DELETE", "INSERT"):
            self.assertNotIn(verb, body)

    def test_shared_read_is_parameterized(self):
        body = SRC[SRC.index("def read_shared"):SRC.index("def section_empathy")]
        self.assertNotIn('execute(f"', body)
        self.assertIn("NOT IN %s", body)
        self.assertIn("make_interval(days => %s)", body)

    def test_section_names_are_an_allowlist(self):
        self.assertEqual(list(ec.SECTIONS), ["empathy", "hold", "quiet", "insight"])
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), patch("sys.stderr", io.StringIO()):
            ec.main(["--section", "x'; SELECT 1; --"])

    def test_machine_channels_never_count_as_him(self):
        for ch in ("hc", "healthcheck", "cron", "claude", "general", "", None):
            self.assertFalse(ec.is_human(ch), ch)


class TestPrivacy(unittest.TestCase):
    def test_his_words_are_scrubbed(self):
        rows = [(TODAY, "We are partners; mail kochj@example.com or see https://a.b/c?x=1", "ok")]
        _, q = ec.stated_cares(rows)[0]
        self.assertNotIn("@", q)
        self.assertNotIn("http", q)
        self.assertIn("[email]", q)
        self.assertIn("[link]", q)

    def test_text_carries_no_sql(self):
        t = ec.empathy_text(ec.weigh(_rows("zigbee", (1, 2, 3)), TODAY), [], TODAY)
        self.assertNotIn("SELECT", t)


class TestPerformance(unittest.TestCase):
    def test_weigh_fast_on_10k_messages(self):
        rows = [(date(2026, 1, 1 + i % 28), f"message {i % 50} about the zigbee unit and the printer {i}", "ok")
                for i in range(10_000)]
        t0 = time.perf_counter()
        ec.weigh(rows, TODAY); ec.stated_cares(rows)
        self.assertLess(time.perf_counter() - t0, 1.5)


class TestRegression(unittest.TestCase):
    def test_signature_stable_across_order(self):
        w = ec.weigh(_rows("zigbee", (1, 2, 3)) + _rows("printer", (4, 5, 6)), TODAY)
        c = [(date(2026, 9, 1), "a"), (date(2026, 9, 2), "b")]
        self.assertEqual(ec.empathy_sig(w, c), ec.empathy_sig(list(reversed(w)), list(reversed(c))))

    def test_acks_and_names_never_become_topics(self):
        rows = [(date(2026, 9, d), "Yes", "ok") for d in range(1, 8)]
        rows += [(date(2026, 9, d), "All approved!", "ok") for d in range(1, 8)]
        rows += [(date(2026, 9, d), "Nova, Little Mister says thanks", "ok") for d in range(1, 8)]
        self.assertEqual([w["topic"] for w in ec.weigh(rows, TODAY)], ["thank"])

    def test_resurface_gate(self):
        self.assertFalse(ec._fresh({"s": "2026-10-03"}, "s", TODAY))
        self.assertTrue(ec._fresh({"s": "2026-09-20"}, "s", TODAY))
        self.assertTrue(ec._fresh({}, "s", TODAY))


class TestIntegration(unittest.TestCase):
    def test_remember_retries_then_raises(self):
        calls = []
        import urllib.request
        real = urllib.request.urlopen

        def boom(*a, **k):
            calls.append(1); raise OSError("down")
        urllib.request.urlopen = boom
        try:
            with self.assertRaises(OSError):
                ec.remember("t", {}, _sleep=lambda s: None)
        finally:
            urllib.request.urlopen = real
        self.assertEqual(len(calls), 3)


class TestDocs(unittest.TestCase):
    def test_docstring_names_the_wish_and_modes(self):
        self.assertIn("wish #67", SRC)
        for flag in ("--dry-run", "--selftest"):
            self.assertIn(flag, SRC)


# ── house categories added 2026-10-05 (Retry / Unit / Frame) ───────────────────

class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Cur:
    def __init__(self, answers=()):
        self.answers = list(answers); self.sql = []; self.params = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split())); self.params.append(params)

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        return self.answers.pop(0) if self.answers else []


class TestRetry(unittest.TestCase):
    def test_remember_fails_twice_then_succeeds_with_backoff(self):
        import urllib.request
        calls, sleeps = [], []
        real = urllib.request.urlopen

        def flaky(req, timeout=0):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("down")
            return _Resp({"id": 9})
        urllib.request.urlopen = flaky
        try:
            out = ec.remember("t", {}, _sleep=sleeps.append)
        finally:
            urllib.request.urlopen = real
        self.assertEqual(out, {"id": 9})
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [2, 4])          # linear backoff, no sleep after the success

    def test_pg_connect_has_no_retry_but_fails_open(self):
        # RETRY GAP: main()/psycopg2.connect — one attempt, then "nothing to do" with exit 0
        import types
        attempts = []

        def boom(*a, **k):
            attempts.append(1); raise OSError("pg down")
        real_pg, real_argv = ec.psycopg2, sys.argv
        ec.psycopg2 = types.SimpleNamespace(connect=boom); sys.argv = ["nova_empathy_core.py"]
        try:
            self.assertEqual(ec.main(), 0)
        finally:
            ec.psycopg2, sys.argv = real_pg, real_argv
        self.assertEqual(len(attempts), 1)

    def test_gather_failure_is_fail_open(self):
        # RETRY GAP: read_shared()/gateway_traces read — one attempt; the empathy section ends cleanly
        rc, posted, out = _run(["--section", "empathy"], _World(fail=True))
        self.assertEqual((rc, posted), (0, []))
        self.assertIn("gateway_traces read failed", out)

    def test_broken_db_never_raises_and_every_section_is_tried(self):
        # RETRY GAP: the section reads are single attempts; a dead DB fails each section, never the process
        rc, posted, out = _run([], _World(fail=True))
        self.assertEqual(rc, 1)
        self.assertEqual(posted, [])
        for name in ("hold", "quiet", "insight"):
            self.assertIn(f"section {name} failed", out)


# ── the Jordan lens (M6, 2026-10-09): a fake world for the four sections ──────

def _trace(m, d, tid, msg, resp="ok", els=False, pres=False):
    # (created_at, created_at::date, trace_id, user_message, response, in_empathy, in_quiet, in_elsewhere, in_presence)
    return (datetime(2026, m, d, 12, tzinfo=timezone.utc), date(2026, m, d), tid, msg, resp, True, True, els, pres)


TRACES = [_trace(9, 1, "t1", "which zigbee sensor should I buy", "No memory of that, stop asking"),
          _trace(9, 2, "t2", "zigbee repeaters added"),
          _trace(9, 3, "t3", "zigbee master bedroom firmware"),
          _trace(9, 28, "t4", "We are partners. How can I unblock you?", els=True, pres=True),
          _trace(10, 3, "t5", "grafana is broken please fix", els=True, pres=True)]


class _World:
    """Answers each section's SQL by what it asks for; records every statement."""

    def __init__(self, traces=TRACES, fail=False):
        self.traces, self.fail, self.sql, self.params = list(traces), fail, [], []
        self._one, self._all = None, []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.sql.append(s); self.params.append(params)
        if self.fail:
            raise RuntimeError("relation missing")
        one, rows = None, []
        if "trace_id, user_message, response" in s:
            rows = self.traces
        elif "FROM people" in s:
            one = ("Jordan — Sr. Manager SRE, builds Nova. More about him.",)
        elif "FROM relationship_arc" in s:
            one = (6, "Trust so absolute it bordered on reckless. More.",
                   [{"date": "2026-05-16", "what_shifted": "logging everything to nova_ops. ok"}])
        elif "FROM self_model" in s:
            one = ("I'm becoming someone who listens more than I speak. More.",)
        elif "kind='question'" in s:
            rows = [(1, 106, "What was the reason the printers went offline?", date(2026, 9, 28))]
        elif "kind='proposal'" in s:
            rows = [(23, "99", date(2026, 9, 28))]
        elif "FROM claude_messages" in s:
            rows = [(19, [365])]
        elif "FROM reach_log WHERE audience" in s:
            rows = [(36, datetime(2026, 9, 19, 19, tzinfo=timezone.utc), "dashboard")]
        elif "FROM predictions" in s:
            rows = [("Jordan will ask about the printer again within 3 days.", 0.75, False)] * 4
        elif "FROM claude_sessions" in s:
            rows = [("Fri", 10)] * 30 + [("Mon", 11)] * 5
        elif "FROM reach_log WHERE ts" in s:
            rows = [("filed", 11), ("dropped", 4)]
        self._one, self._all = one, rows

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all

    def inserts(self):
        return {(p[0], p[1]) for s, p in zip(self.sql, self.params) if s.startswith("INSERT INTO service_config")}

    def reads_of(self, table):
        return sum(f"FROM {table}" in s for s in self.sql)


class _Conn:
    autocommit = False

    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur


def _run(argv, world, llm=None, post=None, remember_fails_for=None):
    """Run the lens against a fake world; returns (rc, [(source, text, meta)], stdout)."""
    import nova_human_insight as hi_mod
    import nova_quiet_sensor as qs_mod
    posted = []

    def rem(text, meta, source=ec.SOURCE, **_):
        if source == remember_fails_for:
            raise OSError("memory server down")
        posted.append((source, text, meta))
    patches = [patch.object(psycopg2, "connect", lambda *a, **k: _Conn(world)),
               patch.object(ec, "remember", rem), patch.object(ec, "_stamp", dict),
               patch.object(hi_mod, "remember", lambda t, m: rem(t, m, source=hi_mod.SOURCE)),
               patch.object(hi_mod, "_stamp", dict)]
    patches.append(patch.object(qs_mod, "_post", post) if post else patch.object(qs_mod, "llm_close", lambda fs: llm))
    for p in patches:
        p.start()
    try:
        with redirect_stdout(io.StringIO()) as buf:
            rc = ec.main(argv)
    finally:
        for p in reversed(patches):
            p.stop()
    return rc, posted, buf.getvalue()


class TestJordanLens(unittest.TestCase):
    """Integration + Functional for the merged pass (M6)."""

    def test_full_pass_reads_his_messages_once_and_writes_four_sections(self):
        w = _World()
        rc, posted, _ = _run([], w)
        self.assertEqual(rc, 0)
        self.assertEqual(w.reads_of("gateway_traces"), 1)             # ONE read of the shared input
        self.assertEqual({p[0] for p in posted}, {"empathy_core", "hold", "quiet_sensor", "human_insight"})
        self.assertEqual(sum(p[0] == "human_insight" for p in posted), 3)
        self.assertEqual(w.inserts(), {("nova_empathy_core", "high_water"), ("nova_hold", "high_water"),
                                       ("nova_hold", "held"), ("nova_quiet_sensor", "high_water"),
                                       ("nova_quiet_sensor", "latest"), ("nova_human_insight", "high_water")})

    def test_each_section_keeps_its_voice_and_guards(self):
        rc, posted, _ = _run([], _World())
        text = {p[0]: p[1] for p in posted}
        meta = {p[0]: p[2] for p in posted}
        self.assertIn("'zigbee'", text["empathy_core"])
        self.assertIn("brushed it off 1 of 3", text["empathy_core"])
        self.assertIn("1. who he is: Jordan — Sr. Manager SRE, builds Nova.", text["hold"])
        self.assertIn("noticing, not knowing", text["quiet_sensor"])
        self.assertIn("[slack_prompts#1->reflection_questions#106]", text["quiet_sensor"])
        self.assertTrue(all(c for c in meta["quiet_sensor"]["cites"]))            # every finding cited
        self.assertEqual(meta["hold"]["organ"], "nova_hold")
        self.assertEqual(meta["quiet_sensor"]["organ"], "nova_quiet_sensor")
        self.assertEqual(meta["empathy_core"]["organ"], "nova_empathy_core")

    def test_quiet_model_line_that_speaks_of_him_is_refused(self):
        import nova_quiet_sensor as qs_mod
        bad = {"message": {"content": "Maybe he is stressed and avoiding these."}}
        rc, posted, _ = _run([], _World(), post=lambda url, body, timeout: bad)
        quiet = [p for p in posted if p[0] == "quiet_sensor"][0]
        self.assertIn(qs_mod.FALLBACK_CLOSE, quiet[1])
        self.assertNotIn("stressed", quiet[1])
        self.assertFalse(quiet[2]["model_close"])

    def test_one_section_alone(self):
        w = _World()
        rc, posted, _ = _run(["--section", "hold"], w)
        self.assertEqual((rc, [p[0] for p in posted]), (0, ["hold"]))
        self.assertEqual(w.inserts(), {("nova_hold", "high_water"), ("nova_hold", "held")})
        self.assertEqual(w.reads_of("predictions"), 0)

    def test_insight_alone_never_reads_his_messages(self):
        w = _World()
        rc, posted, _ = _run(["--section", "insight"], w)
        self.assertEqual(w.reads_of("gateway_traces"), 0)
        self.assertEqual({p[0] for p in posted}, {"human_insight"})

    def test_dry_run_prints_and_writes_nothing(self):
        w = _World()
        rc, posted, out = _run(["--dry-run", "--no-llm"], w)
        self.assertEqual((rc, posted, w.inserts()), (0, [], set()))
        for marker in ("Empathy core,", "Hold,", "Quiet sensor,", "• [Human insight]"):
            self.assertIn(marker, out)

    def test_a_failing_section_never_stops_the_others(self):
        rc, posted, out = _run([], _World(), remember_fails_for="hold")
        self.assertEqual(rc, 1)
        self.assertIn("section hold failed", out)
        self.assertEqual({p[0] for p in posted}, {"empathy_core", "quiet_sensor", "human_insight"})

    def test_shared_read_matches_what_each_section_used_to_read(self):
        sh = ec.read_shared(_World())
        self.assertEqual(sh["empathy"], [(t[1], t[3], t[4]) for t in TRACES])
        self.assertEqual(sh["topics"], [(t[1], t[2], t[3]) for t in TRACES])
        self.assertEqual((sh["to_me"], sh["presence_dates"], sh["last_date"]),
                         (2, {date(2026, 9, 28), date(2026, 10, 3)}, date(2026, 10, 3)))


class TestUnit(unittest.TestCase):
    def test_selftest_runs_clean(self):
        ec.demo()

    def test_scrub_edges(self):
        self.assertEqual(ec.scrub(""), "")
        self.assertEqual(ec.scrub("  a   b \n c "), "a b c")
        self.assertEqual(ec.scrub("<mailto:kochj@example.com> and https://x.y/z?q=1"), "[email] and [link]")

    def test_stem_and_topics(self):
        self.assertEqual(ec._stem("memories"), "memory")
        self.assertEqual(ec._stem("sensors"), "sensor")
        for untouched in ("status", "bus", "analysis", "zigbee"):
            self.assertEqual(ec._stem(untouched), untouched)
        self.assertEqual(ec.topics(""), set())
        self.assertEqual(ec.topics(None), set())
        self.assertEqual(ec.topics("the and for Nova Jordan"), set())      # all stoplist
        self.assertEqual(ec.topics("Zigbee ZIGBEE zigbee!"), {"zigbee"})   # one message counts once

    def test_weigh_and_cares_on_empty_and_acks(self):
        self.assertEqual(ec.weigh([], TODAY), [])
        self.assertEqual(ec.stated_cares([]), [])
        self.assertEqual(ec.weigh([(TODAY, "Yes", "ok")] * 9, TODAY), [])
        self.assertEqual(ec.weigh([(TODAY, None, None)], TODAY), [])
        self.assertEqual(ec.stated_cares([(TODAY, "All approved!", "ok")]), [])   # an ack is not a care

    def test_weigh_top_n_and_ordering(self):
        rows = _rows("zigbee", (1, 2, 3, 4)) + _rows("printer", (1, 2, 3)) + _rows("garage", (5, 6, 7))
        w = ec.weigh(rows, TODAY, n=2)
        self.assertEqual(len(w), 2)
        self.assertEqual(w[0]["topic"], "zigbee")
        self.assertEqual(w[0]["since_days"], (TODAY - date(2026, 9, 4)).days)

    def test_sig_and_text_edges(self):
        self.assertEqual(ec.empathy_sig([], []), ec.empathy_sig([], []))
        self.assertEqual(len(ec.empathy_sig([], [])), 16)
        self.assertIn("quiet is his", ec.empathy_text([], [], TODAY))
        t = ec.empathy_text(ec.weigh(_rows("zigbee", (1, 2, 3), resp="error: no"), TODAY), [], TODAY)
        self.assertIn("brushed it off 3 of 3", t)

    def test_state_helpers(self):
        self.assertEqual(ec.load_seen(_Cur([None])), {})
        self.assertEqual(ec.load_seen(_Cur([('{"seen": {"a": "2026-10-01"}}',)])), {"a": "2026-10-01"})
        self.assertEqual(ec.load_seen(_Cur([({"seen": {"b": "x"}},)])), {"b": "x"})
        cur = _Cur()
        ec.save_seen(cur, {"a": "2026-10-01"})
        self.assertIn("INSERT INTO service_config", cur.sql[0])
        self.assertEqual(cur.params[0][0], ec.STATE_SERVICE)
        self.assertTrue(ec._fresh({"s": "not-a-date"}, "s", TODAY))     # unreadable state never blocks


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        import os
        import subprocess
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_empathy_core.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all empathy-core assertions passed", r.stdout)

    def test_import_never_runs_main(self):
        import os
        import subprocess
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_empathy_core"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("[empathy-core", r.stdout)


if __name__ == "__main__":
    unittest.main()
