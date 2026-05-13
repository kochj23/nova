#!/bin/zsh
# nova_gateway_v2_start.sh — Start Nova Gateway v2 with Keychain secrets loaded.
# Written by Jordan Koch.

source "$HOME/.openclaw/scripts/wait-for-port.sh"

# Wait for dependencies
wait_for_port 5432  "PostgreSQL" 120 || exit 1
wait_for_port 6379  "Redis"      60  || exit 1
wait_for_port 11434 "Ollama"     120 || exit 1
wait_for_port 8080  "signal-cli" 60  || true  # Signal optional at startup

echo "[gateway_v2_start] Dependencies ready, starting Nova Gateway v2..."

exec /opt/homebrew/bin/python3 "$HOME/.openclaw/scripts/nova_gateway_v2.py"
