#!/bin/zsh
# nova_memory_server_start.sh — Start memory server after prerequisites are ready.
# Called by launchd plist. Waits for Postgres, Redis, and Ollama before exec.
# Written by Jordan Koch.

source "$HOME/.openclaw/scripts/wait-for-port.sh"

wait_for_port 5432  "PostgreSQL" 120 || exit 1
wait_for_port 6379  "Redis"      90  || exit 1
wait_for_port 11434 "Ollama"     120 || exit 1

exec /opt/homebrew/bin/python3 "$HOME/.openclaw/memory_server.py"
