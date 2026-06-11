#!/bin/zsh --no-rcs
# tinychat_start.sh — Start TinyChat after Ollama is ready.
# Called by launchd. Written by Jordan Koch.

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
source "$HOME/.openclaw/scripts/wait-for-port.sh"
wait_for_port 11434 "Ollama" 120 || exit 1

unset PYTHONPATH
export PORT=8000
export HOST=192.168.1.6
export OPENAI_API_BASE=http://192.168.1.6:11434/v1
export OPENAI_API_KEY=ollama
export LLM_MODEL=deepseek-r1:8b
export HOME="${HOME:-/Users/$(whoami)}"
cd /Volumes/Data/tinychat/chatbot
exec /Volumes/Data/tinychat/venv/bin/python3 run.py
