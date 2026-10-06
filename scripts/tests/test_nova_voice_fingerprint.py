#!/usr/bin/env python3
"""Tests for nova_voice_fingerprint.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Diarization math runs for real on synthetic embeddings; ffmpeg and the resemblyzer encoder are mocked."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_voice_fingerprint.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_voice_fingerprint_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vf = _load()


def _voices(counts, dim=256, noise=0.02, seed=0):
    """Synthetic d-vectors: one random unit direction per speaker, `counts[i]` noisy windows each."""
    rng = np.random.default_rng(seed)
    centers = [vf._norm(rng.normal(size=dim)) for _ in counts]
    parts = np.vstack([vf._norm(c + rng.normal(scale=noise, size=dim)) for c, n in zip(centers, counts) for _ in range(n)])
    splits = [slice(int(i * 1.6 * 16000), int((i + 1) * 1.6 * 16000)) for i in range(len(parts))]
    return parts, splits


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_ffmpeg_argv_list_and_hostile_filename(self):
        with mock.patch.object(vf.subprocess, "run") as run:
            wav = vf.audio_wav("clip; echo pwned.mp4", 30)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["ffmpeg", "-t", "30"])
        self.assertEqual(argv[argv.index("-i") + 1], "clip; echo pwned.mp4")
        self.assertNotIn("shell", run.call_args[1])
        os.unlink(wav)


class TestPerformance(unittest.TestCase):
    def test_diarize_200_windows_fast(self):
        parts, splits = _voices([120, 80])
        t0 = time.perf_counter()
        labels = vf.diarize(parts, splits)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(len(set(labels)), 2)


class TestRetry(unittest.TestCase):
    def test_ffmpeg_failure_is_not_retried(self):
        # RETRY GAP: audio_wav — one ffmpeg run, return code ignored; a bad video yields an empty wav path
        with mock.patch.object(vf.subprocess, "run", return_value=mock.Mock(returncode=1)) as run:
            wav = vf.audio_wav("missing.mp4", 5)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(wav.endswith(".wav"))
        os.unlink(wav)


class TestUnit(unittest.TestCase):
    def test_diarize_edges_and_talk_time_order(self):
        self.assertEqual(list(vf.diarize(np.zeros((1, 4)), [slice(0, 1)])), [0])
        self.assertEqual(len(vf.diarize(np.zeros((0, 4)), [])), 0)
        parts, splits = _voices([5, 20])
        labels = vf.diarize(parts, splits)
        self.assertEqual(int((labels == 0).sum()), 20)        # Speaker 0 = most talkative

    def test_turns_and_hms(self):
        labels = np.array([0, 0, 1, 1, 0])
        splits = [slice(i * 16000, (i + 1) * 16000) for i in range(5)]
        self.assertEqual(vf.turns_from_labels(labels, splits), [(0.0, 2.0, 0), (2.0, 4.0, 1), (4.0, 5.0, 0)])
        self.assertEqual(vf.turns_from_labels([], []), [])
        self.assertEqual(vf.hms(125.9), "02:05")

    def test_norm(self):
        self.assertAlmostEqual(float(np.linalg.norm(vf._norm(np.array([3.0, 4.0])))), 1.0)
        self.assertTrue(np.all(vf._norm(np.zeros(3)) == 0))


class TestIntegration(unittest.TestCase):
    def test_windows_chain_into_top_speakers(self):
        parts, splits = _voices([30, 10])
        enc = mock.Mock(); enc.embed_utterance.return_value = (None, parts, splits)
        with mock.patch.object(vf, "audio_wav", return_value="/tmp/x.wav"), \
                mock.patch.object(vf, "preprocess_wav", return_value=np.zeros(10)), \
                mock.patch.object(vf, "encoder", return_value=enc):
            p, s = vf.windows("v.mp4", 60)
        tops = vf.top_speakers(p, vf.diarize(p, s), s)
        self.assertEqual([(t[0], t[1]) for t in tops], [(0, 48), (1, 16)])
        self.assertAlmostEqual(float(np.linalg.norm(tops[0][2])), 1.0, places=5)
        self.assertEqual(enc.embed_utterance.call_args[1], {"return_partials": True, "rate": 1.3})


class TestFunctional(unittest.TestCase):
    def test_main_cross_video_match(self):
        a, sa = _voices([30, 10], seed=1)
        b, sb = _voices([30, 10], seed=1, noise=0.03)          # same two voices again
        with mock.patch.object(vf, "windows", side_effect=[(a, sa), (b, sb)]), \
                mock.patch.object(sys, "argv", ["x", "v1.mp4", "--match", "v2.mp4", "--secs", "60"]), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            vf.main()
        o = out.getvalue()
        self.assertIn("40 windows -> 2 speakers", o)
        self.assertEqual(o.count("[SAME PERSON]"), 2)

    def test_main_single_video_lists_turns(self):
        a, sa = _voices([10, 5], seed=2)
        with mock.patch.object(vf, "windows", return_value=(a, sa)), \
                mock.patch.object(sys, "argv", ["x", "v1.mp4"]), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            vf.main()
        self.assertIn("Speaker 0: ~16s of speech", out.getvalue())
        self.assertNotIn("Cross-video", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(PATH), "--help"], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--match", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIsNone(vf._ENC)                              # encoder model is lazy, not loaded on import
        r = subprocess.run([sys.executable, "-c", "import nova_voice_fingerprint"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=60, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
