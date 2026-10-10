#!/usr/bin/env python3
"""Tests for nova_weight_of_memory.py (wish #37 "Weight of Memory"), one per house category:
functional, security, privacy, performance, regression, integration, docs. Since 2026-10-09 (M12)
the weighing pass runs inside nova_memory_anchor.py; main() is a thin wrapper for `--weight`.
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
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

# Offline guard: no real PG, no real memory server from this file, ever (2026-10-09: after the M12 merge an
# unpatched weighing pass in this suite reached the live memory server; it now fails loudly instead).
_GUARDS = []


def _offline(*a, **k):
    raise RuntimeError("offline test: real PG / HTTP blocked")


def setUpModule():
    import urllib.request
    import psycopg2
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


wm = _load("wm", SCRIPTS / "nova_weight_of_memory.py")
SRC = (SCRIPTS / "nova_weight_of_memory.py").read_text()


class TestFunctional(unittest.TestCase):
    def test_selftest_passes(self):
        wm.demo()  # raises on any failed assertion

    def test_returns_floor_gates_weight(self):
        # below MIN_RETURNS a theme has no gravity no matter how old
        self.assertEqual(wm.weight(wm.MIN_RETURNS - 1, 400, 0), 0.0)
        self.assertGreater(wm.weight(wm.MIN_RETURNS, 0, 0), 0.0)

    def test_returning_outranks_a_bigger_but_shallow_theme(self):
        items = [{"key": "preocc:1", "topic": "shallow", "returns": 4, "longevity_days": 0,
                  "days_since": 0, "weight": wm.weight(4, 0, 0)},
                 {"key": "preocc:2", "topic": "deep", "returns": 20, "longevity_days": 300,
                  "days_since": 0, "weight": wm.weight(20, 300, 0)}]
        self.assertEqual(wm.rank_weighty(items, 1)[0]["key"], "preocc:2")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_read_only_over_the_world(self):
        # the only writes are its own high-water row and the memory POST — never the world
        body = SRC[SRC.index("def gather"):SRC.index("def main")]
        for verb in ("UPDATE", "DELETE", "INSERT", "DROP", "status ="):
            self.assertNotIn(verb, body)


class TestPrivacy(unittest.TestCase):
    def test_text_carries_no_sql_or_row_bodies(self):
        heavy = [{"key": "preocc:1", "topic": "the failing disk", "returns": 22,
                  "longevity_days": 240, "days_since": 2}]
        t = wm.weight_text(heavy, date(2026, 9, 28))
        for leak in ("SELECT", "preocc:", "http://", "password"):
            self.assertNotIn(leak, t)

    def test_source_tag_is_namespaced(self):
        self.assertEqual(wm.SOURCE, "weight_of_memory")


class TestPerformance(unittest.TestCase):
    def test_ranking_is_quick_on_many_themes(self):
        items = [{"key": f"preocc:{i}", "topic": str(i), "returns": i % 30,
                  "longevity_days": i, "days_since": 0,
                  "weight": wm.weight(i % 30, i, 0)} for i in range(5000)]
        t0 = time.perf_counter()
        top = wm.rank_weighty(items)
        self.assertLessEqual(time.perf_counter() - t0, 0.5)
        self.assertLessEqual(len(top), wm.WEIGH_N)


class TestRegression(unittest.TestCase):
    def test_longevity_saturates_at_cap(self):
        self.assertEqual(wm.weight(10, wm.LONGEVITY_CAP_DAYS, 0),
                         wm.weight(10, wm.LONGEVITY_CAP_DAYS * 2, 0))

    def test_staleness_decays_but_never_zeroes(self):
        fresh = wm.weight(10, 100, wm.STALE_DAYS)
        stale = wm.weight(10, 100, wm.STALE_DAYS * 4)
        self.assertTrue(0 < stale < fresh)

    def test_signature_order_independent(self):
        self.assertEqual(wm.weigh_sig([{"key": "a"}, {"key": "b"}]),
                         wm.weigh_sig([{"key": "b"}, {"key": "a"}]))
        self.assertNotEqual(wm.weigh_sig([{"key": "a"}]), wm.weigh_sig([{"key": "a"}, {"key": "b"}]))


class TestIntegration(unittest.TestCase):
    def test_freshness_gate_matches_resurface_window(self):
        today = date(2026, 9, 28)
        seen = {"sig1": today.isoformat()}
        self.assertFalse(wm._fresh(seen, "sig1", today))          # just stated -> not fresh
        self.assertTrue(wm._fresh(seen, "sig2", today))           # unseen -> fresh
        old = date(2026, 9, 28 - min(27, wm.RESURFACE_DAYS + 1)) if wm.RESURFACE_DAYS < 27 else today
        self.assertTrue(wm._fresh({"sigX": old.isoformat()}, "sigX", today))

    def test_conventions_match_sibling_organ(self):
        for tok in ("OPS_DSN", "MEMSRV", "STATE_SERVICE", "def remember", "def load_seen", "def _fresh"):
            self.assertIn(tok, SRC)


class TestDocs(unittest.TestCase):
    def test_has_usage_and_wish_attribution(self):
        self.assertIn("--dry-run", SRC)
        self.assertIn("--selftest", SRC)
        self.assertIn("wish #37", SRC)

    def test_listed_in_readme(self):
        readme = (SCRIPTS.parent / "README.md").read_text()
        self.assertIn("nova_weight_of_memory.py", readme)


# ── house categories added 2026-10-05: Retry, Unit, Frame ───────────────────────

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


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestRetry(unittest.TestCase):
    def test_remember_retries_twice_then_succeeds_with_backoff(self):
        calls, slept = [], []

        def flaky(req, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("memory server down")
            return _Resp({"id": 5})
        with mock.patch("urllib.request.urlopen", flaky):
            self.assertEqual(wm.remember("t", {}, _sleep=slept.append), {"id": 5})
        self.assertEqual(len(calls), 3)
        self.assertEqual(slept, [2, 4])

    def test_remember_raises_after_the_last_attempt(self):
        calls = []

        def boom(req, timeout=None):
            calls.append(1); raise OSError("down")
        with mock.patch("urllib.request.urlopen", boom):
            with self.assertRaises(OSError):
                wm.remember("t", {}, _tries=4, _sleep=lambda s: None)
        self.assertEqual(len(calls), 4)

    # RETRY GAP: main() psycopg2.connect — one attempt; fails open to rc 0 and writes nothing.
    def test_main_fails_open_without_pg(self):
        calls = []

        def boom(*a, **k):
            calls.append(1); raise OSError("pg down")
        with mock.patch.object(wm.psycopg2, "connect", boom), mock.patch.object(wm.sys, "argv", ["x"]), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wm.main(), 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("fail-open", out.getvalue())

    def test_gather_fails_open_on_a_broken_read(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wm.gather(_Cur([("FROM preoccupations", RuntimeError("no table"))]), date(2026, 10, 5)), [])
        self.assertIn("preoccupations read failed", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_weight_edges(self):
        self.assertEqual(wm.weight(0, 0, 0), 0.0)
        self.assertEqual(wm.weight(wm.MIN_RETURNS, -50, 0), float(wm.MIN_RETURNS))     # negative longevity clamps to 0
        self.assertEqual(wm.weight(10, 100, wm.STALE_DAYS), wm.weight(10, 100, 0))      # decay starts strictly past STALE_DAYS
        self.assertAlmostEqual(wm.weight(10, 100, wm.STALE_DAYS * 2), wm.weight(10, 100, 0) / 2)

    def test_months_rounds_and_floors_at_one(self):
        self.assertEqual(wm._months(0), 1)
        self.assertEqual(wm._months(44), 1)
        self.assertEqual(wm._months(46), 2)
        self.assertEqual(wm._months(365), 12)

    def test_rank_and_sig_on_empty_input(self):
        self.assertEqual(wm.rank_weighty([]), [])
        self.assertEqual(wm.rank_weighty([{"key": "a", "weight": 1.0}], n=0), [])
        self.assertEqual(wm.weigh_sig([]), wm.weigh_sig([]))
        self.assertEqual(len(wm.weigh_sig([{"key": "a"}])), 16)

    def test_fresh_treats_malformed_state_as_fresh(self):
        today = date(2026, 10, 5)
        self.assertTrue(wm._fresh({"s": "not-a-date"}, "s", today))
        self.assertTrue(wm._fresh({"s": ""}, "s", today))
        self.assertTrue(wm._fresh({}, "s", today))
        self.assertFalse(wm._fresh({"s": "2026-10-04"}, "s", today))

    def test_load_and_save_seen_round_trip(self):
        self.assertEqual(wm.load_seen(_Cur([("FROM service_config", ({"seen": {"a": "2026-10-01"}},))])), {"a": "2026-10-01"})
        self.assertEqual(wm.load_seen(_Cur([("FROM service_config", ('{"seen": {"b": "2026-10-02"}}',))])), {"b": "2026-10-02"})
        self.assertEqual(wm.load_seen(_Cur([("FROM service_config", (None,))])), {})
        self.assertEqual(wm.load_seen(_Cur()), {})
        cur = _Cur(); wm.save_seen(cur, {"a": "2026-10-05"})
        self.assertEqual(cur.params[0][:2], (wm.STATE_SERVICE, wm.STATE_KEY))
        self.assertEqual(json.loads(cur.params[0][2]), {"seen": {"a": "2026-10-05"}})

    def test_gather_computes_longevity_and_staleness(self):
        today = date(2026, 10, 5)
        rows = [(1, "the failing disk", 22, date(2026, 2, 1), date(2026, 10, 3)),
                (2, "never developed", 5, None, None),
                (3, "shallow", 1, date(2026, 10, 1), date(2026, 10, 1))]   # the SQL COALESCEs returns to 0
        items = wm.gather(_Cur([("FROM preoccupations", rows)]), today)
        by = {i["key"]: i for i in items}
        self.assertEqual((by["preocc:1"]["longevity_days"], by["preocc:1"]["days_since"]), (244, 2))
        self.assertEqual((by["preocc:2"]["longevity_days"], by["preocc:2"]["days_since"]), (0, 10**6))
        self.assertEqual(by["preocc:3"]["weight"], 0.0)
        self.assertEqual([i["key"] for i in wm.rank_weighty(items)], ["preocc:1", "preocc:2"])

    def test_main_golden_path_writes_once_then_dedups(self):
        today = datetime.now(timezone.utc).date()
        rows = [(1, "the failing disk", 22, today - date.resolution * 240, today - date.resolution * 2)]
        posted = []

        def urlopen(req, timeout=None):
            posted.append(json.loads(req.data.decode())); return _Resp({"id": 1})
        cur = _Cur([("FROM preoccupations", rows)])
        with mock.patch.object(wm.psycopg2, "connect", return_value=_Conn(cur)), mock.patch.object(wm.sys, "argv", ["x"]), \
             mock.patch("urllib.request.urlopen", urlopen), mock.patch.object(wm, "_stamp", return_value={}), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(wm.main(), 0)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["source"], wm.SOURCE)
        self.assertIn("the failing disk — returned to 22x", posted[0]["text"])
        self.assertEqual(posted[0]["metadata"]["heaviest"], ["preocc:1"])
        saved = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO service_config" in s]
        sig = posted[0]["metadata"]["sig"]
        self.assertEqual(json.loads(saved[0][2])["seen"], {sig: today.isoformat()})
        # same heaviest set, same day -> nothing re-stated
        cur2 = _Cur([("FROM preoccupations", rows), ("FROM service_config", ({"seen": {sig: today.isoformat()}},))])
        with mock.patch.object(wm.psycopg2, "connect", return_value=_Conn(cur2)), mock.patch.object(wm.sys, "argv", ["x"]), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("must not post")), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wm.main(), 0)
        self.assertIn("heaviest set unchanged", out.getvalue())
        self.assertFalse(any("INSERT" in s for s in cur2.sql))
        self.assertFalse(any("memory_anchors" in s for s in cur.sql + cur2.sql))   # the weighing pass alone

    def test_wrapper_runs_the_anchor_weighing_pass(self):
        import nova_memory_anchor as anchor
        calls = []
        with mock.patch.object(anchor, "main", lambda argv: calls.append(argv) or 0), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wm.main(["--dry-run"]), 0)
        self.assertEqual(calls, [["--weight", "--dry-run"]])
        self.assertIn("merged into nova_memory_anchor.py on 2026-10-09", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_weight_of_memory.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all weight-of-memory assertions passed", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch.object(wm.psycopg2, "connect", side_effect=AssertionError("main ran")):
            _load("wm_again", SCRIPTS / "nova_weight_of_memory.py")


if __name__ == "__main__":
    unittest.main()
