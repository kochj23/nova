#!/bin/zsh
# nova-restart.sh — Manual Nova recovery after reboot or crash.
#
# Run this when Nova services are down and you don't have Claude Code handy.
# It handles the full boot sequence including the PG I/O saturation issue.
#
# Usage:
#   ~/.openclaw/scripts/nova-restart.sh          # Full restart
#   ~/.openclaw/scripts/nova-restart.sh --status # Just check status
#   ~/.openclaw/scripts/nova-restart.sh --force  # Kill everything first
#
# Written by Jordan Koch + Claude Code (2026-06-10)

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo "${GREEN}✓${NC} $1" }
warn() { echo "${YELLOW}⚠${NC} $1" }
fail() { echo "${RED}✗${NC} $1" }
info() { echo "  $1" }

UID_NUM=$(id -u)

# ── Status Check ─────────────────────────────────────────────────────────────

check_port() {
    local port=$1 name=$2
    if lsof -i :$port -sTCP:LISTEN > /dev/null 2>&1; then
        ok "$name (port $port)"
        return 0
    else
        fail "$name (port $port) — DOWN"
        return 1
    fi
}

status_check() {
    echo "═══ Nova Service Status ═══"
    echo ""
    local all_ok=true
    check_port 5432  "PostgreSQL"     || all_ok=false
    check_port 6379  "Redis"          || all_ok=false
    check_port 11434 "Ollama"         || all_ok=false
    check_port 18790 "Memory Server"  || all_ok=false
    check_port 18792 "Gateway v2"     || all_ok=false
    check_port 37400 "NovaControl"    || all_ok=false
    check_port 37433 "NovaHomeKit"    || all_ok=false
    check_port 37465 "Presence Engine" || all_ok=false
    check_port 37468 "Automation Engine" || all_ok=false
    check_port 37469 "Endpoint Monitor" || all_ok=false
    echo ""

    # Quick health checks
    if curl -sf --max-time 3 http://192.168.1.6:18790/health > /dev/null 2>&1; then
        local count=$(curl -sf --max-time 3 http://192.168.1.6:18790/health | python3 -c "import sys,json;print(json.load(sys.stdin)['count'])" 2>/dev/null)
        ok "Memory Server healthy ($count memories)"
    fi
    if curl -sf --max-time 3 http://192.168.1.2:18792/health > /dev/null 2>&1; then
        ok "Gateway healthy"
    fi

    echo ""
    if $all_ok; then
        echo "${GREEN}All core services running.${NC}"
    else
        echo "${YELLOW}Some services are down. Run without --status to restart.${NC}"
    fi
}

if [[ "${1:-}" == "--status" ]]; then
    status_check
    exit 0
fi

# ── Force Mode ───────────────────────────────────────────────────────────────

if [[ "${1:-}" == "--force" ]]; then
    warn "Force mode: killing all Nova Python processes..."
    pkill -f "nova_gateway_v2.py" 2>/dev/null || true
    pkill -f "memory_server.py" 2>/dev/null || true
    pkill -f "nova_presence_engine.py" 2>/dev/null || true
    pkill -f "nova_automation_engine.py" 2>/dev/null || true
    sleep 2
fi

echo "═══ Nova Restart Sequence ═══"
echo ""

# ── Step 1: Verify PostgreSQL ────────────────────────────────────────────────

info "Step 1: Checking PostgreSQL..."

if ! lsof -i :5432 -sTCP:LISTEN > /dev/null 2>&1; then
    warn "PostgreSQL not running — starting via Homebrew..."
    brew services start postgresql@17 2>/dev/null || true
    sleep 5
fi

# Wait for PG to accept connections
for i in $(seq 1 15); do
    if psql -h localhost -d nova_ops -U kochj -c "SELECT 1" > /dev/null 2>&1; then
        ok "PostgreSQL accepting queries"
        break
    fi
    if [[ $i -eq 15 ]]; then
        fail "PostgreSQL not responding after 30s — check logs"
        exit 1
    fi
    sleep 2
done

# ── Step 2: Kill stuck COUNT(*) queries ──────────────────────────────────────

info "Step 2: Clearing stuck queries on nova_memories..."

STUCK=$(psql -h 127.0.0.1 -d nova_memories -U kochj -t -c "
    SELECT count(*) FROM pg_stat_activity
    WHERE datname = 'nova_memories' AND state = 'active'
      AND (query ILIKE '%count%from memories%' OR query LIKE 'ANALYZE%')
      AND pid != pg_backend_pid()
      AND query_start < now() - interval '30 seconds';
" 2>/dev/null | tr -d ' ')

if [[ "$STUCK" -gt 0 ]]; then
    warn "Found $STUCK stuck queries — killing them..."
    psql -h 127.0.0.1 -d nova_memories -U kochj -c "
        SELECT pg_terminate_backend(pid) FROM pg_stat_activity
        WHERE datname = 'nova_memories' AND state = 'active'
          AND (query ILIKE '%count%from memories%' OR query LIKE 'ANALYZE%')
          AND pid != pg_backend_pid()
          AND query_start < now() - interval '30 seconds';
    " > /dev/null 2>&1
    sleep 2
    ok "Stuck queries terminated"
else
    ok "No stuck queries"
fi

# ── Step 3: Verify Redis ─────────────────────────────────────────────────────

info "Step 3: Checking Redis..."
if redis-cli ping > /dev/null 2>&1; then
    ok "Redis responding"
else
    warn "Redis not responding — checking launchd..."
    launchctl kickstart gui/$UID_NUM/net.digitalnoise.redis 2>/dev/null || true
    sleep 3
    if redis-cli ping > /dev/null 2>&1; then
        ok "Redis started"
    else
        fail "Redis won't start — check for port conflict"
        exit 1
    fi
fi

# ── Step 4: Verify Ollama ────────────────────────────────────────────────────

info "Step 4: Checking Ollama..."
if curl -sf --max-time 3 http://127.0.0.1:11434/api/tags > /dev/null 2>&1; then
    ok "Ollama responding"
else
    warn "Ollama not responding — launching app..."
    open -a Ollama 2>/dev/null || true
    for i in $(seq 1 20); do
        if curl -sf --max-time 3 http://127.0.0.1:11434/api/tags > /dev/null 2>&1; then
            ok "Ollama started"
            break
        fi
        if [[ $i -eq 20 ]]; then
            warn "Ollama slow to start — continuing (non-blocking)"
        fi
        sleep 3
    done
fi

# ── Step 5: Start Memory Server ──────────────────────────────────────────────

info "Step 5: Starting Memory Server..."

if curl -sf --max-time 3 http://192.168.1.6:18790/health > /dev/null 2>&1; then
    ok "Memory Server already running"
else
    # Kill zombie if port bound but not responding
    pkill -f "memory_server.py" 2>/dev/null || true
    sleep 2

    # Reset launchd throttle
    launchctl bootout gui/$UID_NUM/net.digitalnoise.nova-memory-server 2>/dev/null || true
    sleep 1
    launchctl bootstrap gui/$UID_NUM ~/Library/LaunchAgents/net.digitalnoise.nova-memory-server.plist 2>/dev/null || true

    # Wait for health
    for i in $(seq 1 30); do
        if curl -sf --max-time 3 http://192.168.1.6:18790/health > /dev/null 2>&1; then
            ok "Memory Server healthy"
            break
        fi
        if [[ $i -eq 30 ]]; then
            fail "Memory Server won't start after 60s"
            info "Check: tail ~/.openclaw/logs/memory-server*.log"
            info "Common fix: kill stuck PG queries and retry"
            exit 1
        fi
        sleep 2
    done
fi

# ── Step 6: Start Gateway ────────────────────────────────────────────────────

info "Step 6: Starting Gateway v2..."

if curl -sf --max-time 3 http://192.168.1.2:18792/health > /dev/null 2>&1; then
    ok "Gateway already running"
else
    pkill -f "nova_gateway_v2.py" 2>/dev/null || true
    sleep 2
    launchctl bootout gui/$UID_NUM/net.digitalnoise.nova-gateway-v2 2>/dev/null || true
    sleep 1
    launchctl bootstrap gui/$UID_NUM ~/Library/LaunchAgents/net.digitalnoise.nova-gateway-v2.plist 2>/dev/null || true

    # Gateway has a 10s signal-cli wait + startup time
    for i in $(seq 1 20); do
        if curl -sf --max-time 3 http://192.168.1.2:18792/health > /dev/null 2>&1; then
            ok "Gateway healthy"
            break
        fi
        if [[ $i -eq 20 ]]; then
            fail "Gateway won't start after 40s"
            info "Check: launchctl list | grep gateway"
            exit 1
        fi
        sleep 2
    done
fi

# ── Step 7: Start Cloudflare Tunnel ──────────────────────────────────────────

info "Step 7: Checking Cloudflare Tunnel..."
if pgrep -f "cloudflared tunnel" > /dev/null 2>&1; then
    ok "Cloudflare Tunnel running"
else
    warn "Tunnel not running — starting..."
    cloudflared tunnel run &>/tmp/cloudflared.log &
    sleep 3
    if pgrep -f "cloudflared tunnel" > /dev/null 2>&1; then
        ok "Cloudflare Tunnel started"
    else
        warn "Tunnel failed to start — chat.digitalnoise.net will be down"
    fi
fi

# ── Step 8: Kick remaining services ─────────────────────────────────────────

info "Step 8: Ensuring other services are running..."

SERVICES=(
    "net.digitalnoise.nova-presence-engine"
    "net.digitalnoise.nova-automation-engine"
    "net.digitalnoise.nova-endpoint-monitor"
    "net.digitalnoise.big-brother"
    "net.digitalnoise.nova-snmp-poller"
    "net.digitalnoise.nova-capacity"
    "net.digitalnoise.nova-hue"
    "com.nova.scheduler"
)

for svc in "${SERVICES[@]}"; do
    status=$(launchctl list | grep "$svc" | awk '{print $2}')
    if [[ "$status" == "-" || -z "$status" ]]; then
        launchctl kickstart gui/$UID_NUM/$svc 2>/dev/null && info "  Started $svc" || true
    fi
done
ok "Background services checked"

# ── Step 9: Load SSH keys ────────────────────────────────────────────────────

info "Step 9: Loading SSH keys..."
if ssh-add -l > /dev/null 2>&1; then
    ok "SSH keys already loaded"
else
    ssh-add --apple-use-keychain ~/.ssh/id_ed25519 2>/dev/null && ok "SSH key loaded from Keychain" || warn "SSH key load failed"
fi

# ── Final Status ─────────────────────────────────────────────────────────────

echo ""
echo "═══ Restart Complete ═══"
echo ""
status_check
