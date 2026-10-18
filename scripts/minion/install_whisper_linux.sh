#!/bin/bash
# install_whisper_linux.sh — give THIS Linux minion a Whisper worker (2026-10-18): ~/.nova-minion/whisper-venv with
# faster-whisper (CTranslate2 int8, CPU) + nova_minion_whisper.py, and the large-v3-turbo CT2 weights pre-fetched.
# Run from ~/.nova-minion/install (enroll_minion.sh copies it there) or with the script next to it.
#   install_whisper_linux.sh            (idempotent; prints the backend selftest)
# Written by Jordan Koch (via Claude).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/.nova-minion"
mkdir -p "$DIR"
cp "$HERE/nova_minion_whisper.py" "$DIR/"
PY="$(command -v python3)"
[ -x "$DIR/whisper-venv/bin/python" ] || "$PY" -m venv "$DIR/whisper-venv"
nice -n 19 "$DIR/whisper-venv/bin/pip" install -q --upgrade pip >/dev/null
nice -n 19 "$DIR/whisper-venv/bin/pip" install -q faster-whisper numpy
HF_HUB_DISABLE_TELEMETRY=1 nice -n 19 "$DIR/whisper-venv/bin/python" -c \
  "from faster_whisper import WhisperModel; WhisperModel('deepdml/faster-whisper-large-v3-turbo-ct2', device='cpu', compute_type='int8')"
"$DIR/whisper-venv/bin/python" "$DIR/nova_minion_whisper.py" --selftest
