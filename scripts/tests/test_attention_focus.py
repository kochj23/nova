#!/usr/bin/env python3
"""Tests for nova_attention_focus.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame), plus the original Privacy / Regression / Docs classes for wish #36.
Written by Jordan Koch (via Claude)."""
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
from datetime import date
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


af = _load("af", SCRIPTS / "nova_attention_focus.py")
SRC = (SCRIPTS / "nova_attention_focus.py").read_text()


class TestFunctional(unittest.TestCase):
    def test_selftest_passes(self):
        af.demo()  # raises on any failed assertion

    def test_live_critical_outranks_neglected_goal(self):
        items = [{"key": "goal:1", "label": "g", "score": af.score_goal(200, 7)},
                 {"key": "incident:1", "label": "i", "score": af.score_incident("critical", 0, False)}]
        self.assertEqual(af.rank_focus(items, 1)[0]["key"], "incident:1")

    def test_main_dry_run_prints_focus_and_writes_nothing(self):
        today = date(2026, 9, 28)
        cur = _world(today)
        buf = io.StringIO()
        with patch.object(af.psycopg2, "connect", lambda *a, **k: _Conn(cur)), \
                patch.object(sys, "argv", ["x", "--dry-run"]), redirect_stdout(buf):
            self.assertEqual(af.main(), 0)
        out = buf.getvalue()
        self.assertIn("1. open critical on nova-core: pg down", out)
        self.assertIn("horology (returned 22x)", out)
        self.assertEqual(cur.executed("INSERT"), [])

    def test_main_golden_path_remembers_then_saves_high_water(self):
        today = date(2026, 9, 28)
        cur = _world(today)
        posted = []
        buf = io.StringIO()
        with patch.object(af.psycopg2, "connect", lambda *a, **k: _Conn(cur)), \
                patch.object(af, "remember", lambda text, meta: posted.append((text, meta)) or {"id": 1}), \
                patch.object(sys, "argv", ["x"]), redirect_stdout(buf):
            self.assertEqual(af.main(), 0)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][1]["organ"], af.STATE_SERVICE)
        self.assertIn("incident:1", posted[0][1]["focus"])
        self.assertEqual(len(cur.executed("INSERT INTO service_config")), 1)
        self.assertIn("stated a new focus", buf.getvalue())

    def test_main_unchanged_focus_is_silent(self):
        today = date(2026, 9, 28)
        items, pre = af.gather(_world(today), today)
        sig = af.focus_sig(af.rank_focus(items))
        cur = _world(today)
        cur.answers = [(k, v) for k, v in cur.answers if k != "FROM service_config"]
        stamp = af.datetime.now(af.timezone.utc).date().isoformat()   # main() gates on the real UTC date
        cur.answers.append(("FROM service_config", ({"seen": {sig: stamp}},)))
        buf = io.StringIO()
        with patch.object(af.psycopg2, "connect", lambda *a, **k: _Conn(cur)), \
                patch.object(af, "remember", lambda *a, **k: self.fail("must not write")), \
                patch.object(sys, "argv", ["x"]), redirect_stdout(buf):
            self.assertEqual(af.main(), 0)
        self.assertIn("unchanged", buf.getvalue())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_read_only_over_the_world(self):
        # the only writes are its own high-water row and the memory POST
        body = SRC[SRC.index("def gather"):SRC.index("def main")]
        for verb in ("UPDATE", "DELETE", "INSERT", "acked_at ="):
            self.assertNotIn(verb, body)


class TestPrivacy(unittest.TestCase):
    def test_focus_text_carries_no_sql_or_emails(self):
        focus = [{"key": "incident:1", "label": "open critical on nova-core: thing (1d, un-acked)"}]
        hold = [{"topic": "horology", "returns": 22}]
        t = af.focus_text(focus, hold, date(2026, 9, 28))
        self.assertNotIn("SELECT", t)
        self.assertNotIn("@", t)


class TestPerformance(unittest.TestCase):
    def test_rank_and_hold_fast_on_10k(self):
        items = [{"key": f"k{i}", "label": "x", "score": (i % 100) / 100} for i in range(10_000)]
        pre = [{"key": f"p{i}", "topic": "t", "returns": i % 30, "days_since": i % 20} for i in range(10_000)]
        t0 = time.perf_counter()
        f = af.rank_focus(items)
        af.hold_set(pre, {x["key"] for x in f})
        self.assertLess(time.perf_counter() - t0, 0.2)


class TestRegression(unittest.TestCase):
    def test_signature_stable_across_order(self):
        a = [{"key": "x"}, {"key": "y"}]
        self.assertEqual(af.focus_sig(a), af.focus_sig(list(reversed(a))))

    def test_resurface_gate(self):
        today = date(2026, 9, 28)
        seen = {"s": "2026-09-27"}
        self.assertFalse(af._fresh(seen, "s", today))
        self.assertTrue(af._fresh({"s": "2026-09-20"}, "s", today))
        self.assertTrue(af._fresh({}, "s", today))


class TestIntegration(unittest.TestCase):
    def test_remember_retries_then_raises(self):
        calls = []
        orig = af.json.dumps
        import urllib.request
        real = urllib.request.urlopen

        def boom(*a, **k):
            calls.append(1); raise OSError("down")
        urllib.request.urlopen = boom
        try:
            with self.assertRaises(OSError):
                af.remember("t", {}, _sleep=lambda s: None)
        finally:
            urllib.request.urlopen = real
        self.assertEqual(len(calls), 3)
        self.assertIs(af.json.dumps, orig)


class TestDocs(unittest.TestCase):
    def test_docstring_names_the_wish_and_modes(self):
        self.assertIn("wish #36", SRC)
        for flag in ("--dry-run", "--selftest"):
            self.assertIn(flag, SRC)


class _Cur:
    """Answers keyed by a SQL fragment (first match wins); records every execute."""
    def __init__(self, answers=()):
        self.answers = list(answers); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        hit = next((v for k, v in self.answers if k in sql), None)
        if isinstance(hit, Exception):
            raise hit
        self._last = hit

    def fetchone(self):
        return self._last[0] if isinstance(self._last, list) else self._last

    def fetchall(self):
        return self._last if isinstance(self._last, list) else ([] if self._last is None else [self._last])

    def executed(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur): self._cur = cur; self.autocommit = False

    def cursor(self, *a, **k): return self._cur


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()

    def read(self): return self._d

    def __enter__(self): return self

    def __exit__(self, *a): return False


def _world(today):
    """A cursor stub with one open critical, one review due, one goal and two preoccupations."""
    return _Cur([
        ("FROM telemetry.incidents", [(1, "critical", "nova-core", "pg down", today, False)]),
        ("FROM growth_commitments", [(7, "I over-claim", today)]),
        ("FROM goals", [(3, "ship the thing", 7, None)]),
        ("FROM preoccupations", [(11, "horology", 22, today), (12, "rail radio", 1, today)]),
        ("FROM service_config", None),
    ])


class TestRetry(unittest.TestCase):
    def test_remember_fails_twice_then_succeeds(self):
        import urllib.request
        attempts, slept = [], []

        def flaky(req, timeout=60):
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("memory server busy")
            return _Resp({"id": "m1"})
        with patch.object(urllib.request, "urlopen", flaky):
            out = af.remember("t", {"k": 1}, _sleep=slept.append)
        self.assertEqual(out, {"id": "m1"})
        self.assertEqual(len(attempts), 3)
        self.assertEqual(slept, [2, 4])  # backoff grows

    def test_gather_fails_open_per_table(self):
        # RETRY GAP: gather — each read is tried once; a failed table contributes nothing and the run continues
        cur = _Cur([("FROM telemetry.incidents", RuntimeError("no incidents view")),
                    ("FROM growth_commitments", []), ("FROM goals", []),
                    ("FROM preoccupations", [(1, "t", 5, date(2026, 9, 28))])])
        buf = io.StringIO()
        with redirect_stdout(buf):
            items, pre = af.gather(cur, date(2026, 9, 28))
        self.assertEqual(items, [])
        self.assertEqual(len(pre), 1)
        self.assertIn("incidents read failed", buf.getvalue())

    def test_main_fails_open_when_pg_is_down(self):
        # RETRY GAP: main psycopg2.connect — one attempt, logs and exits 0 so the scheduler stays quiet
        def boom(*a, **k):
            raise OSError("pg down")
        buf = io.StringIO()
        with patch.object(af.psycopg2, "connect", boom), patch.object(sys, "argv", ["x"]), redirect_stdout(buf):
            self.assertEqual(af.main(), 0)
        self.assertIn("fail-open", buf.getvalue())


class TestUnit(unittest.TestCase):
    def test_score_edges(self):
        self.assertEqual(af.score_goal(5, 0), 0.0)
        self.assertEqual(af.score_goal(5, None), 0.0)
        self.assertEqual(af.score_goal(7, 7), 0.2)
        self.assertEqual(af.score_review(af.REVIEW_HORIZON_DAYS + 1), 0.0)
        self.assertEqual(af.score_review(af.REVIEW_HORIZON_DAYS), 0.0)
        self.assertAlmostEqual(af.score_incident(None, 0, True), 0.4)
        self.assertAlmostEqual(af.score_incident("WARNING", 0, False), 0.6 + af.UNACKED_BONUS)

    def test_rank_and_hold_empty_inputs(self):
        self.assertEqual(af.rank_focus([]), [])
        self.assertEqual(af.rank_focus([{"key": "a", "score": 0}]), [])
        self.assertEqual(af.hold_set([], set()), [])
        self.assertEqual(af.focus_sig([]), af.focus_sig([]))

    def test_focus_text_without_hold(self):
        t = af.focus_text([{"label": "open critical"}], [], date(2026, 9, 28))
        self.assertIn("1. open critical", t)
        self.assertIn("Nothing recent to hold", t)
        self.assertIn("quiet", af.focus_text([], [], date(2026, 9, 28)))

    def test_load_seen_accepts_jsonb_dict_or_text(self):
        self.assertEqual(af.load_seen(_Cur([("FROM service_config", ({"seen": {"s": "2026-09-27"}},))])), {"s": "2026-09-27"})
        self.assertEqual(af.load_seen(_Cur([("FROM service_config", ('{"seen": {"s": "x"}}',))])), {"s": "x"})
        self.assertEqual(af.load_seen(_Cur([("FROM service_config", None)])), {})
        self.assertEqual(af.load_seen(_Cur([("FROM service_config", (None,))])), {})

    def test_save_seen_upserts_the_high_water(self):
        cur = _Cur()
        af.save_seen(cur, {"sig": "2026-09-28"})
        sql, params = cur.executed("INSERT INTO service_config")[0]
        self.assertIn("ON CONFLICT (service, key)", sql)
        self.assertEqual(params[:2], (af.STATE_SERVICE, af.STATE_KEY))
        self.assertEqual(json.loads(params[2]), {"seen": {"sig": "2026-09-28"}})

    def test_fresh_tolerates_garbage_dates(self):
        self.assertTrue(af._fresh({"s": "not-a-date"}, "s", date(2026, 9, 28)))


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_attention_focus.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("passed", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertIn('sys.argv[1] == "--selftest"', SRC)
        self.assertEqual(af.__name__, "af")
        self.assertTrue(callable(af.main))


if __name__ == "__main__":
    unittest.main()
