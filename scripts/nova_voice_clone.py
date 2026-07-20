#!/usr/bin/env python3
"""nova_voice_clone.py — clone a voice from a reference clip and speak arbitrary text (XTTS-v2).

PERSONAL / NON-COMMERCIAL use. Clones the timbre of a reference wav (e.g. a nostalgic ride
announcer) and reads new text in that voice. First run downloads the XTTS-v2 model (~1.8 GB).

Usage: nova_voice_clone.py --ref <reference.wav> --text "what to say" [--out out.wav]
"""
import os, sys, argparse
os.environ.setdefault("COQUI_TOS_AGREED", "1")   # accept the XTTS model license (non-commercial)

DEFAULT_REF = os.path.expanduser("~/.openclaw/voice_refs/btmrr_spiel.wav")
DEFAULT_TEXT = ("Howdy, partners! This here's the wildest ride in the wilderness. "
                "Keep your hands, arms, and pull requests inside the train at all times, "
                "and hang on to them hats and glasses!")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=DEFAULT_REF)
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("--text-file", default=None, help="read the text to speak from a file")
    ap.add_argument("--out", default=os.path.expanduser("~/.openclaw/voice_out/clone.wav"))
    a = ap.parse_args()
    if a.text_file:
        a.text = open(a.text_file).read().strip()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)

    from TTS.api import TTS
    print("Loading XTTS-v2 (first run downloads ~1.8GB)...", flush=True)
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2", progress_bar=False)
    print(f"Cloning voice from {os.path.basename(a.ref)} and speaking...", flush=True)
    tts.tts_to_file(text=a.text, speaker_wav=a.ref, language="en", file_path=a.out)
    print(f"\n✓ wrote {a.out}")
    print(f'  said: "{a.text[:80]}..."')


if __name__ == "__main__":
    main()
