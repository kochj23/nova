#!/bin/zsh
# nova_control_web_start.sh — Start Nova Control Web dashboard.
# Called by launchd. Written by Jordan Koch.

source "$HOME/.openclaw/scripts/wait-for-port.sh"
wait_for_port 11434 "Ollama" 120 || exit 1

export HOME="${HOME:-/Users/$(whoami)}"
cd "$HOME/.openclaw/apps/nova-control-web"
exec /opt/homebrew/bin/python3 server.py
