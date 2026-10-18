#!/usr/bin/env python3
"""Tests for minion/nova_minion_whisper.py (2026-10-18: .pcm input, faster-whisper backend for Linux, --json) and the
minion agent 1.5 change (a Whisper minion without Ollama still lends itself). The 7 house categories (Security,
Performance, Retry, Unit, Integration, Functional, Frame); no model is loaded — the backends are mocked.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import sys
import tempfile
import time
import types
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1] / "minion"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mw = _load("minion_whisper_under_test", "nova_minion_whisper.py")
ma = _load("minion_agent_under_test", "nova_minion_agent.py")
SRC = (HERE / "nova_minion_whisper.py").read_text()

try:
    import numpy  # noqa: F401
    HAVE_NP = True
except ImportError:
    HAVE_NP = False


def _pcm(d, seconds=2):
    p = Path(d) / "a.pcm"
    p.write_bytes(b"\x00\x40" * 16000 * seconds)
    return p


def _wav(d, seconds=2, rate=16000):
    p = Path(d) / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x40" * rate * seconds)
    return p


class TestSecurity(unittest.TestCase):
    def test_no_network_or_shell_in_the_worker(self):
        for bad in ("subprocess", "os.system", "urllib", "requests", "token"):
            self.assertNotIn(bad, SRC)

    @unittest.skipUnless(HAVE_NP, "numpy")
    def test_rejects_wrong_wav_format(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(SystemExit):
            mw.load_audio(str(_wav(d, rate=44100)))


class TestPerformance(unittest.TestCase):
    @unittest.skipUnless(HAVE_NP, "numpy")
    def test_hour_of_pcm_loads_fast(self):
        with tempfile.TemporaryDirectory() as d:
            p = _pcm(d, 3600)
            t0 = time.monotonic()
            a = mw.load_audio(str(p))
            self.assertLess(time.monotonic() - t0, 5)
            self.assertEqual(len(a), 3600 * 16000)


class TestRetry(unittest.TestCase):
    def test_backend_falls_back_to_faster_whisper(self):
        fake = types.ModuleType("faster_whisper")
        with mock.patch.dict(sys.modules, {"mlx_whisper": None, "faster_whisper": fake}):
            self.assertEqual(mw.backend(), "faster")


class TestUnit(unittest.TestCase):
    @unittest.skipUnless(HAVE_NP, "numpy")
    def test_pcm_and_wav_decode_the_same(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = mw.load_audio(str(_pcm(d))), mw.load_audio(str(_wav(d)))
            self.assertEqual(len(a), len(b))
            self.assertAlmostEqual(float(a[0]), 0.5)

    @unittest.skipUnless(HAVE_NP, "numpy")
    def test_odd_byte_pcm_is_trimmed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.pcm"
            p.write_bytes(b"\x00\x40\x01")
            self.assertEqual(len(mw.load_audio(str(p))), 1)

    def test_agent_info_carries_worker_counts(self):
        src = (HERE / "nova_minion_agent.py").read_text()
        self.assertIn('"whisper_workers"', src)
        self.assertEqual(ma.VERSION, "1.5")


class TestIntegration(unittest.TestCase):
    def _tick(self, conf, models):
        beats = []
        ma._state.update(state="offline", since=None)
        with mock.patch.object(ma, "loaded_here", return_value=[]), mock.patch.object(ma, "models_here", return_value=models), \
                mock.patch.object(ma, "free_gb", return_value=20.0), mock.patch.object(ma, "linux_busy", return_value=False), \
                mock.patch.object(ma, "LINUX", True), mock.patch.object(ma, "os_label", return_value="Ubuntu"), \
                mock.patch.object(ma, "http", side_effect=lambda url, body=None, **k: beats.append(body) or {}), \
                mock.patch.object(ma.threading, "Thread"), redirect_stdout(io.StringIO()):
            st = ma.tick(conf, "tok")
        return st, beats[-1]

    def test_whisper_minion_without_ollama_is_idle(self):
        st, hb = self._tick({"node": "nova-core", "hub": "http://h:1", "ollama_url": "", "idle_after_min": 10,
                             "models": [], "whisper": True, "whisper_workers": 2, "whisper_threads": 6}, [])
        self.assertEqual(st, "idle")
        self.assertEqual((hb["info"]["whisper"], hb["info"]["whisper_workers"], hb["info"]["whisper_threads"]), (True, 2, 6))

    def test_plain_minion_without_ollama_stays_away(self):
        st, _ = self._tick({"node": "n", "hub": "http://h:1", "ollama_url": "http://x:11434", "idle_after_min": 10,
                            "models": []}, [])
        self.assertEqual(st, "away")


class TestFunctional(unittest.TestCase):
    @unittest.skipUnless(HAVE_NP, "numpy")
    def test_json_output_with_faster_backend_and_mlx_model_id_dropped(self):
        seen = {}

        def fake_tx(audio, be, model, threads):
            seen.update(be=be, model=model, threads=threads)
            return "hello there", model or mw.FASTER_MODEL
        with tempfile.TemporaryDirectory() as d:
            argv = ["x", str(_pcm(d, 3)), "--json", "--model", mw.MODEL, "--threads", "6"]
            out = io.StringIO()
            with mock.patch.object(sys, "argv", argv), mock.patch.object(mw, "backend", return_value="faster"), \
                    mock.patch.object(mw, "transcribe", side_effect=fake_tx), redirect_stdout(out):
                self.assertEqual(mw.main(), 0)
        d = json.loads(out.getvalue())
        self.assertEqual((d["text"], d["audio_s"], d["backend"]), ("hello there", 3.0, "faster"))
        self.assertEqual((seen["model"], seen["threads"]), (None, 6))     # MLX id is not a CT2 model


class TestFrame(unittest.TestCase):
    def test_same_model_family_on_both_backends(self):
        self.assertEqual(mw.MODEL, "mlx-community/whisper-large-v3-turbo")
        self.assertIn("large-v3-turbo", mw.FASTER_MODEL)
        self.assertIn("from __future__ import annotations", SRC)      # macOS system python 3.9 venvs

    def test_installer_exists_and_is_idempotent(self):
        sh = (HERE / "install_whisper_linux.sh").read_text()
        self.assertIn("[ -x \"$DIR/whisper-venv/bin/python\" ] ||", sh)
        self.assertIn("faster-whisper", sh)


if __name__ == "__main__":
    unittest.main()
