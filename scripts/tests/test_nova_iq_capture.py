#!/usr/bin/env python3
"""Tests for nova_iq_capture.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The script is a top-level capture run with no functions and no __main__ guard, so every test drives it
end to end through runpy with a stub `pynrsp` SDR client (no radio, no SDRconnect socket)."""
import array
import io
import os
import re
import runpy
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_iq_capture.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_iq_test_"))


def _stub(samples, connect_exc=None, sr_exc=None):
    """A fake pynrsp whose Client records calls and delivers `samples` (int16 interleaved) once streaming starts."""
    calls = []

    class Client:
        def __init__(self, host): calls.append(("init", host)); self.on_iq = None; self._ws = types.SimpleNamespace(close=lambda: None)
        def connect(self, timeout): calls.append(("connect", timeout)); (_ for _ in ()).throw(connect_exc) if connect_exc else None
        def select_device(self, d): calls.append(("device", d))
        def set_property(self, k, v):
            calls.append(("prop", k, v))
            if sr_exc:
                raise sr_exc
        def set_frequency(self, f): calls.append(("freq", f))
        def device_stream_enable(self, on): calls.append(("stream", on))
        def iq_stream(self, on):
            calls.append(("iq", on))
            if on and samples:
                self.on_iq(array.array("h", samples))

    mod = types.ModuleType("pynrsp"); mod.Client = Client
    const = types.ModuleType("pynrsp.const"); mod.const = const
    return {"pynrsp": mod, "pynrsp.const": const}, calls


def _run(argv, samples=(16384, -16384, 0, 32767), **kw):
    stubs, calls = _stub(list(samples), **kw)
    out = io.StringIO()
    code = 0
    with patch.dict(sys.modules, stubs), patch.object(sys, "argv", [str(SCRIPT)] + argv), \
         patch("time.sleep"), redirect_stdout(out):
        try:
            runpy.run_path(str(SCRIPT), run_name="__main__")
        except SystemExit as e:
            code = e.code
    return code, calls, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("subprocess", SRC)

    def test_only_talks_to_loopback_sdrconnect(self):
        out = TMP / "a.cfile"
        _, calls, _ = _run(["935.2", "0", str(out)])
        self.assertEqual(calls[0], ("init", "127.0.0.1"))

    def test_stream_is_always_stopped(self):
        _, calls, _ = _run(["935.2", "0", str(TMP / "b.cfile")])
        self.assertEqual([c for c in calls if c[0] == "iq"], [("iq", True), ("iq", False)])
        self.assertIn(("stream", False), calls)


class TestPerformance(unittest.TestCase):
    def test_10k_samples_convert_fast(self):
        out = TMP / "perf.cfile"
        t0 = time.perf_counter()
        code, _, _ = _run(["935.2", "0", str(out)], samples=[i % 30000 for i in range(10_000)])
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(code, 0)
        self.assertEqual(out.stat().st_size, 10_000 * 4)


class TestRetry(unittest.TestCase):
    def test_sample_rate_rejection_is_tolerated(self):
        # RETRY GAP: set_property(device_sample_rate) — one attempt; a rejection warns and capture continues
        out = TMP / "sr.cfile"
        code, calls, txt = _run(["935.2", "0", str(out)], sr_exc=RuntimeError("busy"))
        self.assertEqual(code, 0)
        self.assertIn("sr warn: busy", txt)
        self.assertTrue(out.exists())

    def test_connect_failure_raises_before_tuning(self):
        # RETRY GAP: Client.connect — one attempt, no retry; nothing is tuned or written
        out = TMP / "never.cfile"
        with self.assertRaises(ConnectionError):
            _run(["935.2", "0", str(out)], connect_exc=ConnectionError("refused"))
        self.assertFalse(out.exists())


class TestUnit(unittest.TestCase):
    def test_int16_to_complex64_normalisation(self):
        out = TMP / "u.cfile"
        _run(["935.2", "0", str(out)], samples=(16384, -16384, 0, -32768))
        vals = struct.unpack("<4f", out.read_bytes())
        self.assertEqual(vals, (0.5, -0.5, 0.0, -1.0))

    def test_no_samples_exits_1(self):
        out = TMP / "empty.cfile"
        code, _, txt = _run(["935.2", "0", str(out)], samples=())
        self.assertEqual(code, 1)
        self.assertIn("NO IQ SAMPLES", txt)
        self.assertFalse(out.exists())

    def test_default_sample_rate(self):
        _, calls, _ = _run(["935.2", "0", str(TMP / "d.cfile")])
        self.assertIn(("prop", "device_sample_rate", "2000000"), calls)


class TestIntegration(unittest.TestCase):
    def test_tunes_device_2_at_requested_frequency_and_rate(self):
        _, calls, txt = _run(["1842.6", "0", str(TMP / "i.cfile"), "1000000"])
        self.assertIn(("device", 2), calls)
        self.assertIn(("freq", 1842600000), calls)
        self.assertIn(("prop", "device_sample_rate", "1000000"), calls)
        self.assertIn("1.0Msps", txt)


class TestFunctional(unittest.TestCase):
    def test_capture_writes_cfile_and_reports(self):
        out = TMP / "f.cfile"
        code, _, txt = _run(["935.2", "0", str(out)], samples=(1, 2, 3, 4, 5, 6))
        self.assertEqual(code, 0)
        self.assertEqual(out.stat().st_size, 6 * 4)
        self.assertIn(f"captured 3 IQ samples", txt)

    def test_missing_args_fail_before_touching_radio(self):
        with self.assertRaises(IndexError):
            _run(["935.2"])


class TestFrame(unittest.TestCase):
    def test_script_runs_end_to_end_with_stub_radio(self):
        out = TMP / "frame.cfile"
        code = ("import sys, types, array, runpy\n"
                "class C:\n"
                "    def __init__(s, h): s.on_iq = None; s._ws = types.SimpleNamespace(close=lambda: None)\n"
                "    def connect(s, timeout): pass\n"
                "    def select_device(s, d): pass\n"
                "    def set_property(s, k, v): pass\n"
                "    def set_frequency(s, f): pass\n"
                "    def device_stream_enable(s, on): pass\n"
                "    def iq_stream(s, on):\n"
                "        if on: s.on_iq(array.array('h', [1, 2]))\n"
                "m = types.ModuleType('pynrsp'); m.Client = C; m.const = types.ModuleType('pynrsp.const')\n"
                "sys.modules['pynrsp'] = m; sys.modules['pynrsp.const'] = m.const\n"
                "import time; time.sleep = lambda s: None\n"
                "sys.argv = [sys.argv[1], '935.2', '0', sys.argv[2]]\n"
                "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPT), str(out)], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("captured 1 IQ samples", r.stdout)

    def test_usage_documented(self):
        # no __main__ guard / no --help: the module IS the capture run, so the docstring is the usage contract
        self.assertIn("Usage: nova_iq_capture.py <freq_mhz> <seconds> <out.cfile>", SRC)


if __name__ == "__main__":
    unittest.main()
