#!/bin/zsh
# nova_memory_server_start.sh — Start memory server after prerequisites are ready.
# Called by launchd plist. Waits for Postgres, Redis, and Ollama before exec.
# Written by Jordan Koch.

source "$HOME/.openclaw/scripts/wait-for-port.sh"

wait_for_port 5432  "PostgreSQL" 120 || exit 1
wait_for_port 6379  "Redis"      90  || exit 1
wait_for_port 11434 "Ollama"     120 || exit 1

# Wait for PostgreSQL to actually accept queries (port open ≠ ready after restart/recovery)
echo "[memory_server_start] Waiting for PostgreSQL to accept queries..."
for i in $(seq 1 30); do
    if /opt/homebrew/bin/psql -U kochj -d nova_memories -c "SELECT 1" > /dev/null 2>&1; then
        echo "[memory_server_start] PostgreSQL ready after ${i}x2s"
        break
    fi
    sleep 2
done

exec /opt/homebrew/bin/python3 "$HOME/.openclaw/memory_server.py"
