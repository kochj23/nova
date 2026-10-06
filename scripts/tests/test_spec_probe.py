#!/usr/bin/env python3
"""Tests for spec_probe.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import contextlib
import importlib.util
import io
import os
import re
import runpy
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
SCRIPT = SCRIPTS / "spec_probe.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="spec-probe-test-"))


@contextlib.contextmanager
def _modules(**mods):
    """Set sys.modules keys for the block and restore ONLY those keys afterwards."""
    missing = object()
    saved = {k: sys.modules.get(k, missing) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is missing:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _frame(n=1024, floor=20, peaks=()):
    bins = [floor] * n
    for i, level in peaks:
        bins[i] = level
    return bytes(bins)


def _pynrsp(frames=(), connect_exc=None, prop_exc=None):
    """A pynrsp stand-in: records the SDRconnect call sequence, delivers frames on spectrum(True)."""
    calls = []

    class Client:
        def __init__(self, host):
            calls.append(("init", host)); self.on_spectrum = None; self._running = True
            self._ws = types.SimpleNamespace(close=lambda: calls.append(("ws_close",)))

        def connect(self, timeout=None):
            calls.append(("connect", timeout))
            if connect_exc:
                raise connect_exc

        def select_device(self, dev):
            calls.append(("select_device", dev))

        def set_property(self, k, v):
            calls.append(("set_property", k, v))
            if prop_exc:
                raise prop_exc

        def set_frequency(self, f):
            calls.append(("set_frequency", f))

        def device_stream_enable(self, on):
            calls.append(("stream", on))

        def spectrum(self, on):
            calls.append(("spectrum", on))
            if on:
                for fr in frames:
                    self.on_spectrum(fr)

    mod = types.ModuleType("pynrsp")
    mod.Client, mod.const, mod.calls = Client, types.SimpleNamespace(), calls
    return mod


def _run(argv, frames=(), **kw):
    """Run the probe top-to-bottom with a stubbed radio. Returns (exit code, stdout, calls, globals)."""
    mod = _pynrsp(frames, **kw)
    out, rc, g = io.StringIO(), 0, {}
    with _modules(pynrsp=mod), patch.object(sys, "argv", ["spec_probe.py", *argv]), \
         patch.object(sys, "path", list(sys.path)), patch("time.sleep"), redirect_stdout(out):
        try:
            g = runpy.run_path(str(SCRIPT), run_name="spec_probe_under_test")
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), mod.calls, g


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_only_talks_to_loopback_sdrconnect(self):
        hosts = re.findall(r'Client\("([^"]+)"\)', SRC)
        self.assertEqual(hosts, ["127.0.0.1"])
        self.assertNotIn("subprocess", SRC); self.assertNotIn("os.system", SRC)

    def test_junk_frequency_argument_is_rejected_before_any_radio_contact(self):
        mod = _pynrsp()
        with _modules(pynrsp=mod), patch.object(sys, "argv", ["spec_probe.py", "100; rm -rf /"]), \
             patch.object(sys, "path", list(sys.path)), patch("time.sleep"), redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                runpy.run_path(str(SCRIPT), run_name="spec_probe_under_test")
        self.assertEqual(mod.calls, [])                       # float() rejects it before Client() exists


class TestPerformance(unittest.TestCase):
    def test_peak_detection_fast_on_64k_bins(self):
        fr = _frame(65536, 20, [(i, 200) for i in range(0, 65536, 997)])
        t0 = time.perf_counter()
        rc, out, _, g = _run(["100"], frames=[fr])
        self.assertLess(time.perf_counter() - t0, 1.5)
        self.assertEqual(rc, 0)
        self.assertEqual(len(g["peaks"]), len(range(0, 65536, 997)))


class TestRetry(unittest.TestCase):
    def test_no_frames_in_capture_window_fails_closed_with_exit_1(self):
        # RETRY GAP: spectrum capture — one 3 s window, no second attempt; exits 1 with a clear message
        rc, out, calls, _ = _run(["100"], frames=[])
        self.assertEqual(rc, 1)
        self.assertIn("NO SPECTRUM FRAMES", out)
        self.assertIn(("spectrum", False), calls)             # the stream is still torn down first

    def test_connect_failure_escapes_without_touching_the_device(self):
        # RETRY GAP: Client.connect — single attempt; the probe is a hand-run tool, so the error surfaces raw
        mod = _pynrsp(connect_exc=OSError("SDRconnect down"))
        with _modules(pynrsp=mod), patch.object(sys, "argv", ["spec_probe.py", "100"]), \
             patch.object(sys, "path", list(sys.path)), patch("time.sleep"), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                runpy.run_path(str(SCRIPT), run_name="spec_probe_under_test")
        self.assertNotIn(("select_device", 2), mod.calls)

    def test_sample_rate_rejection_is_swallowed_and_the_probe_continues(self):
        rc, out, calls, g = _run(["100"], frames=[_frame(peaks=[(512, 200)])], prop_exc=RuntimeError("unsupported"))
        self.assertEqual(rc, 0)
        self.assertIn("sr set warn: unsupported", out)
        self.assertEqual(len(g["peaks"]), 1)


class TestUnit(unittest.TestCase):
    def test_flat_noise_floor_yields_no_carriers(self):
        rc, out, _, g = _run(["100"], frames=[_frame()])
        self.assertEqual(rc, 0)
        self.assertEqual(g["peaks"], [])
        self.assertEqual(g["thr"], 20 + 15)                   # MAD of a flat frame is 0 -> floor + 15

    def test_single_carrier_lands_on_the_centre_frequency(self):
        rc, out, _, g = _run(["100"], frames=[_frame(peaks=[(512, 200)])])
        self.assertEqual(g["peaks"], [(100.0, 200)])          # 100 MHz - 4 MHz + 512 * 7812.5 Hz
        self.assertEqual(g["bin_bw"], 8e6 / 1024)

    def test_adjacent_hot_bins_collapse_to_their_strongest_bin(self):
        rc, out, _, g = _run(["100"], frames=[_frame(peaks=[(100, 90), (101, 220), (102, 95), (700, 180)])])
        self.assertEqual([lvl for _, lvl in g["peaks"]], [220, 180])
        self.assertAlmostEqual(g["peaks"][0][0], round((100e6 - 4e6 + 101 * 7812.5) / 1e6, 3))

    def test_last_frame_wins_and_output_is_capped_at_twenty_lines(self):
        quiet, busy = _frame(), _frame(peaks=[(i, 200) for i in range(0, 1000, 20)])
        rc, out, _, g = _run(["100"], frames=[quiet, busy])
        self.assertEqual(len(g["peaks"]), 50)
        self.assertEqual(sum(1 for ln in out.splitlines() if ln.strip().endswith("/255")), 20)


class TestIntegration(unittest.TestCase):
    def test_radio_call_sequence_and_defaults(self):
        rc, out, calls, g = _run(["100"], frames=[_frame()])
        names = [c[0] for c in calls]
        self.assertEqual(names[:7], ["init", "connect", "select_device", "set_property", "set_frequency", "stream", "spectrum"])
        self.assertIn(("select_device", 2), calls)
        self.assertIn(("set_property", "device_sample_rate", "8000000"), calls)   # SR defaults to 8 MHz
        self.assertIn(("set_frequency", 100_000_000), calls)
        self.assertEqual(calls[-3:], [("spectrum", False), ("stream", False), ("ws_close",)])

    def test_explicit_span_argument_reaches_the_device_and_the_bin_width(self):
        rc, out, calls, g = _run(["433.92", "2"], frames=[_frame()])
        self.assertIn(("set_property", "device_sample_rate", "2000000"), calls)
        self.assertEqual(g["SR"], 2e6)
        self.assertIn("center=433.92 span=2.0MHz bins=1024", out)


class TestFunctional(unittest.TestCase):
    def test_golden_path_prints_summary_and_carrier_table(self):
        rc, out, _, _ = _run(["100"], frames=[_frame(peaks=[(512, 200)])])
        self.assertEqual(rc, 0)
        self.assertIn("center=100.0 span=8.0MHz bins=1024 floor=20.0 thr=35.0", out)
        self.assertIn("carriers detected (1):", out)
        self.assertIn("  100.0 MHz  level=200/255", out)

    def test_error_path_reports_empty_capture(self):
        rc, out, _, _ = _run(["100"], frames=[])
        self.assertEqual((rc, out.strip()), (1, "NO SPECTRUM FRAMES"))


class TestFrame(unittest.TestCase):
    def test_probe_runs_end_to_end_as_a_subprocess_with_a_stub_radio(self):
        pkg = TMP / "pp" / "pynrsp"; pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "__init__.py").write_text(
            "import time as _t; _t.sleep = lambda s: None\n"
            "const = None\n"
            "class Client:\n"
            "    def __init__(self, host): self.on_spectrum = None; self._running = True; self._ws = type('W', (), {'close': lambda self: None})()\n"
            "    def connect(self, timeout=None): pass\n"
            "    def select_device(self, d): pass\n"
            "    def set_property(self, k, v): pass\n"
            "    def set_frequency(self, f): pass\n"
            "    def device_stream_enable(self, on): pass\n"
            "    def spectrum(self, on):\n"
            "        if on: self.on_spectrum(bytes([20] * 1024 + [200] + [20] * 1023))\n")
        env = {**os.environ, "NOVA_TEST_QUIET": "1", "PYTHONPATH": str(TMP / "pp")}
        r = subprocess.run([sys.executable, str(SCRIPT), "100"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("carriers detected (1):", r.stdout)
        self.assertIn("100.0 MHz  level=200/255", r.stdout)

    def test_script_is_a_top_level_probe_that_fails_fast_without_its_radio_library(self):
        self.assertNotIn("__main__", SRC)                      # by design: a hand-run probe, no library surface
        if importlib.util.find_spec("pynrsp") is None:
            r = subprocess.run([sys.executable, str(SCRIPT), "100"], capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1"})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("pynrsp", r.stderr)


if __name__ == "__main__":
    unittest.main()
