#!/usr/bin/env python3
"""7-category tests for the wish #70 change to nova_affect.py: signal_good_thing (Trixie's Joy —
today's evidenced good thing is a small, honest valence lift; none is neutral, never negative).
Security, Performance, Retry, Unit, Integration, Functional, Frame. Offline: every DB cursor is a
fake, no LLM/HTTP, no writes. Written by Jordan Koch (via Claude)."""
import contextlib
import inspect
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_affect as aff  # noqa: E402

SIG_SRC = inspect.getsource(aff.signal_good_thing)


class _Cur:
    """good_things fake: `row` answers the read; `fail` transient errors before it succeeds."""
    def __init__(self, row=None, table=True, fail=0):
        self.row, self.table, self.fail, self.sql = row, table, fail, []
        self.connection = mock.MagicMock()
        self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if "to_regclass" in sql:
            self._last = ("public.good_things",) if self.table else (None,)
            return
        if "FROM good_things" in sql and self.fail:
            self.fail -= 1
            raise RuntimeError("server closed the connection unexpectedly")
        self._last = self.row

    def fetchone(self):
        return self._last


def _sig(cur):
    with mock.patch.object(aff.time, "sleep") as sl:
        s = aff.signal_good_thing(cur)
    assert len(s) == 1
    return s[0], sl


class TestSecurity(unittest.TestCase):
    def test_read_only_and_unparameterised_constant_sql(self):
        # The signal never writes and has no caller-controlled SQL at all.
        self.assertNotRegex(SIG_SRC, r"INSERT|UPDATE|DELETE|DROP")
        self.assertNotRegex(SIG_SRC, r'execute\(\s*f"')
        cur = _Cur(row=("x", "y"))
        _sig(cur)
        self.assertTrue(all(p is None for s, p in cur.sql if "FROM good_things" in s))

    def test_hostile_row_text_is_data_not_sql(self):
        cur = _Cur(row=("'; DROP TABLE affect_state; --", "trace:abc"))
        s, _ = _sig(cur)
        self.assertEqual(len([x for x, _ in cur.sql if "DROP" in x]), 0)
        self.assertIn("DROP TABLE", s["note"])  # carried as evidence text only

    def test_never_pushes_valence_negative(self):
        for cur in (_Cur(row=None), _Cur(table=False), _Cur(fail=9), _Cur(row=("a", "b"))):
            s, _ = _sig(cur)
            self.assertGreaterEqual(s["dv"], 0)
            self.assertEqual(s["da"], 0)


class TestPerformance(unittest.TestCase):
    def test_single_bounded_query(self):
        self.assertIn("LIMIT 1", SIG_SRC)
        self.assertIn("interval '24 hours'", SIG_SRC)

    def test_10k_reads_fast(self):
        t = time.monotonic()
        for _ in range(10000):
            aff.signal_good_thing(_Cur(row=("a mild day", "telemetry.weather")))
        self.assertLess(time.monotonic() - t, 2.0)


class TestRetry(unittest.TestCase):
    def test_transient_error_retries_then_succeeds(self):
        cur = _Cur(row=("shipped wish #70", "feature_wishes:70"), fail=2)
        s, sl = _sig(cur)
        self.assertTrue(s["usable"])
        self.assertEqual(s["dv"], aff.WEIGHTS["good_thing_v"])
        self.assertEqual(len([1 for x, _ in cur.sql if "FROM good_things" in x]), 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [0.5, 1.0])  # backoff grows
        self.assertEqual(cur.connection.rollback.call_count, 2)

    def test_gives_up_after_three_unusable_not_raised(self):
        cur = _Cur(fail=99)
        s, sl = _sig(cur)
        self.assertFalse(s["usable"])
        self.assertEqual(s["dv"], 0)
        self.assertEqual(len([1 for x, _ in cur.sql if "FROM good_things" in x]), 3)
        self.assertEqual(sl.call_count, 2)


class TestUnit(unittest.TestCase):
    def test_weight_is_small(self):
        self.assertEqual(aff.WEIGHTS["good_thing_v"], 0.06)

    def test_absent_table_unusable(self):
        s, _ = _sig(_Cur(table=False))
        self.assertFalse(s["usable"])
        self.assertIn("absent", s["note"])

    def test_none_logged_is_neutral_but_usable(self):
        s, _ = _sig(_Cur(row=None))
        self.assertTrue(s["usable"])
        self.assertEqual((s["value"], s["dv"]), (0, 0))
        self.assertIn("neutral", s["note"])

    def test_present_cites_evidence(self):
        s, _ = _sig(_Cur(row=("A mild clean-air day.", "telemetry.weather:24h")))
        self.assertEqual(s["value"], 1)
        self.assertIn("telemetry.weather:24h", s["note"])


class TestIntegration(unittest.TestCase):
    def test_compute_affect_folds_good_thing_last(self):
        others = [n for n in dir(aff) if n.startswith("signal_") and n != "signal_good_thing"]
        with contextlib.ExitStack() as st:
            for n in others:
                st.enter_context(mock.patch.object(aff, n, return_value=[]))
            st.enter_context(mock.patch.object(aff, "llm", return_value=""))
            out = aff.compute_affect(_Cur(row=("x", "y")), None)
        self.assertEqual([s["signal"] for s in out["signals"]], ["good_thing"])

    def test_combine_adds_exactly_the_weight(self):
        base = [aff.sig("resonance", 1, 0, 0.2, 0.05, "warm")]
        gt = aff.signal_good_thing(_Cur(row=("x", "y")))
        v0 = aff.combine(base)[0]
        v1 = aff.combine(base + gt)[0]
        self.assertAlmostEqual(v1 - v0, aff.WEIGHTS["good_thing_v"], places=3)


class TestFunctional(unittest.TestCase):
    def _state(self, cur):
        busy = [aff.sig("resonance", 1, 0, 0.12, 0.05, "warm words"),
                aff.sig("infra_health", 1, 0, 0.05, 0.0, "healthy")]
        with mock.patch.object(aff, "compute_affect", wraps=aff.compute_affect), \
             contextlib.ExitStack() as st:
            for n in [n for n in dir(aff) if n.startswith("signal_") and n != "signal_good_thing"]:
                st.enter_context(mock.patch.object(aff, n, return_value=[]))
            st.enter_context(mock.patch.object(aff, "signal_resonance", return_value=busy))
            st.enter_context(mock.patch.object(aff, "llm", return_value=""))
            st.enter_context(mock.patch.object(aff.time, "sleep"))
            return aff.compute_affect(cur, None)

    def test_golden_good_day_reads_warmer_with_evidence(self):
        with_gt = self._state(_Cur(row=("Wish #70 shipped.", "feature_wishes:70")))
        without = self._state(_Cur(row=None))
        self.assertGreater(with_gt["valence"], without["valence"])
        self.assertTrue(any("Wish #70 shipped" in e for e in aff._evidence_strings(with_gt)))

    def test_error_path_unreadable_still_computes(self):
        st = self._state(_Cur(fail=99))
        self.assertIn("valence", st)
        self.assertIn("good_thing", [s["signal"] for s in st["signals"]])


class TestFrame(unittest.TestCase):
    def test_demo_neutral_exits_zero_without_db(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_affect.py"), "--demo-neutral"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])

    def test_import_is_guarded(self):
        src = (SCRIPTS / "nova_affect.py").read_text()
        self.assertIn('if __name__ == "__main__":', src)
        self.assertNotRegex(src, r"/Users/[a-z]")


if __name__ == "__main__":
    unittest.main()
