#!/bin/zsh
# ollama_serve_start.sh — Start Ollama serve process directly (no GUI app).
# More reliable than Ollama.app for headless server mode after reboot.
# Written by Jordan Koch.

export HOME="${HOME:-/Users/$(whoami)}"
export OLLAMA_HOST=127.0.0.1:11434
export OLLAMA_MODELS="$HOME/.ollama/models"

exec /Applications/Ollama.app/Contents/Resources/ollama serve
