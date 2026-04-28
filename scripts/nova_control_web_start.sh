#!/bin/zsh
# nova_control_web_start.sh — Start Nova Control Web dashboard.
# Called by launchd. Written by Jordan Koch.

export HOME="${HOME:-/Users/$(whoami)}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

source "$HOME/.openclaw/scripts/wait-for-port.sh"
wait_for_port 11434 "Ollama" 120 || exit 1

cd "$HOME/.openclaw/apps/nova-control-web"
exec /opt/homebrew/bin/python3 server.py
