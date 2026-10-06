#!/usr/bin/env python3
"""Tests for nova_fm_capture.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
The module reads sys.argv and imports SoapySDR at load, so it is loaded with a fake argv (OUT_DIR in a
tempdir) and a stub SoapySDR set into sys.modules only for the load (that one key restored after).
No radio hardware is touched; main()'s endless read loop is broken by the mocked readStream."""
import atexit
import importlib.util
import shutil
import os
import re
import sys
import tempfile
import time
import types
import unittest
import wave
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import scipy.signal  # noqa: F401  (imported before the load so nothing Cython is added under the stub)

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_fm_capture.py"
SRC = SCRIPT.read_text()
TMP = tempfile.mkdtemp(prefix="fmcap_")
atexit.register(shutil.rmtree, TMP, True)


@contextmanager
def _only_key(name, value):
    missing = object()
    old = sys.modules.get(name, missing)
    sys.modules[name] = value  # restored below to the SAME object (or removed)
    try:
        yield
    finally:
        if old is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old


def _fake_soapy():
    m = types.ModuleType("SoapySDR")
    m.SOAPY_SDR_RX, m.SOAPY_SDR_CF32 = 0, "CF32"
    m.Device = MagicMock()
    return m


def _load(targets):
    spec = importlib.util.spec_from_file_location("nova_fm_capture_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with _only_key("SoapySDR", _fake_soapy()), patch.object(sys, "argv", ["x", "SER1", TMP, *targets]):
        spec.loader.exec_module(mod)
    return mod


fm = _load(["NOAA:162550000:A"])


class TestSecurity(unittest.TestCase):
    def test_no_credentials_or_network(self):
        self.assertIsNone(re.search(r"(password|token|secret)\s*=", SRC, re.I))
        self.assertNotIn("urlopen", SRC)
        self.assertNotIn("requests", SRC)

    def test_label_spaces_sanitized_in_filename(self):
        with tempfile.TemporaryDirectory() as d, patch.object(fm, "OUT_DIR", d):
            c = fm.ChannelState("Fire Dispatch", 154e6, 0, 8000)
            c.buf = [np.full(800, 1000, "<i2")]
            c._flush()
            names = os.listdir(d)
        self.assertEqual(len(names), 1)
        self.assertNotIn(" ", names[0])
        self.assertTrue(names[0].endswith("__NFM__Fire_Dispatch.wav"))


class TestPerformance(unittest.TestCase):
    def test_demod_large_block_fast(self):
        iq = (np.exp(1j * np.linspace(0, 400, 200_000))).astype(np.complex64)
        t0 = time.perf_counter()
        for _ in range(5):
            audio, prev = fm.fm_demod_chunk(iq, np.complex64(1), fm.DECIM_STAGES)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(len(audio), 200_000 // 40)


class TestRetry(unittest.TestCase):
    def test_missing_device_exits_cleanly(self):
        # RETRY GAP: open_device() — one enumerate; no matching mode -> sys.exit with a message
        fm.SoapySDR.Device.enumerate = MagicMock(return_value=[{"mode": "DT"}])
        with self.assertRaises(SystemExit) as e:
            fm.open_device(dual=False)
        self.assertIn("no MA-mode RSPduo", str(e.exception.code))
        self.assertEqual(fm.SoapySDR.Device.enumerate.call_count, 1)

    def test_short_reads_are_skipped_not_fatal(self):
        sdr = self._sdr([SimpleNamespace(ret=-1), SimpleNamespace(ret=0), KeyboardInterrupt()])
        with patch.object(fm, "open_device", return_value=sdr), patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                fm.main()
        self.assertEqual(sdr.readStream.call_count, 3)
        sdr.closeStream.assert_called_once()

    @staticmethod
    def _sdr(reads):
        sdr = MagicMock()
        sdr.getSampleRate.return_value = 2_000_000
        sdr.readStream.side_effect = reads
        return sdr


class TestUnit(unittest.TestCase):
    def test_demod_output_is_int16_and_carries_prev(self):
        iq = np.ones(400, np.complex64)
        audio, prev = fm.fm_demod_chunk(iq, np.complex64(1), (5, 8))
        self.assertEqual(audio.dtype, np.dtype("<i2"))
        self.assertEqual(len(audio), 10)
        self.assertEqual(prev, iq[-1])
        self.assertTrue(np.all(np.abs(audio) <= 32767))

    def test_silent_chunk_not_written(self):
        with tempfile.TemporaryDirectory() as d, patch.object(fm, "OUT_DIR", d):
            c = fm.ChannelState("x", 1e6, 0, 8000)
            c.buf = [np.zeros(800, "<i2")]
            c._flush()
            self.assertEqual(os.listdir(d), [])
            self.assertEqual(c.buf, [])

    def test_feed_flushes_only_after_chunk_time(self):
        c = fm.ChannelState("x", 1e6, 0, 8000)
        with patch.object(c, "_flush") as fl:
            c.feed(np.zeros(10, "<i2"))
            fl.assert_not_called()
            c.chunk_start -= fm.CHUNK_S
            c.feed(np.zeros(10, "<i2"))
            fl.assert_called_once()


class TestIntegration(unittest.TestCase):
    def test_wav_header_matches_audio_rate(self):
        with tempfile.TemporaryDirectory() as d, patch.object(fm, "OUT_DIR", d):
            c = fm.ChannelState("wx", 162.55e6, 0, 50000.4)
            c.buf = [np.full(1000, 2000, "<i2")]
            c._flush()
            (name,) = os.listdir(d)
            with wave.open(os.path.join(d, name)) as w:
                self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()),
                                 (1, 2, 50000, 1000))
        self.assertIn("__162.5500__NFM__wx", name)

    def test_dual_mode_detected_from_tuner_numbers(self):
        dual = _load(["A:100000000:1", "B:200000000:2"])
        sdr = MagicMock(); sdr.getSampleRate.return_value = 2_000_000
        sdr.readStream.side_effect = KeyboardInterrupt()
        with patch.object(dual, "open_device", return_value=sdr) as od, patch("builtins.print"):
            with self.assertRaises(KeyboardInterrupt):
                dual.main()
        od.assert_called_once_with(True)
        self.assertEqual(sdr.setupStream.call_count, 2)
        sdr.setAntenna.assert_not_called()


class TestFunctional(unittest.TestCase):
    def test_main_single_channel_tunes_reads_and_tears_down(self):
        sdr = MagicMock(); sdr.getSampleRate.return_value = 2_000_000
        n = 1920

        def read(stream, bufs, size, timeoutUs):
            bufs[0][:n] = np.exp(1j * np.linspace(0, 50, n)).astype(np.complex64)
            return SimpleNamespace(ret=n)
        sdr.readStream.side_effect = [read(None, [np.empty(19200, np.complex64)], 0, 0), KeyboardInterrupt()]
        with patch.object(fm, "open_device", return_value=sdr), patch("builtins.print") as p:
            with self.assertRaises(KeyboardInterrupt):
                fm.main()
        sdr.setAntenna.assert_called_once_with(0, 0, "A")
        sdr.setFrequency.assert_called_once_with(0, 0, 162550000.0)
        sdr.deactivateStream.assert_called_once(); sdr.closeStream.assert_called_once()
        self.assertIn("50000 Hz audio", p.call_args_list[0][0][0])


class TestFrame(unittest.TestCase):
    def test_load_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        m = _load(["X:1000000:A"])
        m.SoapySDR.Device.assert_not_called()
        self.assertTrue(os.path.isdir(TMP))

    def test_compiles(self):
        compile(SRC, str(SCRIPT), "exec")
        self.assertTrue(SRC.startswith("#!/usr/bin/env python3"))


if __name__ == "__main__":
    unittest.main()
