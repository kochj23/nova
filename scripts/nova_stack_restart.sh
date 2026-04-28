#!/bin/zsh
# nova_stack_restart.sh — Bring the entire Nova stack up in correct order.
# Run this after a reboot if things are broken, or anytime services are misbehaving.
# Written by Jordan Koch.

set -e

echo "=== Nova Stack Restart ==="
echo "$(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# Step 1: Ensure Ollama is serving
echo "[1/5] Checking Ollama..."
if /usr/bin/nc -z 127.0.0.1 11434 2>/dev/null; then
    echo "  ✓ Ollama already running on port 11434"
else
    echo "  Starting Ollama serve..."
    pkill -f "Ollama.app" 2>/dev/null || true
    pkill -f "ollama serve" 2>/dev/null || true
    sleep 2
    /Applications/Ollama.app/Contents/Resources/ollama serve &>/dev/null &
    disown
    # Wait up to 30s
    for i in $(seq 1 30); do
        if /usr/bin/nc -z 127.0.0.1 11434 2>/dev/null; then
            echo "  ✓ Ollama ready after ${i}s"
            break
        fi
        sleep 1
        if [ "$i" -eq 30 ]; then
            echo "  ✗ FAILED: Ollama not responding after 30s"
            exit 1
        fi
    done
fi

# Step 2: Ensure Postgres and Redis are up
echo ""
echo "[2/5] Checking Postgres + Redis..."
if /usr/bin/nc -z 127.0.0.1 5432 2>/dev/null; then
    echo "  ✓ Postgres on 5432"
else
    echo "  Starting Postgres..."
    launchctl kickstart gui/$(id -u)/homebrew.mxcl.postgresql@17
    sleep 3
fi
if /usr/bin/nc -z 127.0.0.1 6379 2>/dev/null; then
    echo "  ✓ Redis on 6379"
else
    echo "  Starting Redis..."
    launchctl kickstart gui/$(id -u)/net.digitalnoise.redis
    sleep 2
fi

# Step 3: Memory server
echo ""
echo "[3/5] Restarting Memory Server..."
launchctl kickstart -k gui/$(id -u)/net.digitalnoise.nova-memory-server 2>/dev/null || true
sleep 3
if /usr/bin/nc -z 127.0.0.1 18790 2>/dev/null; then
    echo "  ✓ Memory Server on 18790"
else
    echo "  ⚠ Memory Server not ready yet (may need more time)"
fi

# Step 4: Gateway (the brain)
echo ""
echo "[4/5] Restarting Gateway..."
launchctl kickstart -k gui/$(id -u)/ai.openclaw.gateway 2>/dev/null || true
sleep 5
if /usr/bin/nc -z 127.0.0.1 18789 2>/dev/null; then
    echo "  ✓ Gateway on 18789"
else
    echo "  ⚠ Gateway not ready yet (may need more time for channel connections)"
fi

# Step 5: Web UIs
echo ""
echo "[5/5] Restarting OpenWebUI + TinyChat..."
launchctl kickstart -k gui/$(id -u)/net.digitalnoise.openwebui 2>/dev/null || true
launchctl kickstart -k gui/$(id -u)/net.digitalnoise.tinychat 2>/dev/null || true
sleep 5
if /usr/bin/nc -z 192.168.1.6 8000 2>/dev/null; then
    echo "  ✓ TinyChat on 8000"
else
    echo "  ⚠ TinyChat starting..."
fi
if /usr/bin/nc -z 192.168.1.6 3000 2>/dev/null; then
    echo "  ✓ OpenWebUI on 3000"
else
    echo "  ⚠ OpenWebUI starting (takes 30-60s)..."
fi

echo ""
echo "=== Done ==="
echo "Gateway log: tail -f ~/.openclaw/logs/gateway.log"
echo "To test Nova: openclaw agent --session-id b184fae0-b03c-42bb-94a4-8651313e6449 --message 'hello'"
