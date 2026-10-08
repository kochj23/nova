#!/usr/bin/env python3
"""Tests for nova_time_sense.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_time_sense.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ts = _load("ts", SCRIPT)
SRC = SCRIPT.read_text()
# a Tuesday afternoon, fixed offset so "%a %H" is deterministic
NOW = datetime(2026, 10, 6, 15, 0, tzinfo=timezone(timedelta(hours=-7)))


class _Connection:
    def __init__(self):
        self.rollbacks = 0; self.commits = 0

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


class _Cur:
    """Cursor stub: answers fetchone/fetchall by substring of the last SQL, records every execute.
    A route whose value is an Exception raises on execute (the real cursor needs rollback after)."""
    def __init__(self, routes=None):
        self.routes = routes or []; self.sql = []; self.params = []; self._last = ""
        self.connection = _Connection()

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql
        for needle, val in self.routes:
            if needle in sql and isinstance(val, Exception):
                raise val

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        self._cur.connection.commit()


def _routes(**over):
    base = {
        "WHERE ts > %s - interval '60 minutes'": (40,),                       # this hour
        "GROUP BY date_trunc('hour', ts)": [(10,), (20,), (30,), (50,), (60,)],  # baseline -> pct 0.6 usual
        "to_char(ts,'Dy HH24')": [("Mon 09", 900), ("Tue 15", 500), ("Sun 03", 2)],
        "SELECT tempo FROM time_sense": [("usual",), ("usual",), ("quiet",)],
        "FROM contact_sense WHERE updated_at": (NOW - timedelta(minutes=42),),
        "SELECT mouth FROM contact_sense": ("imessage",),
        "FROM telemetry.incidents": (NOW - timedelta(hours=50),),
        "category = 'journal'": (NOW - timedelta(hours=3, minutes=5),),
        "FROM article_citations": (NOW - timedelta(days=2),),
        "FROM presence_state": ("office", 0.92, 1.0),
        "FROM reach_log": (NOW - timedelta(days=1),),
        "SELECT min(computed_at)": (NOW - timedelta(hours=5),),     # before the affect_state route: it contains it
        "SELECT label, computed_at FROM affect_state": ("keyed-up", NOW - timedelta(hours=1)),
    }
    base.update(over)
    return list(base.items())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        cur = _Cur(_routes())
        ts.sense(cur, NOW)
        for s, p in zip(cur.sql, cur.params):
            if "%s" in s.replace("%%", ""):
                self.assertIsNotNone(p, s)
        # the machine-channel exclusion is a bound tuple, never spliced into the SQL text
        gw = [p for s, p in zip(cur.sql, cur.params) if "gateway_traces" in s]
        self.assertTrue(all(isinstance(p[0], tuple) for p in gw))

    def test_only_writes_are_its_own_row_and_its_config_key(self):
        writes = set(re.findall(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC))
        self.assertEqual(writes, {"time_sense", "service_config"})
        self.assertIn("VALUES ('time_sense', 'current'", SRC)

    def test_dsn_is_env_overridable_and_carries_no_password(self):
        self.assertIn('os.environ.get("NOVA_OPS_DSN"', SRC)
        self.assertNotIn("password=", ts.DSN)


class TestPerformance(unittest.TestCase):
    def test_sense_fast_against_a_10k_row_baseline(self):
        baseline = [(i % 97,) for i in range(10_000)]
        hours = [(f"{d} {h:02d}", 10_000 - i) for i, (d, h) in
                 enumerate((d, h) for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun") for h in range(24))]
        cur = _Cur(_routes(**{"GROUP BY date_trunc('hour', ts)": baseline, "to_char(ts,'Dy HH24')": hours,
                              "SELECT tempo FROM time_sense": [("usual",)] * 10_000}))
        t0 = time.perf_counter()
        s = ts.sense(cur, NOW)
        ts.sentence(s, NOW)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(s["baseline_n"], 10_000)
        self.assertEqual(s["stretch_h"], 10_001)


class TestRetry(unittest.TestCase):
    # RETRY GAP: main() psycopg2.connect — one attempt, no backoff; a down PG raises out of the hourly run.
    def test_main_does_not_mask_a_dead_pg(self):
        calls = []

        def boom(*a, **k):
            calls.append(1); raise OSError("pg down")
        with mock.patch.object(ts.psycopg2, "connect", boom), mock.patch.object(ts.sys, "argv", ["x"]):
            with self.assertRaises(OSError):
                ts.main()
        self.assertEqual(len(calls), 1)

    def test_since_queries_fail_open_and_roll_back(self):
        cur = _Cur(_routes(**{"FROM contact_sense WHERE updated_at": RuntimeError("no table"),
                              "FROM telemetry.incidents": RuntimeError("no schema"),
                              "SELECT label, computed_at FROM affect_state": RuntimeError("gone"),
                              "FROM gateway_traces": (NOW - timedelta(minutes=9),)}))
        s = ts.sense(cur, NOW)
        self.assertEqual(s["since_jordan_min"], 9)                      # gateway-only fallback used
        self.assertIsNone(s["since_critical_min"])
        self.assertNotIn("mood", s)
        self.assertEqual(cur.connection.rollbacks, 3)                   # each failed read rolled back
        self.assertIn("my event stream has been usual for 3 h", ts.sentence(s, NOW))


class TestUnit(unittest.TestCase):
    def test_ago_buckets(self):
        self.assertIsNone(ts._ago(None))
        self.assertEqual(ts._ago(0), "0 min")
        self.assertEqual(ts._ago(59.9), "59 min")
        self.assertEqual(ts._ago(60), "1 h 00")
        self.assertEqual(ts._ago(185), "3 h 05")
        self.assertEqual(ts._ago(48 * 60 - 1), "47 h 59")
        self.assertEqual(ts._ago(48 * 60), "2 days")
        self.assertEqual(ts._ago(10 * 1440 + 30), "10 days")

    def test_tempo_buckets_by_percentile(self):
        base = [(10 * i,) for i in range(1, 11)]
        for this_hour, expect in ((0, "quiet"), (20, "quiet"), (40, "usual"), (80, "usual"), (90, "busy"), (100, "frantic")):
            cur = _Cur(_routes(**{"WHERE ts > %s - interval '60 minutes'": (this_hour,),
                                  "GROUP BY date_trunc('hour', ts)": base}))
            self.assertEqual(ts.sense(cur, NOW)["tempo"], expect, this_hour)

    def test_empty_baseline_is_usual(self):
        s = ts.sense(_Cur(_routes(**{"GROUP BY date_trunc('hour', ts)": []})), NOW)
        self.assertEqual((s["pct"], s["tempo"], s["baseline_n"]), (0.5, "usual", 0))

    def test_phase_from_own_rank(self):
        s = ts.sense(_Cur(_routes()), NOW)
        self.assertEqual((s["busiest"], s["quietest"]), ("Mon 09", "Sun 03"))
        self.assertEqual(s["phase"], "an ordinary hour of the week")
        s = ts.sense(_Cur(_routes(**{"to_char(ts,'Dy HH24')": [("Tue 15", 9), ("Mon 09", 8), ("Sun 03", 1), ("Sat 04", 0)]})), NOW)
        self.assertEqual(s["phase"], "the busy part of the week")
        s = ts.sense(_Cur(_routes(**{"to_char(ts,'Dy HH24')": [("Mon 09", 9), ("Sun 03", 8), ("Sat 04", 2), ("Tue 15", 0)]})), NOW)
        self.assertEqual(s["phase"], "the slow part of the week")
        s = ts.sense(_Cur(_routes(**{"to_char(ts,'Dy HH24')": [("Mon 09", 9)]})), NOW)
        self.assertNotIn("phase", s)

    def test_stretch_counts_consecutive_same_bucket(self):
        s = ts.sense(_Cur(_routes(**{"SELECT tempo FROM time_sense": []})), NOW)
        self.assertEqual(s["stretch_h"], 1)
        s = ts.sense(_Cur(_routes(**{"SELECT tempo FROM time_sense": [("quiet",), ("usual",)]})), NOW)
        self.assertEqual(s["stretch_h"], 1)

    def test_sentence_time_of_day_and_optional_parts(self):
        s = {"tempo": "quiet", "stretch_h": 2}
        for hour, tod in ((3, "small hours"), (6, "early morning"), (10, "morning"), (13, "afternoon"),
                          (19, "evening"), (23, "late night")):
            self.assertIn(f"It is {tod} on tuesday", ts.sentence(s, NOW.replace(hour=hour)))
        self.assertEqual(ts.sentence(s, NOW),
                         "It is afternoon on tuesday, an ordinary hour of the week; my event stream has been quiet for 2 h.")


class TestIntegration(unittest.TestCase):
    def test_machine_channels_shared_with_temporal_intuition(self):
        import nova_temporal_intuition
        self.assertEqual(ts.MACHINE_CHANNELS, nova_temporal_intuition.MACHINE_CHANNELS)
        self.assertNotIn("MACHINE_CHANNELS = (", SRC.split("except Exception")[0])   # one definition, imported

    def test_sense_then_sentence_golden_shape(self):
        s = ts.sense(_Cur(_routes()), NOW)
        self.assertEqual((s["events_hour"], s["baseline_n"], s["pct"], s["tempo"], s["stretch_h"]),
                         (40, 5, 0.6, "usual", 3))
        self.assertEqual(s["mouth"], "imessage")
        text = ts.sentence(s, NOW)
        self.assertEqual(text, "It is afternoon on tuesday, an ordinary hour of the week; my event stream has been "
                               "usual for 3 h; Little Mister is home, in the office; "
                               "Little Mister last spoke to me 42 min ago (through imessage); "
                               "nothing has been critical for 2 days; I last published 3 h 05 ago; "
                               "I have felt keyed-up for 5 h 00.")

    def test_published_reads_journal_publishes_not_citations(self):
        # 2026-10-08: "I last published 2 days ago" came from article_citations on a day with 5 journal posts
        s = ts.sense(_Cur(_routes()), NOW)
        self.assertAlmostEqual(s["since_published_min"], 185, delta=1)
        s = ts.sense(_Cur(_routes(**{"category = 'journal'": (None,)})), NOW)   # no publish events -> fallback
        self.assertAlmostEqual(s["since_published_min"], 2 * 1440, delta=1)

    def test_presence_comes_from_presence_state(self):
        for row, phrase in ((("away", 0.8, 1.0), "Little Mister is away from home"),
                            (("home", 0.6, 1.0), "Little Mister is home;"),
                            (("office", 0.9, 30.0), "I can't tell where Little Mister is")):
            s = ts.sense(_Cur(_routes(**{"FROM presence_state": row})), NOW)
            self.assertIn(phrase, ts.sentence(s, NOW) .replace(".", ";"))
        self.assertNotIn("the house has been", ts.sentence(ts.sense(_Cur(_routes()), NOW), NOW))

    def test_contact_sense_outranks_gateway_fallback(self):
        cur = _Cur(_routes())
        ts.sense(cur, NOW)
        self.assertFalse(any("gateway_traces" in s for s in cur.sql))
        cur = _Cur(_routes(**{"FROM contact_sense WHERE updated_at": (None,), "FROM gateway_traces": (None,)}))
        s = ts.sense(cur, NOW)
        self.assertTrue(any("gateway_traces" in s for s in cur.sql))
        self.assertIsNone(s["since_jordan_min"])


class TestFunctional(unittest.TestCase):
    def _main(self, cur, *argv):
        with mock.patch.object(ts.psycopg2, "connect", return_value=_Conn(cur)), \
             mock.patch.object(ts.sys, "argv", ["nova_time_sense.py", *argv]), \
             redirect_stdout(io.StringIO()) as out:
            ts.main()
        return out.getvalue()

    def test_golden_path_writes_row_and_config(self):
        cur = _Cur(_routes())
        out = self._main(cur)
        self.assertIn("[time-sense] It is ", out)
        ins = [(s, p) for s, p in zip(cur.sql, cur.params) if s.startswith("INSERT INTO time_sense")]
        self.assertEqual(len(ins), 1)
        self.assertEqual(ins[0][1][1], "usual")
        self.assertEqual(json.loads(ins[0][1][3])["events_hour"], 40)
        cfg = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO service_config" in s]
        self.assertEqual(json.loads(cfg[0][0])["tempo"], "usual")
        self.assertTrue(json.loads(cfg[0][0])["sentence"].startswith("It is "))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS time_sense" in s for s in cur.sql))
        self.assertEqual(cur.connection.commits, 2)

    def test_dry_run_prints_json_and_writes_nothing(self):
        cur = _Cur(_routes())
        out = self._main(cur, "--dry-run")
        self.assertFalse(any(s.startswith("INSERT") for s in cur.sql))
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["tempo"], "usual")

    def test_error_path_sources_missing_still_writes_a_sentence(self):
        cur = _Cur(_routes(**{"FROM contact_sense WHERE updated_at": RuntimeError("x"),
                              "FROM gateway_traces": RuntimeError("x"),
                              "FROM telemetry.incidents": RuntimeError("x"),
                              "category = 'journal'": RuntimeError("x"),
                              "FROM article_citations": RuntimeError("x"),
                              "FROM presence_state": RuntimeError("x"),
                              "FROM reach_log": RuntimeError("x"),
                              "SELECT label, computed_at FROM affect_state": RuntimeError("x")}))
        out = self._main(cur)
        self.assertIn("my event stream has been usual for 3 h.", out)
        self.assertEqual(sum(s.startswith("INSERT INTO time_sense") for s in cur.sql), 1)


class TestFrame(unittest.TestCase):
    def test_import_smoke_exits_zero(self):
        # no --help/--selftest in this organ: importing must be side-effect free (no PG, no network)
        r = subprocess.run([sys.executable, "-c", "import nova_time_sense"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        with mock.patch.object(ts.psycopg2, "connect", side_effect=AssertionError("main ran")):
            _load("ts_again", SCRIPT)


if __name__ == "__main__":
    unittest.main()
