#!/usr/bin/env python3
"""Tests for nova_rf_discovery.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

pynrsp is a stub module for the load only (keys restored), HOME points at a tempdir so the import-time
~/nrsp2_calls mkdir and ~/.cell_watch_seen.json never touch the real home, the module's nova_config is a
local proxy (post_both / notify_local are MagicMocks), the SDR Client is a MagicMock and time.sleep is a
no-op. The phase-2 cellular decode (Popen) is proven gated off."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_rf_discovery.py"
SRC = SCRIPT.read_text()
TMP = tempfile.TemporaryDirectory()


def _stubs():
    pkg = types.ModuleType("pynrsp")
    pkg.Client = MagicMock()
    const = types.ModuleType("pynrsp.const")
    const.PROP_SIGNAL_SNR, const.PROP_CAN_CONTROL = "signal_snr", "can_control"
    pkg.const = const
    return {"pynrsp": pkg, "pynrsp.const": const}


def _load():
    stubs = _stubs()
    saved = {k: sys.modules.get(k) for k in stubs}
    try:
        with patch.dict(sys.modules, stubs), patch.dict(os.environ, {"HOME": TMP.name}):
            spec = importlib.util.spec_from_file_location("nova_rf_discovery_t", SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    mod.nova_config = types.SimpleNamespace(post_both=MagicMock(), notify_local=MagicMock(), SLACK_BB="C_BB")
    return mod


rf = _load()


def _frame(n=512, floor=30, peaks=()):
    b = bytearray([floor] * n)
    for i, lvl in peaks:
        b[i] = lvl
    return bytes(b)


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        for p in (patch.object(rf.time, "sleep"), patch.object(rf, "SEEN_PATH", str(Path(self.td.name) / "seen.json")),
                  patch.object(rf, "OUT", self.td.name), patch.object(rf, "DECODE_ENABLED", False)):
            p.start()
            self.addCleanup(p.stop)
        rf.nova_config.post_both.reset_mock()
        rf._spec_frames.clear()
        self.out = io.StringIO()
        r = redirect_stdout(self.out)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_cellular_decode_gated_off_by_default(self):
        self.assertIn('os.environ.get("RF_DECODE_ENABLE", "0") == "1"', SRC)
        c = MagicMock()
        with patch("subprocess.Popen") as popen:
            rf.capture_and_decode(c, 881.2)
        popen.assert_not_called()
        c.iq_stream.assert_not_called()
        self.assertIn("gated OFF", self.out.getvalue())


class TestPerformance(_Base):
    def test_detector_on_many_frames_fast(self):
        f = _frame(4096, peaks=[(1000, 200), (3000, 180)])
        t0 = time.perf_counter()
        for _ in range(200):
            peaks = rf._detect_carriers(f, 881e6, 8e6)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(peaks), 2)


class TestRetry(_Base):
    def test_capture_failure_is_non_fatal(self):
        # RETRY GAP: capture_and_decode — one attempt, any SDR error is swallowed so the watch keeps running
        c = MagicMock()
        c.set_frequency.side_effect = OSError("sdr gone")
        with patch.object(rf, "DECODE_ENABLED", True), patch("subprocess.Popen") as popen, \
                patch.dict(os.environ, {"HOME": self.td.name}):
            rf.capture_and_decode(c, 881.2)
        popen.assert_not_called()
        self.assertIn("non-fatal", self.out.getvalue())

    def test_alert_post_failure_swallowed(self):
        rf.nova_config.post_both.side_effect = RuntimeError("slack down")
        try:
            rf._alert_rogue(881.2, 200, "GSM850 DL")
        finally:
            rf.nova_config.post_both.side_effect = None
        self.assertIn("bb post err", self.out.getvalue())
        rf.nova_config.notify_local.assert_called()


class TestUnit(_Base):
    def test_choose_tuner(self):
        self.assertEqual(rf.choose_tuner(462.5e6), "Tuner 1 50 ohm")
        self.assertEqual(rf.choose_tuner(162.55e6), "Tuner 2 50 ohm")

    def test_detector_gates(self):
        self.assertTrue(rf._detect_carriers(_frame(peaks=[(256, 200)]), 881e6, 8e6))
        self.assertFalse(rf._detect_carriers(_frame(peaks=[(256, 55)]), 881e6, 8e6))          # weak
        wide = _frame(peaks=[(i, 200) for i in range(100, 200)])                                 # ~1.5 MHz LTE
        self.assertFalse(rf._detect_carriers(wide, 881e6, 8e6))
        self.assertEqual(rf._detect_carriers(b"\x00" * 10, 881e6, 8e6), [])
        (freq, lvl), = rf._detect_carriers(_frame(peaks=[(256, 200)]), 881e6, 8e6)
        self.assertEqual((freq, lvl), (881.0, 200))

    def test_snr_reading_and_num(self):
        self.assertIsNone(rf._num("x"))
        c = MagicMock(_cache={"signal_snr": "12.5"})
        self.assertEqual(rf.read_snr(c), 12.5)
        c = MagicMock(_cache={})
        c.get_property.side_effect = TimeoutError()
        self.assertIsNone(rf.read_snr(c))

    def test_flush_wav_threshold(self):
        rf._buf.clear()
        rf._buf.extend(b"\x00" * 100)
        self.assertIsNone(rf._flush_wav(162.55, "NFM", "NOAA WX"))
        rf._buf.extend(b"\x00" * 50_000)
        p = rf._flush_wav(162.55, "NFM", "NOAA/WX 2")
        self.assertTrue(p.endswith("__162.55__NFM__NOAA-WX_2.wav"))
        self.assertTrue(Path(p).exists())


class TestIntegration(_Base):
    def test_state_round_trip(self):
        self.assertEqual(rf._load_state(), {"passes": 0, "carriers": set()})
        rf._save_state({"passes": 2, "carriers": {"881.0"}})
        self.assertEqual(rf._load_state(), {"passes": 2, "carriers": {"881.0"}})


class TestFunctional(_Base):
    def _client(self, frame):
        # the SDR pushes spectrum frames asynchronously while the sweep sleeps: emulate that via sleep()
        rf.time.sleep.side_effect = lambda s: rf._spec_frames.append(frame)
        return MagicMock()

    def test_baseline_then_alert_only_on_new_carrier(self):
        st = {"passes": 0, "carriers": set()}
        legit = _frame(peaks=[(256, 200)])
        for _ in range(rf.BASELINE_PASSES):
            rf.cell_watch_pass(self._client(legit), st)
        rf.nova_config.post_both.assert_not_called()
        self.assertIn("baseline COMPLETE", self.out.getvalue())
        rf.cell_watch_pass(self._client(legit), st)
        rf.nova_config.post_both.assert_not_called()                     # known carrier
        rf.cell_watch_pass(self._client(_frame(peaks=[(256, 200), (100, 220)])), st)
        self.assertGreaterEqual(rf.nova_config.post_both.call_count, 1)
        self.assertIn("NEW (alerted)", self.out.getvalue())
        msg = rf.nova_config.post_both.call_args.args[0]
        self.assertIn("Possible IMSI-catcher", msg)
        self.assertEqual(rf.nova_config.post_both.call_args.kwargs["slack_channel"], "C_BB")


class TestFrame(unittest.TestCase):
    def test_import_with_stub_sdr_never_runs(self):
        # __main__ connects to the SDR and loops forever: smoke is an import in a child (pynrsp stubbed, HOME tmp)
        with tempfile.TemporaryDirectory() as home:
            code = ("import sys, types, importlib.util as u\n"
                    "p = types.ModuleType('pynrsp'); c = types.ModuleType('pynrsp.const')\n"
                    "p.Client = object; p.const = c\n"
                    "sys.modules.update({'pynrsp': p, 'pynrsp.const': c})\n"
                    f"s = u.spec_from_file_location('rf', {str(SCRIPT)!r}); m = u.module_from_spec(s)\n"
                    "s.loader.exec_module(m); print(len(m.BAND_PLAN), m.DECODE_ENABLED)\n")
            env = {k: v for k, v in os.environ.items() if k != "RF_DECODE_ENABLE"}
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                               env={**env, "HOME": home, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), f"{len(rf.BAND_PLAN)} False")
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
