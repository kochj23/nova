#!/usr/bin/env python3
"""Tests for nova_memory_anchor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Since 2026-10-09 (M12) the anchor also runs Weight of Memory's
weighing pass (its own source and high-water) from the same read. Written by Jordan Koch (via Claude)."""
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
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_memory_anchor.py"
SRC = SCRIPT.read_text()

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


ma = _load("ma", SCRIPT)
TODAY = datetime.now(timezone.utc).date()


class _Cur:
    """Routes each query to a canned answer by SQL substring (first match wins); records every statement."""
    def __init__(self, routes=()):
        self.routes = list(routes); self.sql = []; self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split()); self.sql.append((self._last, params))

    def _hit(self):
        for key, val in self.routes:
            if key in self._last:
                return val
        return None

    def fetchone(self):
        v = self._hit()
        if isinstance(v, list):
            return v[0] if v else None
        return v

    def fetchall(self):
        v = self._hit()
        return list(v) if isinstance(v, list) else ([] if v is None else [v])

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Boom(_Cur):
    def execute(self, sql, params=None):
        raise RuntimeError("relation does not exist")


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


HEAVY = (1, "the failing disk", 22, TODAY - timedelta(days=240), TODAY - timedelta(days=2))   # preoccupations row


def _cur(anchors=(), preoccs=(), seen=None):
    return _Cur([("FROM memory_anchors WHERE released_at IS NULL", list(anchors)),
                 ("FROM preoccupations WHERE status='active'", list(preoccs)),
                 ("FROM service_config", ({"seen": seen},) if seen is not None else None)])


def _main(cur, argv=("nova_memory_anchor.py",), remember=None, weigh_remember=None):
    """Run the anchor against a fake cursor; the anchor's and the weighing pass's memory POSTs are both mocked.
    Returns (rc, anchor remember mock, stdout); the weighing mock is on _main.weigh."""
    remember = remember or MagicMock(return_value={"id": 1})
    _main.weigh = weigh_remember or MagicMock(return_value={"id": 2})
    out = io.StringIO()
    with patch.object(ma.psycopg2, "connect", return_value=_Conn(cur)), patch.object(ma, "remember", remember), \
         patch.object(ma.wom, "remember", _main.weigh), patch.object(ma.wom, "_stamp", dict), \
         patch.object(sys, "argv", list(argv)), redirect_stdout(out):
        rc = ma.main()
    return rc, remember, out.getvalue()


def _state_rows(cur, service):
    return [p for s, p in cur.ran("INSERT INTO service_config") if p[0] == service]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_and_writes_stay_in_its_own_tables(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"memory_anchors", "service_config"})
        self.assertNotIn("DELETE", SRC.replace("Never deleted", "").replace("never deleted", ""))
        cur = _cur(preoccs=[(1, "x'); DROP TABLE memory_anchors; --", 22, TODAY - timedelta(days=200), TODAY)])
        _main(cur)
        sql, params = cur.ran("INSERT INTO memory_anchors")[0]
        self.assertNotIn("DROP", sql); self.assertEqual(params[1], "x'); DROP TABLE memory_anchors; --")


class TestPerformance(unittest.TestCase):
    def test_decide_fast_on_10k_anchors(self):
        # real shape: Weight of Memory names WEIGH_N (3) heaviest themes; the held set is what can grow
        anchors = {f"preocc:{i}": {"subject": "s", "anchored_at": TODAY, "zero_since": TODAY - timedelta(days=100)} for i in range(10_000)}
        heaviest = ["preocc:1", "preocc:2", "preocc:20000"]
        weights = {f"preocc:{i}": 1.0 for i in range(2_500, 5_000)}
        t0 = time.perf_counter()
        to_set, to_release, after = ma.decide(anchors, heaviest, weights, TODAY)
        ma.anchor_sig(after)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual((to_set, len(to_release), len(after)), (["preocc:20000"], 7_498, 2_503))


class TestRetry(unittest.TestCase):
    def test_remember_retries_with_backoff_then_succeeds(self):
        calls = []

        def fake(req, timeout=0):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("memsrv down")
            return _Resp({"id": 7})
        with patch("urllib.request.urlopen", side_effect=fake), patch("time.sleep") as sl:
            self.assertEqual(ma.remember("t", {}), {"id": 7})
        self.assertEqual(len(calls), 3)
        self.assertEqual([c[0][0] for c in sl.call_args_list], [2, 4])
        self.assertTrue(calls[0].endswith("/remember"))

    def test_remember_raises_after_three_failures(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")) as u, patch("time.sleep"):
            with self.assertRaises(OSError):
                ma.remember("t", {})
        self.assertEqual(u.call_count, 3)

    def test_pg_down_fails_open(self):
        # RETRY GAP: main (psycopg2.connect) — no retry, but no exception escapes and nothing is written
        with patch.object(ma.psycopg2, "connect", side_effect=OSError("no pg")), patch.object(sys, "argv", ["x"]), redirect_stdout(io.StringIO()):
            self.assertEqual(ma.main(), 0)
            self.assertEqual(ma.main(["--weight"]), 0)
        self.assertEqual(ma.held_keys(_Boom()), set())


class TestUnit(unittest.TestCase):
    def test_selftest_passes(self):
        with redirect_stdout(io.StringIO()):
            ma.demo()

    def test_hysteresis_release_after_release_after_days(self):
        d = date(2026, 1, 1)
        anchors = {"p1": {"subject": "s", "anchored_at": d - timedelta(days=30), "zero_since": d}}
        for days, released in ((0, False), (ma.RELEASE_AFTER_DAYS - 1, False), (ma.RELEASE_AFTER_DAYS, True), (400, True)):
            _, to_release, after = ma.decide(anchors, [], {}, d + timedelta(days=days))
            self.assertEqual((to_release == ["p1"], "p1" not in after), (released, released), days)
        _, to_release, after = ma.decide(anchors, [], {"p1": 0.01}, d + timedelta(days=500))   # any gravity resets the clock
        self.assertEqual(to_release, []); self.assertIsNone(after["p1"]["zero_since"])
        _, _, after = ma.decide({}, [], {}, d)
        self.assertEqual(after, {})

    def test_decide_is_idempotent_and_non_mutating(self):
        d = date(2026, 1, 1)
        anchors = {"p1": {"subject": "s", "anchored_at": d, "zero_since": None}}
        s1, r1, a1 = ma.decide(anchors, ["p1"], {"p1": 5.0}, d)
        s2, r2, a2 = ma.decide(a1, ["p1"], {"p1": 5.0}, d)
        self.assertEqual((s1, r1, s2, r2), ([], [], [], []))
        self.assertIsNot(a1["p1"], anchors["p1"])

    def test_fresh_and_text(self):
        self.assertTrue(ma._fresh({}, "s", TODAY))
        self.assertTrue(ma._fresh({"s": "garbage"}, "s", TODAY))
        self.assertFalse(ma._fresh({"s": (TODAY - timedelta(days=1)).isoformat()}, "s", TODAY))
        self.assertTrue(ma._fresh({"s": (TODAY - timedelta(days=ma.RESURFACE_DAYS)).isoformat()}, "s", TODAY))
        t = ma.anchor_text([{"subject": "b", "anchored_at": TODAY}, {"subject": "a", "anchored_at": TODAY - timedelta(days=9)}], [], TODAY)
        self.assertLess(t.index("a — anchored 9 day(s) ago"), t.index("b — anchored today"))   # oldest anchor first
        self.assertNotIn("preocc:", t)

    def test_load_seen_accepts_json_text_or_dict(self):
        self.assertEqual(ma.load_seen(_Cur([("FROM service_config", (json.dumps({"seen": {"a": "2026-01-01"}}),))])), {"a": "2026-01-01"})
        self.assertEqual(ma.load_seen(_Cur([("FROM service_config", ({"seen": {"b": "x"}},))])), {"b": "x"})
        self.assertEqual(ma.load_seen(_Cur()), {})


class TestIntegration(unittest.TestCase):
    def test_weighing_is_borrowed_from_weight_of_memory(self):
        import nova_weight_of_memory as wom
        self.assertEqual((ma.OPS_DSN, ma.MEMSRV), (wom.OPS_DSN, wom.MEMSRV))
        self.assertIn("wom.gather(cur, today)", SRC); self.assertIn("wom.rank_weighty(items)", SRC)
        self.assertNotIn("def weight(", SRC)                                   # one definition of "matters"
        items = wom.gather(_cur(preoccs=[HEAVY]), TODAY)
        heaviest = [h["key"] for h in wom.rank_weighty(items)]
        to_set, _, after = ma.decide({}, heaviest, {i["key"]: i["weight"] for i in items}, TODAY)
        self.assertEqual(to_set, ["preocc:1"])                                 # the key letting-go's guard reads
        self.assertIn("key LIKE 'preocc:%'", (SCRIPTS / "nova_letting_go.py").read_text())

    def test_held_keys_and_state_row(self):
        cur = _Cur([("FROM memory_anchors", [("preocc:1",), ("preocc:2",)])])
        self.assertEqual(ma.held_keys(cur), {"preocc:1", "preocc:2"})
        self.assertIn("released_at IS NULL", cur.sql[0][0])
        ma.save_seen(cur, {"sig": "2026-01-01"})
        sql, params = cur.ran("INSERT INTO service_config")[0]
        self.assertEqual(params[:2], (ma.STATE_SERVICE, ma.STATE_KEY)); self.assertEqual(json.loads(params[2]), {"seen": {"sig": "2026-01-01"}})


class TestFunctional(unittest.TestCase):
    def test_golden_path_sets_an_anchor_and_states_it(self):
        cur = _cur(preoccs=[HEAVY])
        rc, remember, out = _main(cur)
        self.assertEqual(rc, 0)
        sql, params = cur.ran("INSERT INTO memory_anchors")[0]
        self.assertEqual(params[:3], ("preocc:1", "the failing disk", TODAY))
        self.assertGreater(json.loads(params[3])["weight"], 0)
        text, meta = remember.call_args[0]
        self.assertIn("the failing disk — anchored today", text)
        self.assertEqual((meta["set"], meta["held"], meta["organ"]), (["preocc:1"], ["preocc:1"], "nova_memory_anchor"))
        self.assertEqual(json.loads(_state_rows(cur, ma.STATE_SERVICE)[0][2])["seen"], {ma.anchor_sig({"preocc:1": 1}): TODAY.isoformat()})

    def test_release_path_after_a_long_zero(self):
        cur = _cur(anchors=[("preocc:9", "old radios", TODAY - timedelta(days=300), TODAY - timedelta(days=ma.RELEASE_AFTER_DAYS)),
                            ("preocc:1", "the failing disk", TODAY - timedelta(days=5), None)], preoccs=[HEAVY])
        rc, remember, _ = _main(cur)
        self.assertEqual(cur.ran("UPDATE memory_anchors SET released_at=now()")[0][1], ("preocc:9",))
        text, meta = remember.call_args[0]
        self.assertIn("Let go of the anchor on: old radios", text)
        self.assertEqual((meta["released"], meta["held"]), (["preocc:9"], ["preocc:1"]))
        self.assertEqual(cur.ran("INSERT INTO memory_anchors"), [])

    def test_unchanged_set_holds_quietly(self):
        sig = ma.anchor_sig({"preocc:1": 1})
        cur = _cur(anchors=[("preocc:1", "the failing disk", TODAY - timedelta(days=5), None)], preoccs=[HEAVY], seen={sig: TODAY.isoformat()})
        rc, remember, out = _main(cur)
        remember.assert_not_called()
        self.assertIn("holding quietly", out)
        self.assertEqual(_state_rows(cur, ma.STATE_SERVICE), [])

    def test_dry_run_and_report_write_nothing(self):
        cur = _cur(preoccs=[HEAVY])
        rc, remember, out = _main(cur, ["x", "--dry-run"])
        self.assertIn("the failing disk", out); remember.assert_not_called(); _main.weigh.assert_not_called()
        self.assertIn("The weight of what I remember", out)                     # the weighing pass prints too
        self.assertEqual(cur.ran("INSERT INTO"), [])
        cur = _cur(anchors=[("preocc:1", "the failing disk", TODAY, None)])
        rc, remember, out = _main(cur, ["x", "--report"])
        self.assertIn("1 anchor(s) held", out); self.assertEqual(cur.ran("FROM preoccupations"), [])
        _main.weigh.assert_not_called()


class TestWeightOfMemoryMerged(unittest.TestCase):
    """Integration + Functional for the M12 merge: one weighing feeds both passes, outputs unchanged."""

    def test_one_read_two_memories_each_under_its_own_source_and_state(self):
        cur = _cur(preoccs=[HEAVY])
        rc, remember, out = _main(cur)
        self.assertEqual(rc, 0)
        self.assertEqual(len(cur.ran("FROM preoccupations")), 1)               # ONE weighing per pass
        text, meta = _main.weigh.call_args[0]
        self.assertIn("the failing disk — returned to 22x", text)
        self.assertEqual((meta["organ"], meta["heaviest"]), ("nova_weight_of_memory", ["preocc:1"]))
        self.assertEqual(len(_state_rows(cur, "nova_weight_of_memory")), 1)
        self.assertEqual(remember.call_args[0][1]["organ"], "nova_memory_anchor")

    def test_weight_mode_runs_only_the_weighing_pass(self):
        cur = _cur(preoccs=[HEAVY])
        rc, remember, out = _main(cur, ["x", "--weight"])
        self.assertEqual(rc, 0)
        _main.weigh.assert_called_once(); remember.assert_not_called()
        self.assertEqual(cur.ran("memory_anchors"), [])                        # no schema, no anchor reads or writes

    def test_weight_memory_goes_out_under_weight_of_memory_source(self):
        import nova_weight_of_memory as wom
        posted = []

        def ok(req, timeout=0):
            posted.append(json.loads(req.data)); return _Resp({"id": 1})
        with patch("urllib.request.urlopen", ok):
            ma.wom.remember("t", {"organ": "x"})
        self.assertEqual(posted[0]["source"], wom.SOURCE)

    def test_a_failing_weighing_pass_never_stops_the_anchoring(self):
        cur = _cur(preoccs=[HEAVY])
        rc, remember, out = _main(cur, weigh_remember=MagicMock(side_effect=OSError("memory server down")))
        self.assertEqual(rc, 1)
        self.assertIn("weighing pass failed", out)
        remember.assert_called_once()
        self.assertEqual(len(cur.ran("INSERT INTO memory_anchors")), 1)

    def test_unchanged_heaviest_set_is_not_restated(self):
        import nova_weight_of_memory as wom
        heaviest = wom.rank_weighty(wom.gather(_cur(preoccs=[HEAVY]), TODAY))
        cur = _cur(preoccs=[HEAVY], seen={wom.weigh_sig(heaviest): TODAY.isoformat()})
        rc, remember, out = _main(cur, ["x", "--weight"])
        self.assertIn("heaviest set unchanged", out)
        _main.weigh.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_selftest_runs_and_import_is_guarded(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all memory-anchor assertions passed", r.stdout)
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
