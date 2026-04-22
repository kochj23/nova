#!/bin/zsh
# wait-for-port.sh — Block until a TCP port is accepting connections.
# Usage: source wait-for-port.sh; wait_for_port 11434 "Ollama" 60
# Written by Jordan Koch.

wait_for_port() {
    local port="$1"
    local name="${2:-service}"
    local timeout="${3:-90}"
    local elapsed=0

    while ! /usr/bin/nc -z 127.0.0.1 "$port" 2>/dev/null; do
        if [ "$elapsed" -ge "$timeout" ]; then
            echo "[wait-for-port] TIMEOUT: $name (port $port) not ready after ${timeout}s" >&2
            return 1
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    echo "[wait-for-port] $name (port $port) ready after ${elapsed}s"
    return 0
}
