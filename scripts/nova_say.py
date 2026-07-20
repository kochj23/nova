#!/usr/bin/env python3
"""nova_say.py — Nova's OWN synthetic voice (an XTTS built-in speaker, not a cloned person).
Speak arbitrary text as Nova: journal readouts, Big Brother alerts read aloud, etc.
Usage: nova_say.py "text to speak" [--voice "Ana Florence"] [--out nova.wav]"""
import os, argparse
os.environ.setdefault("COQUI_TOS_AGREED", "1")
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--voice", default=os.environ.get("NOVA_VOICE", "Ana Florence"))
    ap.add_argument("--out", default=os.path.expanduser("~/.openclaw/voice_out/nova.wav"))
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    from TTS.api import TTS
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2", progress_bar=False)
    tts.tts_to_file(text=a.text, speaker=a.voice, language="en", file_path=a.out)
    print(f"\n✓ Nova ({a.voice}) said: \"{a.text[:70]}\"\n  -> {a.out}")
if __name__ == "__main__":
    main()
