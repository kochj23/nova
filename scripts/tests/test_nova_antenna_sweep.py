#!/usr/bin/env python3
"""Tests for nova_antenna_sweep.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_antenna_sweep.py"
SRC = SCRIPT.read_text()


def _soapy_stub():
    m = types.ModuleType("SoapySDR")
    m.SOAPY_SDR_RX = 1
    m.SOAPY_SDR_CS16 = "CS16"
    m.Device = mock.MagicMock(name="Device")
    return m


SOAPY = _soapy_stub()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # SoapySDR is not installed on the test host and the script reads sys.argv[1] at import:
    # both are supplied only for the duration of exec_module (sys.modules is restored after).
    with mock.patch.dict(sys.modules, {"SoapySDR": SOAPY}), \
         mock.patch.object(sys, "argv", ["nova_antenna_sweep.py", "TESTSERIAL"]):
        spec.loader.exec_module(mod)
    return mod


A = _load("antenna_under_test", SCRIPT)


class _SR:
    def __init__(self, ret):
        self.ret = ret


def _tone(n, freq=0.1, amp=20000.0):
    t = np.arange(n)
    return (np.exp(2j * np.pi * freq * t) * amp).astype(np.complex64)


def _fake_sdr(rets):
    """An SDR whose readStream pops `rets` (samples read, <=0 = nothing) and fills a clean tone."""
    sdr = mock.MagicMock(name="sdr")
    rets = list(rets)

    def read(stream, bufs, n, timeoutUs=0):
        r = rets.pop(0)
        if r > 0:
            sig = _tone(r)
            raw = bufs[0]
            raw[0:2 * r:2] = sig.real.astype(np.int16)
            raw[1:2 * r:2] = sig.imag.astype(np.int16)
        return _SR(r)

    sdr.readStream.side_effect = read
    return sdr


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_serial_comes_from_argv_not_source(self):
        self.assertEqual(A.SERIAL, "TESTSERIAL")
        self.assertNotRegex(SRC, r"SERIAL\s*=\s*['\"]")

    def test_no_network_or_shell_surface(self):
        for needle in ("urlopen", "subprocess", "shell=True", "psycopg2"):
            self.assertNotIn(needle, SRC)

    def test_enumerate_filter_is_driver_scoped(self):
        self.assertIn('f"driver=sdrplay,serial={SERIAL}"', SRC)


class TestPerformance(unittest.TestCase):
    def test_snr_fft_on_capture_size_is_fast(self):
        iq = _tone(A.CAPTURE_N)
        t0 = time.perf_counter()
        for _ in range(20):
            A.snr_db(iq)
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_sweep_has_bounded_capture_loop(self):
        self.assertIn("for _ in range(3)", SRC)          # three captures, never an open-ended read loop


class TestRetry(unittest.TestCase):
    def test_empty_reads_fail_open_to_none(self):
        # RETRY GAP: sweep_antenna/readStream — exactly three captures are attempted; if every read returns
        # <=0 samples the port is reported with snr_db=None instead of being re-read or raising.
        sdr = _fake_sdr([0, -1, 0])
        with mock.patch.object(A.time, "sleep"):
            r = A.sweep_antenna(sdr, "Tuner 1 50 ohm", "band", 1e8, 2e6)
        self.assertEqual(sdr.readStream.call_count, 3)
        self.assertIsNone(r["snr_db"])
        self.assertIsNone(r["signal_db"])
        sdr.deactivateStream.assert_called_once()
        sdr.closeStream.assert_called_once()

    def test_partial_reads_still_report_a_median(self):
        sdr = _fake_sdr([4096, 0, 4096])
        with mock.patch.object(A.time, "sleep"):
            r = A.sweep_antenna(sdr, "Tuner 2 50 ohm", "band", 1e8, 2e6)
        self.assertIsNotNone(r["snr_db"])


class TestUnit(unittest.TestCase):
    def test_snr_of_pure_tone_is_high(self):
        snr, peak = A.snr_db(_tone(4096))
        self.assertGreater(snr, 40.0)
        self.assertIsInstance(peak, float)

    def test_snr_of_white_noise_is_low(self):
        rng = np.random.default_rng(7)
        noise = (rng.standard_normal(4096) + 1j * rng.standard_normal(4096)).astype(np.complex64)
        snr, _ = A.snr_db(noise)
        self.assertLess(snr, 20.0)

    def test_snr_rounds_to_two_places(self):
        snr, peak = A.snr_db(_tone(1024))
        self.assertEqual(snr, round(snr, 2))
        self.assertEqual(peak, round(peak, 2))

    def test_sweep_configures_the_port_and_reports_median(self):
        sdr = _fake_sdr([4096, 4096, 4096])
        with mock.patch.object(A.time, "sleep") as slp:
            r = A.sweep_antenna(sdr, "Tuner 1 50 ohm", "VHF", 106_700_000, 2_000_000)
        sdr.setAntenna.assert_called_once_with(A.SOAPY_SDR_RX, 0, "Tuner 1 50 ohm")
        sdr.setFrequency.assert_called_once_with(A.SOAPY_SDR_RX, 0, 106_700_000)
        sdr.setSampleRate.assert_called_once_with(A.SOAPY_SDR_RX, 0, 2_000_000)
        self.assertEqual(r["antenna"], "Tuner 1 50 ohm")
        self.assertEqual(r["band"], "VHF")
        self.assertGreater(r["snr_db"], 40.0)
        self.assertGreaterEqual(slp.call_count, 4)      # settle + per-capture pauses


class TestIntegration(unittest.TestCase):
    def test_bands_and_antennas_are_well_formed(self):
        self.assertEqual(len(A.ANTENNAS), 2)
        self.assertEqual(len(A.BANDS), 3)
        for label, freq, rate in A.BANDS:
            self.assertIsInstance(label, str)
            self.assertGreater(freq, 1_000_000)
            self.assertEqual(rate, 2_000_000)

    def test_sweep_output_feeds_mains_ranking(self):
        rows = [{"antenna": a, "band": "b", "snr_db": s, "signal_db": 0.0}
                for a, s in (("Tuner 1 50 ohm", 10.0), ("Tuner 2 50 ohm", 25.5))]
        ranked = sorted([r for r in rows if r["snr_db"] is not None], key=lambda r: r["snr_db"], reverse=True)
        self.assertEqual(ranked[0]["antenna"], "Tuner 2 50 ohm")
        self.assertIn('key=lambda r: r["snr_db"], reverse=True', SRC)


class TestFunctional(unittest.TestCase):
    def setUp(self):
        SOAPY.Device.reset_mock()

    def test_main_sweeps_every_band_and_port_and_prints_json(self):
        SOAPY.Device.enumerate.return_value = [{"mode": "SL"}, {"mode": "MA", "serial": "TESTSERIAL"}]
        calls = []

        def fake_sweep(sdr, antenna, label, freq, rate):
            calls.append((antenna, label))
            return {"antenna": antenna, "band": label, "snr_db": 12.0 if "1" in antenna else 30.0, "signal_db": -20.0}

        buf = io.StringIO()
        with mock.patch.object(A, "sweep_antenna", fake_sweep), redirect_stdout(buf):
            A.main()
        out = buf.getvalue()
        self.assertEqual(len(calls), len(A.BANDS) * len(A.ANTENNAS))
        self.assertEqual(out.count("BEST: Tuner 2 50 ohm"), len(A.BANDS))
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(payload["serial"], "TESTSERIAL")
        self.assertEqual(len(payload["results"]), 6)
        SOAPY.Device.assert_called_once_with({"mode": "MA", "serial": "TESTSERIAL"})

    def test_main_exits_when_no_master_unit_matches(self):
        SOAPY.Device.enumerate.return_value = [{"mode": "SL"}]
        with self.assertRaises(SystemExit) as cm:
            A.main()
        self.assertIn("no Master-mode RSPduo", str(cm.exception))
        SOAPY.Device.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys, types, unittest.mock as m\n"
                "s = types.ModuleType('SoapySDR'); s.SOAPY_SDR_RX = 1; s.SOAPY_SDR_CS16 = 'CS16'; s.Device = m.MagicMock()\n"
                "sys.modules['SoapySDR'] = s; sys.argv = ['nova_antenna_sweep.py', 'X']\n"
                "import nova_antenna_sweep\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_script_without_serial_fails_fast(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertNotEqual(r.returncode, 0)     # no serial / no SoapySDR: never silently sweeps


if __name__ == "__main__":
    unittest.main()
