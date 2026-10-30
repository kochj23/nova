#!/usr/bin/env python3
"""nova_fm_whisper mlx fallback (nova-core4, 2026-10-30): segments the model marks as non-speech or low-confidence
are dropped, because large-v3-turbo invents captions on radio static."""
import sys
import types
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "nova_fm_whisper.py"


def load(segments):
    sys.modules["faster_whisper"] = None                     # force the mlx path
    fake = types.ModuleType("mlx_whisper")
    fake.transcribe = lambda p, **k: {"segments": segments}
    sys.modules["mlx_whisper"] = fake
    ns = {}
    exec(compile(SRC.read_text().split("print('fm whisper")[0], str(SRC), "exec"), ns)   # skip the watch loop
    return ns["m"]


class TestMlxFilter(unittest.TestCase):
    def test_static_hallucinations_dropped(self):
        m = load([{"text": "BUR tower, Southwest 12 cleared to land", "no_speech_prob": 0.1, "avg_logprob": -0.3},
                  {"text": "Transcribed by ESO, translated by", "no_speech_prob": 0.9, "avg_logprob": -0.4},
                  {"text": "Sur Caribbean medium Roman", "no_speech_prob": 0.2, "avg_logprob": -1.4}])
        segs, _ = m.transcribe("x.wav", language="en", vad_filter=True)
        self.assertEqual([s.text for s in segs], ["BUR tower, Southwest 12 cleared to land"])

    def test_no_filter_when_vad_off(self):
        m = load([{"text": "a", "no_speech_prob": 0.9, "avg_logprob": -2.0}])
        self.assertEqual(len(m.transcribe("x.wav", vad_filter=False)[0]), 1)


if __name__ == "__main__":
    unittest.main()
