#!/usr/bin/env python3
"""Tests for nrsp_sweep.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nrsp_sweep.py"
SRC = SCRIPT.read_text()

# pynrsp (SDRconnect websocket client) is not a test dependency; stub it so nothing ever talks to a radio.
PKG = types.ModuleType("pynrsp")
CLIENT_MOD = types.ModuleType("pynrsp.client"); CLIENT_MOD.Client = MagicMock(name="Client")
CONST = types.ModuleType("pynrsp.const")
CONST.PROP_SIGNAL_SNR, CONST.PROP_SIGNAL_POWER, CONST.PROP_CAN_CONTROL = "snr", "power", "can_control"
PKG.client, PKG.const = CLIENT_MOD, CONST
STUBS = {"pynrsp": PKG, "pynrsp.client": CLIENT_MOD, "pynrsp.const": CONST}


def _load():
    spec = importlib.util.spec_from_file_location("nrsp", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # FREQ/DEMOD are parsed from argv at import time
    with patch.dict(sys.modules, STUBS), patch.object(sys, "argv", ["nrsp_sweep.py", "506.9375", "NFM"]):
        spec.loader.exec_module(mod)
    return mod


nr = _load()


class _Clock:
    """Fake time: sleep() advances time(); no real waiting."""
    def __init__(self):
        self.now = 1000.0; self.slept = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s); self.now += s


class _Radio:
    """SDRconnect client stand-in with a per-antenna SNR table."""
    def __init__(self, snr=None, fail_gets=0, antennas=None):
        self._cache = {}; self.calls = []; self.snr = snr or {}; self.fail_gets = fail_gets
        self.ant = None; self._antennas = antennas; self._running = True; self._ws = MagicMock()

    def __getattr__(self, name):
        def rec(*a, **k):
            self.calls.append((name, a))
        return rec

    def set_antenna(self, name):
        self.calls.append(("set_antenna", (name,))); self.ant = name

    def antennas(self, timeout=None):
        return self._antennas

    def get_property(self, prop, timeout=None):
        if prop == CONST.PROP_CAN_CONTROL:
            return "true"
        if self.fail_gets > 0:
            self.fail_gets -= 1
            raise TimeoutError("ws jammed")
        return self.snr.get(self.ant, -1.0) if prop == "snr" else -40.0


def _run_main(radio, clock=None):
    clock = clock or _Clock()
    out, err = io.StringIO(), io.StringIO()
    with patch.object(nr, "Client", return_value=radio), patch.object(nr, "time", clock), \
         redirect_stdout(out), redirect_stderr(err):
        nr.main()
    return out.getvalue(), err.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_local_only(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('Client("127.0.0.1")', SRC)
        self.assertNotRegex(SRC, r"subprocess|os\.system|eval\(|exec\(")

    def test_frequency_arg_must_be_numeric(self):
        with patch.dict(sys.modules, STUBS), patch.object(sys, "argv", ["x", "506; rm -rf /"]):
            spec = importlib.util.spec_from_file_location("nrsp_bad", SCRIPT)
            with self.assertRaises(ValueError):
                spec.loader.exec_module(importlib.util.module_from_spec(spec))

    def test_device_always_restored_even_on_failure(self):
        # the sweep hijacks the 2nd RSPduo; the finally block must hand the Dual device back
        radio = _Radio()
        radio.set_demodulator = MagicMock(side_effect=RuntimeError("demod rejected"))
        with self.assertRaises(RuntimeError):
            _run_main(radio)
        self.assertIn(("select_device", (nr.RESTORE_INDEX,)), radio.calls)
        self.assertIn(("device_stream_enable", (False,)), radio.calls)


class TestPerformance(unittest.TestCase):
    def test_num_10k_values_fast_and_sweep_bounded(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            nr._num(str(i)); nr._num(None); nr._num("x")
        self.assertLess(time.perf_counter() - t0, 1.0)
        clock = _Clock(); radio = _Radio(snr={"A": 5.0})
        with patch.object(nr, "time", clock):
            r = nr.sweep(radio, "A")
        samples = int(nr.SAMPLE_WIN / nr.SAMPLE_DT) + 1
        self.assertLessEqual(r["n"], samples)                          # sampling window is bounded


class TestRetry(unittest.TestCase):
    def test_get_fails_twice_then_sampling_recovers(self):
        radio = _Radio(snr={"A": 12.0}, fail_gets=2)
        clock = _Clock()
        with patch.object(nr, "time", clock):
            r = nr.sweep(radio, "A")
        self.assertEqual(radio.fail_gets, 0)
        self.assertGreater(r["n"], 0)
        self.assertEqual(r["snr_med"], 12.0)

    def test_read_metric_fails_open_to_none(self):
        radio = _Radio(fail_gets=99)
        self.assertIsNone(nr.read_metric(radio, "snr"))


class TestUnit(unittest.TestCase):
    def test_num(self):
        self.assertEqual(nr._num("3.5"), 3.5)
        self.assertIsNone(nr._num(None)); self.assertIsNone(nr._num("abc"))

    def test_read_metric_prefers_cache(self):
        radio = _Radio(fail_gets=99); radio._cache["snr"] = "7.25"
        self.assertEqual(nr.read_metric(radio, "snr"), 7.25)

    def test_sweep_no_samples(self):
        radio = _Radio(fail_gets=10_000)
        with patch.object(nr, "time", _Clock()):
            r = nr.sweep(radio, "A")
        self.assertEqual(r, {"antenna": "A", "snr_med": None, "snr_max": None, "pwr_med": None, "n": 0})

    def test_argv_parse(self):
        self.assertEqual((nr.FREQ, nr.DEMOD), (506.9375e6, "NFM"))


class TestIntegration(unittest.TestCase):
    def test_tunes_then_sweeps_each_antenna(self):
        radio = _Radio(snr={"Tuner 1 50 ohm": 4.0, "Tuner 2 50 ohm": 9.0}, antennas=None)
        out, _ = _run_main(radio)
        names = [n for n, _ in radio.calls]
        self.assertLess(names.index("set_frequency"), names.index("set_antenna"))
        self.assertIn(("select_device", (nr.DEV_INDEX,)), radio.calls)
        self.assertEqual([a[0] for n, a in radio.calls if n == "set_antenna"], nr.ANTENNAS)   # fallback list


class TestFunctional(unittest.TestCase):
    def test_golden_path_ranks_best_tuner(self):
        radio = _Radio(snr={"T1": 4.0, "T2": 9.5}, antennas=["T1", "T2"])
        out, err = _run_main(radio)
        self.assertIn("freq=506.9375 MHz demod=NFM  can_control=true", out)
        self.assertLess(out.index("T2"), out.index("T1 "))
        self.assertTrue(out.strip().endswith("BEST: T2"))
        self.assertEqual(err, "")

    def test_no_snr_prints_no_best(self):
        radio = _Radio(antennas=["T1"], fail_gets=10_000)
        out, _ = _run_main(radio)
        self.assertNotIn("BEST:", out)


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        # no --help (argv is positional freq/demod); import with stubbed pynrsp in a throwaway process
        code = ("import sys,types,importlib.util as u; from unittest.mock import MagicMock;"
                "p=types.ModuleType('pynrsp'); c=types.ModuleType('pynrsp.client'); k=types.ModuleType('pynrsp.const');"
                "c.Client=MagicMock(side_effect=AssertionError('main ran')); k.PROP_SIGNAL_SNR=k.PROP_SIGNAL_POWER=k.PROP_CAN_CONTROL='x';"
                "sys.modules.update({'pynrsp':p,'pynrsp.client':c,'pynrsp.const':k}); sys.argv=['nrsp_sweep.py','100','AM'];"
                f"s=u.spec_from_file_location('m', {str(SCRIPT)!r}); m=u.module_from_spec(s); s.loader.exec_module(m)")
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
