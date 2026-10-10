#!/usr/bin/env python3
"""Tests for nova_temporal_intuition.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Since 2026-10-09 (M15) this runs as `nova_time_sense.py --daily`;
main() is a thin wrapper, so the functional tests drive it end to end through time sense.
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_temporal_intuition.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import nova_temporal_intuition as ti  # noqa: E402 — import-clean; the same object time sense's --daily uses
import nova_time_sense as ts_mod  # noqa: E402
SRC = SCRIPT.read_text()

# Offline guard: no real PG, no real memory server from this file, ever (tests patch over it where needed).
_GUARDS = []


def _offline(*a, **k):
    raise RuntimeError("offline test: real PG / HTTP blocked")


def setUpModule():
    import urllib.request
    import psycopg2
    for target in (mock.patch.object(psycopg2, "connect", _offline), mock.patch.object(urllib.request, "urlopen", _offline)):
        target.start(); _GUARDS.append(target)


def tearDownModule():
    while _GUARDS:
        _GUARDS.pop().stop()
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class _FrozenDT(datetime):
    """datetime whose now() is pinned to the fixture NOW — the module reads the real clock, and the
    fixtures are absolute dates, so without this the day counts drift by one every midnight."""
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Cur:
    """Cursor stub: answers fetchone/fetchall by substring of the last SQL, records every execute."""
    def __init__(self, routes=None):
        self.routes = routes or []; self.sql = []; self.params = []; self._last = ""

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
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur


def _ops_routes(**over):
    base = {
        "FROM gateway_traces": [(NOW - timedelta(days=8),)],
        "FROM herd_correspondents": [("Gaston", NOW - timedelta(days=31))],
        "FROM memory_anchors": [("k1", "the failing disk", NOW - timedelta(days=2))],
        "FROM preoccupations": [(5, "horology", NOW - timedelta(days=100))],
        "FROM projects": [],
        "FROM service_config": None,
    }
    base.update(over)
    return list(base.items())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        cur = _Cur()
        ti.save_state(cur, {"herd:x'); DROP TABLE service_config; --": 7})
        self.assertNotIn("DROP TABLE", cur.sql[0])
        self.assertIn("DROP TABLE", cur.params[0][2])          # carried as a bound jsonb value

    def test_read_only_over_the_world(self):
        body = SRC[SRC.index("def gather"):SRC.index("def load_state")]
        for verb in ("INSERT", "UPDATE", "DELETE", "DROP"):
            self.assertNotIn(verb, body)
        writes = set(re.findall(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC))
        self.assertEqual(writes, {"service_config"})

    def test_machine_channels_are_excluded_as_a_bound_tuple(self):
        oc = _Cur(_ops_routes())
        ti.gather(oc, None, NOW)
        p = [p for s, p in zip(oc.sql, oc.params) if "gateway_traces" in s][0]
        self.assertEqual(p, (ti.MACHINE_CHANNELS,))


class TestPerformance(unittest.TestCase):
    def test_crossed_and_notice_text_fast_on_10k(self):
        t0 = time.perf_counter()
        crossings = []
        for i in range(10_000):
            th = ti.crossed(i % 400, (i % 7) * 30 if i % 3 else None)
            if th:
                crossings.append((f"k{i}", "label", i % 400, th))
        text = ti.notice_text(crossings, date(2026, 10, 5))
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertGreater(len(crossings), 1000)
        self.assertIn("a year now", text)


class TestRetry(unittest.TestCase):
    def test_remember_retries_twice_then_succeeds(self):
        calls = []

        def flaky(req, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("memory server down")
            return _Resp({"id": 42})
        with mock.patch("urllib.request.urlopen", flaky), mock.patch("time.sleep") as slp:
            self.assertEqual(ti.remember("t", {}), {"id": 42})
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.args[0] for c in slp.call_args_list], [2, 4])   # linear backoff

    def test_remember_raises_after_three_failures(self):
        calls = []

        def boom(req, timeout=None):
            calls.append(1); raise OSError("down")
        with mock.patch("urllib.request.urlopen", boom), mock.patch("time.sleep"):
            with self.assertRaises(OSError):
                ti.remember("t", {})
        self.assertEqual(len(calls), 3)

    # RETRY GAP: main() psycopg2.connect — one attempt, fails open to rc 0 and does nothing.
    def test_main_fails_open_without_pg(self):
        with mock.patch.object(ti.psycopg2, "connect", side_effect=OSError("pg down")), \
             mock.patch.object(ti.sys, "argv", ["x"]), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ti.main(), 0)
        self.assertIn("fail-open", out.getvalue())

    def test_gather_fails_open_per_query(self):
        oc = _Cur(_ops_routes(**{"FROM herd_correspondents": RuntimeError("no table")}))
        with redirect_stdout(io.StringIO()):
            got = ti.gather(oc, None, NOW)
        keys = {k for k, _, _ in got}
        self.assertIn("jordan:voice", keys)
        self.assertFalse(any(k.startswith("herd:") for k in keys))


class TestUnit(unittest.TestCase):
    def test_selftest_passes(self):
        with redirect_stdout(io.StringIO()):
            ti.demo()

    def test_crossed_edges(self):
        self.assertIsNone(ti.crossed(0, None))
        self.assertIsNone(ti.crossed(6, 0))
        self.assertEqual(ti.crossed(7, 0), 7)
        self.assertEqual(ti.crossed(365, 180), 365)
        self.assertIsNone(ti.crossed(1000, 365))
        self.assertEqual(ti.crossed(10_000, None), 365)

    def test_word_covers_every_threshold(self):
        for d, w in ti.THRESHOLDS:
            self.assertEqual(ti.word(d), w)
        with self.assertRaises(KeyError):
            ti.word(8)

    def test_days_handles_naive_aware_date_and_none(self):
        self.assertIsNone(ti._days(None, NOW))
        self.assertEqual(ti._days(date(2026, 10, 5), NOW), 0)
        self.assertEqual(ti._days(datetime(2026, 9, 5), NOW), 30)
        self.assertEqual(ti._days(datetime(2026, 9, 5, 20, tzinfo=timezone(timedelta(hours=-7))), NOW), 29)
        self.assertLess(ti._days(datetime(2026, 10, 6, tzinfo=timezone.utc), NOW), 0)   # future -> filtered by gather

    def test_gather_drops_none_and_negative_durations(self):
        oc = _Cur(_ops_routes(**{"FROM gateway_traces": [(None,)],
                                 "FROM projects": [(1, "future", NOW + timedelta(days=3))]}))
        got = ti.gather(oc, None, NOW)
        keys = {k for k, _, _ in got}
        self.assertNotIn("jordan:voice", keys)
        self.assertNotIn("project:1", keys)
        self.assertEqual(dict((k, d) for k, _, d in got)["herd:Gaston"], 31)

    def test_load_state_accepts_dict_or_json_string(self):
        self.assertEqual(ti.load_state(_Cur([("FROM service_config", ({"noticed": {"a": 7}},))])), {"a": 7})
        self.assertEqual(ti.load_state(_Cur([("FROM service_config", ('{"noticed": {"b": 30}}',))])), {"b": 30})
        self.assertEqual(ti.load_state(_Cur()), {})
        self.assertEqual(ti.load_state(_Cur([("FROM service_config", (None,))])), {})

    def test_notice_text_is_first_person_and_cites_days(self):
        t = ti.notice_text([("anchor:k", "holding on to 'x'", 95, 90)], date(2026, 10, 5))
        self.assertTrue(t.startswith("A sense of time, 2026-10-05"))
        self.assertIn("a season now: holding on to 'x' (95 days)", t)


class TestIntegration(unittest.TestCase):
    def test_gather_with_memory_cursor_adds_self_age(self):
        oc = _Cur(_ops_routes())
        mc = _Cur([("FROM memories", [(NOW - timedelta(days=400),)])])
        got = {k: d for k, _, d in ti.gather(oc, mc, NOW)}
        self.assertEqual(got["self:age"], 400)
        self.assertEqual(got["anchor:k1"], 2)
        self.assertEqual(got["preocc:5"], 100)

    def test_gather_then_crossed_then_state_round_trip(self):
        got = ti.gather(_Cur(_ops_routes()), None, NOW)
        noticed = {"herd:Gaston": 30}
        crossings = [(k, l, d, ti.crossed(d, noticed.get(k))) for k, l, d in got]
        crossings = [c for c in crossings if c[3]]
        keys = {k for k, _, _, _ in crossings}
        self.assertEqual(keys, {"jordan:voice", "preocc:5"})       # Gaston at 31d already noticed at 30
        cur = _Cur()
        for k, _, _, th in crossings:
            noticed[k] = max(th, noticed.get(k, 0))
        ti.save_state(cur, noticed)
        self.assertEqual(cur.params[0][0], ti.STATE_SERVICE)
        self.assertEqual(json.loads(cur.params[0][2])["noticed"]["preocc:5"], 90)

    def test_lineage_helper_is_feature_detected_not_reimplemented(self):
        self.assertIn("import nova_lineage", SRC)
        self.assertNotIn("def lineage_stamp", SRC)
        self.assertIsInstance(ti._stamp(), dict)

    def test_sibling_organ_shares_machine_channels(self):
        import nova_time_sense
        self.assertEqual(nova_time_sense.MACHINE_CHANNELS, ti.MACHINE_CHANNELS)


class TestFunctional(unittest.TestCase):
    def _main(self, oc, urlopen=None, *argv, mem_fail=False):
        posted = []

        def default_urlopen(req, timeout=None):
            posted.append(json.loads(req.data.decode())); return _Resp({"id": 1})

        def connect(dsn, **k):
            if "nova_memories" in dsn:
                if mem_fail:
                    raise OSError("mem pg down")
                return _Conn(_Cur([("FROM memories", [(NOW - timedelta(days=400),)])]))
            return _Conn(oc)
        with mock.patch.object(ti.psycopg2, "connect", side_effect=connect), \
             mock.patch("urllib.request.urlopen", urlopen or default_urlopen), \
             mock.patch.object(ti, "_stamp", return_value={}), \
             mock.patch.object(ti.sys, "argv", ["nova_temporal_intuition.py", *argv]), \
             mock.patch.object(ti, "datetime", _FrozenDT), mock.patch.object(ts_mod, "datetime", _FrozenDT), \
             redirect_stdout(io.StringIO()) as out:
            rc = ti.main()
        return rc, out.getvalue(), posted

    def test_wrapper_runs_time_sense_daily(self):
        calls = []
        with mock.patch.object(ts_mod, "main", lambda argv: calls.append(argv) or 0), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ti.main(["--report"]), 0)
        self.assertEqual(calls, [["--daily", "--report"]])
        self.assertIn("merged into nova_time_sense.py on 2026-10-09", out.getvalue())

    def test_golden_path_writes_one_memory_and_advances_state(self):
        oc = _Cur(_ops_routes(**{"FROM service_config": ({"noticed": {"herd:Gaston": 30}},)}))
        rc, out, posted = self._main(oc, mem_fail=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["source"], ti.SOURCE)
        self.assertIn("a week now: since Little Mister last spoke to me (8 days)", posted[0]["text"])
        self.assertIn("a season now: since I last developed 'horology' (100 days)", posted[0]["text"])
        self.assertNotIn("Gaston", posted[0]["text"])
        crossings = {c["key"]: c["threshold"] for c in posted[0]["metadata"]["crossings"]}
        self.assertEqual(crossings, {"jordan:voice": 7, "preocc:5": 90})
        saved = [p for s, p in zip(oc.sql, oc.params) if "INSERT INTO service_config" in s]
        self.assertEqual(json.loads(saved[0][2])["noticed"], {"herd:Gaston": 30, "jordan:voice": 7, "preocc:5": 90})
        self.assertIn("noticed: a week since Little Mister", out)

    def test_dry_run_and_report_write_nothing(self):
        oc = _Cur(_ops_routes())
        rc, out, posted = self._main(oc, None, "--dry-run")
        self.assertEqual(rc, 0); self.assertEqual(posted, [])
        self.assertIn("a year now: since my first memory (400 days)", out)
        self.assertFalse(any("INSERT" in s for s in oc.sql))
        oc = _Cur(_ops_routes())
        rc, out, posted = self._main(oc, None, "--report")
        self.assertEqual(rc, 0); self.assertEqual(posted, [])
        self.assertRegex(out, r"\s+400d\s+self:age")

    def test_blank_day_writes_nothing(self):
        oc = _Cur(_ops_routes(**{"FROM service_config": ({"noticed": {"jordan:voice": 7, "herd:Gaston": 30,
                                                                       "preocc:5": 90, "self:age": 365}},)}))
        rc, out, posted = self._main(oc)
        self.assertEqual(rc, 0); self.assertEqual(posted, [])
        self.assertFalse(any("INSERT" in s for s in oc.sql))
        self.assertIn("0 crossed a threshold today", out)

    def test_error_path_memory_server_down_leaves_state_unchanged(self):
        oc = _Cur(_ops_routes())

        def boom(req, timeout=None):
            raise OSError("memory server down")
        with mock.patch("time.sleep"):
            with self.assertRaises(OSError):
                self._main(oc, boom, mem_fail=True)
        self.assertFalse(any("INSERT INTO service_config" in s for s in oc.sql))   # never noticed, so never marked


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all temporal-intuition assertions passed", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch.object(ti.psycopg2, "connect", side_effect=AssertionError("main ran")):
            _load("ti_again", SCRIPT)


if __name__ == "__main__":
    unittest.main()
