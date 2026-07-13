#!/bin/zsh
# dream_run.sh — Wrapper for dream_generate.py.
# OpenClaw's exec preflight rejects compound commands (cd && python3).
# This wrapper lets Nova call a single script path instead.
# Written by Jordan Koch.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec /opt/homebrew/bin/python3 "$SCRIPT_DIR/dream_generate.py" "$@"
