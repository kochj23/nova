#!/usr/bin/env python3
"""nova_minion_whisper.py — runs ON a minion: transcribe one 16 kHz mono WAV with MLX Whisper, print the text.

The Studio's media re-transcription (nova_media_reingest.py --minions) extracts the audio, copies the WAV here,
runs this in ~/.nova-minion/whisper-venv, reads stdout, and deletes the WAV. The WAV is read with the stdlib
`wave` module so the minion needs no ffmpeg. Same model and settings as the Studio's own mlx_whisper CLI.
  nova_minion_whisper.py AUDIO.wav [--model mlx-community/whisper-large-v3-turbo]   |   --selftest
Written by Jordan Koch (via Claude).
"""
import sys
import wave

MODEL = "mlx-community/whisper-large-v3-turbo"


def load_wav(path: str):
    import numpy as np
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getframerate() != 16000 or w.getsampwidth() != 2:
            raise SystemExit(f"need 16 kHz mono s16 WAV, got {w.getnchannels()}ch {w.getframerate()} Hz "
                             f"{8 * w.getsampwidth()}-bit")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def main() -> int:
    if "--selftest" in sys.argv:
        import mlx_whisper  # noqa: F401 — the venv is usable
        import numpy  # noqa: F401
        print("selftest OK")
        return 0
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else MODEL
    import mlx_whisper
    out = mlx_whisper.transcribe(load_wav(args[0]), path_or_hf_repo=model, language="en",
                                 condition_on_previous_text=False, verbose=None)
    sys.stdout.write((out.get("text") or "").strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
