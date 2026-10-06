#!/usr/bin/env python3
"""Tests for nova_rspduo2_scan.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
The SDR (pynrsp Client) is a stub injected via patch.dict(sys.modules) only for the import; HOME points at a
tempdir so WAV chunks land there; time is faked so the watchdog loop ends instantly. No radio is touched."""
import array
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_rspduo2_scan.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="rspduo2-test-"))


class _FakeClient:
    instances = []

    def __init__(self, host):
        self.host = host; self.calls = []; self.can_control = "true"; self.on_audio = None
        self._ws = MagicMock(); _FakeClient.instances.append(self)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        def rec(*a, **k):
            self.calls.append((name, a))
            return self.can_control if name == "get_property" else None
        return rec


def _stub_pynrsp():
    pkg = types.ModuleType("pynrsp"); pkg.Client = _FakeClient
    const = types.ModuleType("pynrsp.const"); const.PROP_CAN_CONTROL = "can_control"
    pkg.const = const
    return {"pynrsp": pkg, "pynrsp.const": const}


def _load(env=None):
    spec = importlib.util.spec_from_file_location("rspduo2_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_pynrsp()), patch.dict(os.environ, {"HOME": str(TMP), **(env or {})}):
        spec.loader.exec_module(mod)
    return mod


rd = _load()
assert rd.OUT.startswith(str(TMP))


class _Clock:
    def __init__(self, step):
        self.t = 1000.0; self.step = step
    def time(self):
        self.t += self.step; return self.t
    def sleep(self, s):
        pass


def _pcm(n):
    return array.array("h", range(n * 2))     # interleaved stereo int16


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_controls_only_local_sdrconnect_and_refuses_without_control(self):
        self.assertIn('Client("127.0.0.1")', SRC)
        class _Denied(_FakeClient):
            def __init__(self, host):
                super().__init__(host); self.can_control = "false"
        _FakeClient.instances.clear()
        with patch.object(rd, "Client", _Denied), patch.object(rd, "time", _Clock(1)):
            self.assertEqual(rd.run(), 1)
        c = _FakeClient.instances[-1]
        names = [n for n, _ in c.calls]
        self.assertNotIn("set_frequency", names)               # never retunes a device it doesn't own
        self.assertNotIn("device_stream_enable", names)


class TestPerformance(unittest.TestCase):
    def test_on_audio_10k_callbacks(self):
        rd._buf.clear(); rd._t0[0] = time.time()
        pcm = _pcm(480)
        t0 = time.perf_counter()
        for _ in range(10_000):
            rd.on_audio(pcm)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(rd._buf), 10_000 * 480 * 2)        # left channel only, int16
        rd._buf.clear()


class TestRetry(unittest.TestCase):
    def test_watchdog_exits_for_systemd_restart(self):
        # RETRY GAP: run() — no in-process reconnect; on audio starvation it returns 1 and systemd restarts it
        _FakeClient.instances.clear()
        with patch.object(rd, "Client", _FakeClient), patch.object(rd, "time", _Clock(rd.WATCHDOG_SEC)):
            self.assertEqual(rd.run(), 1)
        names = [n for n, _ in _FakeClient.instances[-1].calls]
        self.assertEqual(names[-3:], ["audio_stream", "device_stream_enable", "close"])   # clean teardown

    def test_wav_write_error_is_swallowed(self):
        rd._buf.clear(); rd._t0[0] = 0
        with patch.object(rd, "_write", side_effect=OSError("disk full")), patch("builtins.print") as pr:
            rd.on_audio(_pcm(10))
        self.assertIn("wav write err:", pr.call_args[0][0])


class TestUnit(unittest.TestCase):
    def test_choose_tuner(self):
        self.assertEqual(rd.choose_tuner(506.9e6), "Tuner 1 50 ohm")
        self.assertEqual(rd.choose_tuner(400e6), "Tuner 1 50 ohm")
        self.assertEqual(rd.choose_tuner(106.7e6), "Tuner 2 50 ohm")

    def test_env_overrides(self):
        m = _load({"NRSP_FREQ": "162.550", "NRSP_DEV_INDEX": "1", "NRSP_ANTENNA": ""})
        self.assertEqual((m.FREQ, m.DEV, m.ANT), (162_550_000, 1, "Tuner 2 50 ohm"))
        m = _load({"NRSP_ANTENNA": "Tuner 1 Hi-Z"})
        self.assertEqual(m.ANT, "Tuner 1 Hi-Z")


class TestIntegration(unittest.TestCase):
    def test_chunk_written_as_mono_wav(self):
        rd._buf.clear(); rd._t0[0] = 0
        before = set(os.listdir(rd.OUT))
        rd.on_audio(_pcm(100))
        new = set(os.listdir(rd.OUT)) - before
        self.assertEqual(len(new), 1)
        with wave.open(os.path.join(rd.OUT, new.pop())) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()), (1, 2, 48000, 100))


class TestFunctional(unittest.TestCase):
    def test_run_configures_device_in_order(self):
        _FakeClient.instances.clear()
        with patch.object(rd, "Client", _FakeClient), patch.object(rd, "time", _Clock(rd.WATCHDOG_SEC)):
            rd.run()
        c = _FakeClient.instances[-1]
        names = [n for n, _ in c.calls]
        self.assertEqual(names[:7], ["connect", "select_device", "get_property", "set_antenna",
                                     "set_demodulator", "set_frequency", "device_stream_enable"])
        self.assertEqual(dict(c.calls)["set_frequency"], (rd.FREQ,))
        self.assertEqual(dict(c.calls)["select_device"], (rd.DEV,))
        self.assertIs(c.on_audio, rd.on_audio)


class TestFrame(unittest.TestCase):
    def test_import_with_stub_sdr_never_runs(self):
        # no --help/--selftest: __main__ opens the SDR forever; import (with a stub pynrsp) is the smoke test
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys, types; p=types.ModuleType('pynrsp'); c=types.ModuleType('pynrsp.const');"
                "p.Client=object; p.const=c; sys.modules.update({'pynrsp': p, 'pynrsp.const': c});"
                "import runpy; m=runpy.run_path(sys.argv[1], run_name='rspduo2_smoke');"
                "print(m['choose_tuner'](506.9e6))")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "Tuner 1 50 ohm")


if __name__ == "__main__":
    unittest.main()
