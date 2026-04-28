#!/bin/zsh
# ollama_serve_start.sh — Start Ollama serve process directly (no GUI app).
# More reliable than Ollama.app for headless server mode after reboot.
# Written by Jordan Koch.

export HOME="${HOME:-/Users/$(whoami)}"
export OLLAMA_HOST=127.0.0.1:11434
export OLLAMA_MODELS="$HOME/.ollama/models"

# Performance tuning — M3 Ultra 512GB (updated 2026-04-28)
export OLLAMA_MAX_LOADED_MODELS=6
export OLLAMA_NUM_PARALLEL=4
export OLLAMA_KEEP_ALIVE=24h
export OLLAMA_FLASH_ATTENTION=1
export OLLAMA_KV_CACHE_TYPE=q8_0

exec /Applications/Ollama.app/Contents/Resources/ollama serve
