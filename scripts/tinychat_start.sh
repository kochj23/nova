#!/bin/zsh
# tinychat_start.sh — Start TinyChat after Ollama is ready.
# Called by launchd. Written by Jordan Koch.

source "$HOME/.openclaw/scripts/wait-for-port.sh"
wait_for_port 11434 "Ollama" 120 || exit 1

export PORT=8000
export OPENAI_API_BASE=http://127.0.0.1:11434/v1
export OPENAI_API_KEY=ollama
export LLM_MODEL=deepseek-r1:8b
export HOME="${HOME:-/Users/$(whoami)}"
cd /Volumes/Data/tinychat/chatbot
exec /Volumes/Data/tinychat/venv/bin/python3 run.py
