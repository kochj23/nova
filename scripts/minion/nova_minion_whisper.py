#!/usr/bin/env python3
"""nova_minion_whisper.py — runs ON a minion: transcribe one 16 kHz mono clip, print the text.

The Studio's media re-transcription (nova_media_reingest.py --minions) has the media host (nova-core) extract the
audio with ffmpeg as raw 16 kHz mono s16le PCM, streams it here, runs this in ~/.nova-minion/whisper-venv, reads
stdout, and deletes the clip. Input: a .wav (16 kHz mono s16, read with the stdlib `wave` module) or a .pcm (raw
s16le 16 kHz mono) — the minion needs no ffmpeg.
Backends (2026-10-18; picked by what the venv has):
  mlx      Apple Silicon: mlx_whisper, mlx-community/whisper-large-v3-turbo (same model + settings as the Studio)
  faster   Linux/CPU: faster-whisper (CTranslate2, int8), the same large-v3-turbo weights in CT2 form
  nova_minion_whisper.py AUDIO.{wav,pcm} [--model M] [--threads N] [--json] [--cpu]   |   --selftest   |   --backend
--cpu forces faster-whisper (CPU) even where MLX exists, so a Mac can run CPU workers beside its GPU ones.
--json prints {"text", "audio_s", "backend", "model", "s"} instead of bare text.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import json
import os
import sys
import time
import wave

MODEL = "mlx-community/whisper-large-v3-turbo"
FASTER_MODEL = "deepdml/faster-whisper-large-v3-turbo-ct2"
RATE = 16000
_HF = os.path.expanduser("~/.nova-minion/hf")   # a Mac with /Volumes/Data keeps weights there (symlinked), not on the SSD
if os.path.isdir(_HF):
    os.environ.setdefault("HF_HOME", _HF)


def load_audio(path: str):
    """float32 mono samples in [-1, 1) from a 16 kHz mono s16 .wav or a raw s16le .pcm."""
    import numpy as np
    if path.lower().endswith(".pcm"):
        with open(path, "rb") as f:
            raw = f.read()
        raw = raw[: len(raw) // 2 * 2]
    else:
        with wave.open(path, "rb") as w:
            if w.getnchannels() != 1 or w.getframerate() != RATE or w.getsampwidth() != 2:
                raise SystemExit(f"need 16 kHz mono s16 WAV, got {w.getnchannels()}ch {w.getframerate()} Hz "
                                 f"{8 * w.getsampwidth()}-bit")
            raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def backend() -> str:
    if "--cpu" in sys.argv:                   # 2026-10-18: a Mac's extra CPU workers beside its MLX (GPU) ones
        import faster_whisper  # noqa: F401
        return "faster"
    try:
        import mlx_whisper  # noqa: F401
        return "mlx"
    except Exception:  # noqa: BLE001
        pass
    import faster_whisper  # noqa: F401
    return "faster"


def arg(name: str, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def transcribe(audio, be: str, model: str | None, threads: int) -> tuple[str, str]:
    if be == "mlx":
        import mlx_whisper
        m = model or MODEL
        out = mlx_whisper.transcribe(audio, path_or_hf_repo=m, language="en",
                                     condition_on_previous_text=False, verbose=None)
        return (out.get("text") or "").strip(), m
    from faster_whisper import WhisperModel
    m = model or FASTER_MODEL
    wm = WhisperModel(m, device="cpu", compute_type="int8", cpu_threads=threads or (os.cpu_count() or 4))
    segs, _ = wm.transcribe(audio, language="en", condition_on_previous_text=False, beam_size=1, vad_filter=True)
    return " ".join(s.text.strip() for s in segs).strip(), m


def main() -> int:
    if "--selftest" in sys.argv:
        import numpy  # noqa: F401
        print(f"selftest OK ({backend()})")
        return 0
    if "--backend" in sys.argv:
        print(backend())
        return 0
    skip = {"--model", "--threads"}
    args = [a for i, a in enumerate(sys.argv[1:], 1) if not a.startswith("--") and sys.argv[i - 1] not in skip]
    model = arg("--model")
    threads = int(arg("--threads", "0") or 0)
    be = backend()
    if model and be == "faster" and model.startswith("mlx-community/"):
        model = None                          # the Studio passes the MLX id; Linux uses the CT2 copy of it
    audio = load_audio(args[0])
    t0 = time.monotonic()
    text, used = transcribe(audio, be, model, threads)
    if "--json" in sys.argv:
        sys.stdout.write(json.dumps({"text": text, "audio_s": round(len(audio) / RATE, 1), "backend": be,
                                     "model": used, "s": round(time.monotonic() - t0, 1)}))
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
