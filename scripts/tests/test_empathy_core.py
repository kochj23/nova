#!/usr/bin/env python3
"""Tests for nova_empathy_core.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame) plus the wish #67 privacy/regression/docs checks.
Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import re
import sys
import time
import unittest
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]


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
        body = SRC[SRC.index("def gather"):SRC.index("def main")]
        for verb in ("UPDATE", "DELETE", "INSERT"):
            self.assertNotIn(verb, body)

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
        # RETRY GAP: gather()/gateway_traces read — one attempt; a failure ends the run cleanly
        import types

        class Boom:
            def execute(self, *a): raise RuntimeError("relation missing")

        class Conn:
            autocommit = False

            def cursor(self): return Boom()
        real_pg, real_argv = ec.psycopg2, sys.argv
        ec.psycopg2 = types.SimpleNamespace(connect=lambda *a, **k: Conn()); sys.argv = ["x"]
        try:
            self.assertEqual(ec.main(), 0)
        finally:
            ec.psycopg2, sys.argv = real_pg, real_argv


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
