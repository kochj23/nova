#!/usr/bin/env python3
"""Tests for nova_human_insight.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Since 2026-10-09 (M6) Human Insight runs as the 'insight' section of
nova_empathy_core.py; nova_human_insight.main() is a thin wrapper. Written by Jordan Koch (via Claude)."""
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
import urllib.request
from collections import Counter
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
EC_SRC = (SCRIPTS / "nova_empathy_core.py").read_text()

# Offline guard: no real PG, no real memory server from this file, ever (2026-10-09: a stale patch
# target let one run of this file reach the live DB; it now fails loudly instead).
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


import nova_human_insight as hi  # noqa: E402 — import-clean; the same object the lens's insight section uses
SRC = (SCRIPTS / "nova_human_insight.py").read_text()
TODAY = date(2026, 10, 5)


class _Cur:
    def __init__(self, answers=(), fail=None):
        self.answers = list(answers); self.sql = []; self.params = []; self.fail = fail

    def execute(self, sql, params=None):
        if self.fail and self.fail in sql:
            raise RuntimeError("db down")
        self.sql.append(" ".join(sql.split())); self.params.append(params)

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        return self.answers.pop(0) if self.answers else []

    def executed(self, frag):
        return [s for s in self.sql if frag in s]


class _Conn:
    def __init__(self, cur): self.cur = cur; self.autocommit = False

    def cursor(self): return self.cur


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()

    def read(self): return self._d

    def __enter__(self): return self

    def __exit__(self, *a): return False


def _urlopen(fn):
    import urllib.request
    return patch.object(urllib.request, "urlopen", fn)


def _world(seen=None):
    """predictions (4 wrong on one theme), 30 sessions on Friday mornings, a mostly-held reach_log."""
    return _Cur([
        [("Jordan will ask about the printer again within 3 days.", 0.75, False)] * 4 + [("Jordan will smile.", 0.5, True)],
        [("Fri", 10)] * 20 + [("Fri", 11)] * 6 + [("Thu", 15)] * 4,
        [("filed", 11), ("dropped", 4), ("sent", 1)],
        seen,                                                    # service_config high-water
    ])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_and_the_only_write_is_its_own_state(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertEqual(re.findall(r"\b(?<!DO )(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC), ["service_config"])
        self.assertIn("WHERE service=%s AND key=%s", SRC)

    def test_reads_only_her_own_records(self):
        body = EC_SRC[EC_SRC.index("def section_insight"):EC_SRC.index("SECTIONS = {")]   # where main() runs now
        for table in ("predictions", "claude_sessions", "reach_log"):
            self.assertIn(f"FROM {table}", body)
        self.assertNotIn("gateway_traces", body)                 # no raw conversation text leaves the DB


class TestPerformance(unittest.TestCase):
    def test_prediction_grouping_fast_on_10k_rows(self):
        rows = [(f"Jordan will {('ask', 'fix', 'ignore')[i % 3]} thing {i % 50}", 0.6, bool(i % 4)) for i in range(10_000)]
        t0 = time.perf_counter()
        out = hi.prediction_insights(rows)
        for _ in range(1_000):
            hi.rhythm_insight(Counter({"Fri": 30, "Thu": 10}), Counter({h: 1 for h in range(24)}), 40)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(out, [])                                 # 75% right -> never a "keep being wrong"


class TestRetry(unittest.TestCase):
    def test_remember_fails_twice_then_succeeds(self):
        calls, sleeps = [], []

        def flaky(req, timeout=0):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("down")
            return _Resp({"id": 3})
        with _urlopen(flaky):
            self.assertEqual(hi.remember("t", {}, _sleep=sleeps.append), {"id": 3})
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [2, 4])

    def test_remember_gives_up_after_three(self):
        calls = []

        def boom(*a, **k):
            calls.append(1); raise OSError("down")
        with _urlopen(boom), self.assertRaises(OSError):
            hi.remember("t", {}, _sleep=lambda s: None)
        self.assertEqual(len(calls), 3)

    def test_pg_connect_has_no_retry_but_fails_open(self):
        # RETRY GAP: main()/psycopg2.connect — one attempt, exit 0
        attempts = []

        def boom(*a, **k):
            attempts.append(1); raise OSError("pg down")
        with patch.object(psycopg2, "connect", boom), redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(hi.main([]), 0)
        self.assertEqual(len(attempts), 1)
        self.assertIn("fail-open", buf.getvalue())

    def test_wrapper_runs_the_lens_insight_section(self):
        import nova_empathy_core as ec
        calls = []
        with patch.object(ec, "main", lambda argv: calls.append(argv) or 0), redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(hi.main(["--dry-run"]), 0)
        self.assertEqual(calls, [["--section", "insight", "--dry-run"]])
        self.assertIn("merged into nova_empathy_core.py on 2026-10-09", buf.getvalue())


class TestUnit(unittest.TestCase):
    def test_selftest_passes(self):
        with redirect_stdout(io.StringIO()):
            hi.demo()

    def test_prediction_insights_edges(self):
        self.assertEqual(hi.prediction_insights([]), [])
        rows = [("I will not follow up on the thing.", 0.8, False)] * 7 + [("I will not follow up on the thing.", 0.8, True)] * 3
        p = hi.prediction_insights(rows)                         # 70% wrong: exactly at the bar
        self.assertEqual(p[0]["theme"], "not follow up on")     # "i will " is stripped first; the negation stays
        self.assertEqual(p[0]["n"], 10)
        self.assertEqual(hi.prediction_insights(rows[:6] + rows[7:]), [])    # 67%: under the bar

    def test_rhythm_insight_edges(self):
        self.assertIsNone(hi.rhythm_insight(Counter(), Counter(), 50))
        self.assertIsNone(hi.rhythm_insight(Counter({"Fri": 5}), Counter({10: 5}), hi.MIN_SESSIONS - 1))
        flat = hi.rhythm_insight(Counter({d: 5 for d in "ABCDEFG"}), Counter({h: 2 for h in range(24)}), 35)
        self.assertIsNone(flat)                                  # nothing dominates
        r = hi.rhythm_insight(Counter({"Sat": 10, "Sun": 10}), Counter({23: 10, 0: 10, 1: 10}), 30)
        self.assertEqual(r["band"], (23, 1))                      # a band may wrap midnight

    def test_silence_insight_edges(self):
        self.assertIsNone(hi.silence_insight(2, 2, 0))
        s = hi.silence_insight(3, 1, 1)
        self.assertEqual((s["total"], s["held_share"]), (5, 0.8))

    def test_insight_text(self):
        self.assertEqual(hi.insight_text("nope", {}), "")
        t = hi.insight_text("silence", hi.silence_insight(11, 4, 0))
        self.assertIn("held back 100%", t)
        self.assertIn("11 filed, 4 dropped, 0 sent", t)

    def test_state_helpers(self):
        self.assertEqual(hi.load_seen(_Cur([None])), {})
        self.assertEqual(hi.load_seen(_Cur([('{"seen": {"a": "2026-10-01"}}',)])), {"a": "2026-10-01"})
        cur = _Cur()
        hi.save_seen(cur, {"a": "x"})
        self.assertEqual(cur.params[0][:2], (hi.STATE_SERVICE, hi.STATE_KEY))
        self.assertTrue(hi._fresh({}, "s", TODAY))
        self.assertFalse(hi._fresh({"s": "2026-10-01"}, "s", TODAY))
        self.assertTrue(hi._fresh({"s": "2026-09-01"}, "s", TODAY))
        self.assertTrue(hi._fresh({"s": "garbage"}, "s", TODAY))


class TestIntegration(unittest.TestCase):
    def _run(self, argv, cur, posted=None):
        real_pg, real_argv = psycopg2.connect, sys.argv
        psycopg2.connect = lambda *a, **k: _Conn(cur); sys.argv = ["nova_human_insight.py", *argv]
        try:
            with patch.object(hi, "remember", lambda t, m: (posted if posted is not None else []).append((t, m))), \
                    redirect_stdout(io.StringIO()) as buf:
                rc = hi.main()
        finally:
            psycopg2.connect, sys.argv = real_pg, real_argv
        return rc, buf.getvalue()

    def test_dry_run_chains_the_three_signals_and_writes_nothing(self):
        cur = _world()
        posted = []
        rc, out = self._run(["--dry-run"], cur, posted)
        self.assertEqual(rc, 0)
        self.assertIn("3 insight(s) derived", out)
        self.assertEqual(out.count("• [Human insight]"), 3)
        self.assertIn("Fri carries 87%", out)
        self.assertEqual(posted, [])
        self.assertEqual(cur.executed("INSERT INTO service_config"), [])

    def test_state_lives_under_its_own_service_config_key(self):
        self.assertEqual((hi.STATE_SERVICE, hi.STATE_KEY), ("nova_human_insight", "high_water"))
        cur = _world()
        self._run([], cur)
        ins = cur.executed("INSERT INTO service_config")
        self.assertEqual(len(ins), 1)
        seen = json.loads(cur.params[cur.sql.index(ins[0])][2])["seen"]
        # theme = first four words; the best 3-hour band over 10h/11h starts at 09 (first maximum wins)
        self.assertEqual(set(seen), {"prediction:ask about the printer", "rhythm:Fri-9", "silence:held"})

    def test_memory_source_and_lineage_feature_detection(self):
        self.assertEqual(hi.SOURCE, "human_insight")
        self.assertIsInstance(hi._stamp(), dict)                 # lineage present or {} — never raises


class TestFunctional(unittest.TestCase):
    def _run(self, argv, cur, urlopen):
        real_pg, real_argv = psycopg2.connect, sys.argv
        psycopg2.connect = lambda *a, **k: _Conn(cur); sys.argv = ["nova_human_insight.py", *argv]
        try:
            with _urlopen(urlopen), redirect_stdout(io.StringIO()) as buf:
                rc = hi.main()
        finally:
            psycopg2.connect, sys.argv = real_pg, real_argv
        return rc, buf.getvalue()

    def test_golden_path_remembers_three_insights_and_saves_high_water(self):
        posted = []

        def ok(req, timeout=0):
            posted.append(json.loads(req.data)); return _Resp({"id": len(posted)})
        cur = _world()
        rc, out = self._run([], cur, ok)
        self.assertEqual(rc, 0)
        self.assertEqual(len(posted), 3)
        self.assertEqual({p["source"] for p in posted}, {"human_insight"})
        self.assertEqual({p["metadata"]["kind"] for p in posted}, {"prediction", "rhythm", "silence"})
        self.assertTrue(all(p["metadata"]["organ"] == "nova_human_insight" for p in posted))
        self.assertIn("surfaced 3 new insight(s)", out)
        self.assertEqual(len(cur.executed("INSERT INTO service_config")), 1)

    def test_recently_surfaced_insights_are_not_repeated(self):
        posted = []

        def ok(req, timeout=0):
            posted.append(json.loads(req.data)); return _Resp({"id": 1})
        # the gate compares against the real UTC date, so pin "today" in the state to it
        seen = {"rhythm:Fri-9": datetime.now(timezone.utc).date().isoformat(), "silence:held": "2026-09-01"}
        cur = _world(seen=(json.dumps({"seen": seen}),))
        rc, out = self._run([], cur, ok)
        self.assertEqual(rc, 0)
        self.assertEqual({p["metadata"]["kind"] for p in posted}, {"prediction", "silence"})
        self.assertIn("surfaced 2 new insight(s)", out)

    def test_one_failing_source_does_not_sink_the_others(self):
        cur = _Cur([[("Fri", 10)] * 30, [("filed", 11), ("dropped", 4)], None], fail="FROM predictions")
        posted = []

        def ok(req, timeout=0):
            posted.append(json.loads(req.data)); return _Resp({"id": 1})
        rc, out = self._run([], cur, ok)
        self.assertEqual(rc, 0)
        self.assertIn("predictions read failed", out)
        self.assertEqual({p["metadata"]["kind"] for p in posted}, {"rhythm", "silence"})


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_human_insight.py"), "--selftest"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all human-insight assertions passed", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_human_insight"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
