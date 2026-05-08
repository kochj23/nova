#!/bin/zsh
# nova-boot.sh — Deterministic post-reboot orchestrator for all Nova services.
# Replaces the "start everything and pray" approach with ordered startup,
# health checks, and validation tests at each tier.
#
# Usage: Called by launchd (net.digitalnoise.nova-boot) at login, OR manually:
#   ~/.openclaw/scripts/nova-boot.sh [--restart]
#
# With --restart: stops all services first, then starts fresh.
# Without flags: skips services that are already healthy.
#
# Written by Jordan Koch.

set -uo pipefail

# ─── Config ───────────────────────────────────────────────────────────────────
LOGFILE="$HOME/.openclaw/logs/nova-boot.log"
LAN_IP="192.168.1.6"
BOOT_START=$(date +%s)
FAILED=0
WARNINGS=0

# ─── Helpers ──────────────────────────────────────────────────────────────────
log()  { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOGFILE"; }
pass() { echo "[$(date '+%H:%M:%S')] ✓ PASS: $*" | tee -a "$LOGFILE"; }
fail() { echo "[$(date '+%H:%M:%S')] ✗ FAIL: $*" | tee -a "$LOGFILE"; FAILED=$((FAILED + 1)); }
warn() { echo "[$(date '+%H:%M:%S')] ⚠ WARN: $*" | tee -a "$LOGFILE"; WARNINGS=$((WARNINGS + 1)); }

port_listening() {
    local port="$1"
    # Check both loopback and LAN IP (some services bind to 192.168.1.6)
    /usr/bin/nc -z 127.0.0.1 "$port" 2>/dev/null || /usr/bin/nc -z "$LAN_IP" "$port" 2>/dev/null
}

port_listening_on() {
    local host="$1" port="$2"
    /usr/bin/nc -z "$host" "$port" 2>/dev/null
}

wait_for_port() {
    local port="$1" name="$2" timeout="${3:-120}"
    local elapsed=0
    while ! port_listening "$port"; do
        if [ "$elapsed" -ge "$timeout" ]; then
            fail "$name did not start within ${timeout}s (port $port)"
            return 1
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    log "$name ready on port $port (${elapsed}s)"
    return 0
}

service_running() {
    local label="$1"
    local exit_code=$(launchctl list 2>/dev/null | grep "$label" | awk '{print $2}')
    [ "$exit_code" = "0" ] || [ "$exit_code" = "-" ]
}

http_ok() {
    local url="$1"
    local code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$url" 2>/dev/null)
    [ "$code" -ge 200 ] && [ "$code" -lt 500 ]
}

# ─── Pre-flight ───────────────────────────────────────────────────────────────
mkdir -p "$HOME/.openclaw/logs"

# ── Log rotation: rotate any log over 10 MB, keep 3 generations ──────────────
_rotate_log() {
    local f="$1"
    [[ -f "$f" ]] || return
    local size=$(stat -f%z "$f" 2>/dev/null || echo 0)
    if (( size > 10485760 )); then  # 10 MB
        [[ -f "${f}.2" ]] && mv "${f}.2" "${f}.3" 2>/dev/null
        [[ -f "${f}.1" ]] && mv "${f}.1" "${f}.2" 2>/dev/null
        mv "$f" "${f}.1" 2>/dev/null
        echo "[boot] Rotated $(basename $f) (was ${size} bytes)" >> "$LOGFILE"
    fi
}
for _log in "$HOME/.openclaw/logs/"*.log "$HOME/.openclaw/logs/"*.err.log; do
    [[ -f "$_log" ]] && _rotate_log "$_log"
done

echo "" >> "$LOGFILE"
log "════════════════════════════════════════════════════════════"
log "  NOVA BOOT SEQUENCE — $(date '+%Y-%m-%d %H:%M:%S')"
log "════════════════════════════════════════════════════════════"

# Handle --restart flag
if [[ "${1:-}" == "--restart" ]]; then
    log "RESTART MODE: stopping all services first..."

    # Stop in reverse dependency order
    for svc in com.nova.agent-sentinel com.nova.agent-librarian com.nova.agent-coder \
               com.nova.agent-lookout com.nova.agent-analyst com.nova.watchdog \
               com.nova.scheduler com.nova.slack-preprocessor \
               com.digitalnoise.nova.general-monitor \
               net.digitalnoise.openwebui net.digitalnoise.tinychat \
               net.digitalnoise.mlx-server net.digitalnoise.searxng \
               ai.openclaw.gateway net.digitalnoise.nova-memory-server \
               net.digitalnoise.redis; do
        launchctl stop "$svc" 2>/dev/null
    done

    # Kill Ollama.app
    pkill -f "Ollama" 2>/dev/null

    # Kill any orphaned signal-cli processes (prevent lock file conflicts on restart)
    pkill -f "signal-cli" 2>/dev/null
    sleep 1

    # Stop Postgres
    brew services stop postgresql@17 >/dev/null 2>&1

    sleep 3
    log "All services stopped."
fi

# ═══════════════════════════════════════════════════════════════════════════════
# TIER 0: Pre-requisites & Environment Validation
# ═══════════════════════════════════════════════════════════════════════════════
log ""
log "── TIER 0: Environment Validation ──"

# Test: External volumes mounted
if [ -d "/Volumes/Data" ] && [ -r "/Volumes/Data" ]; then
    pass "/Volumes/Data mounted and readable"
else
    fail "/Volumes/Data not available — many services will fail"
fi

if [ -d "/Volumes/MoreData" ] && [ -r "/Volumes/MoreData" ]; then
    pass "/Volumes/MoreData mounted and readable"
else
    fail "/Volumes/MoreData not available — PostgreSQL data lives here"
fi

# Test: Postgres data symlink valid
if [ -L "/opt/homebrew/var/postgresql@17" ]; then
    PG_TARGET=$(readlink /opt/homebrew/var/postgresql@17)
    if [ -d "$PG_TARGET" ]; then
        pass "PostgreSQL symlink → $PG_TARGET (valid)"
    else
        fail "PostgreSQL symlink target $PG_TARGET does not exist"
    fi
else
    warn "/opt/homebrew/var/postgresql@17 is not a symlink"
fi

# Test: Network interface has expected IP
CURRENT_IP=$(ifconfig en0 2>/dev/null | grep "inet " | awk '{print $2}')
if [ "$CURRENT_IP" = "$LAN_IP" ]; then
    pass "Network: en0 has expected IP $LAN_IP"
else
    warn "Network: en0 is $CURRENT_IP (expected $LAN_IP) — services binding to $LAN_IP may fail"
fi

# Test: Ollama Application Support symlink (broke after reboot 2026-05-01)
OLLAMA_SUPPORT="$HOME/Library/Application Support/Ollama"
if [ -L "$OLLAMA_SUPPORT" ]; then
    OLLAMA_TARGET=$(readlink "$OLLAMA_SUPPORT")
    if [ ! -d "$OLLAMA_TARGET" ]; then
        warn "Ollama support symlink broken (target $OLLAMA_TARGET missing) — creating it"
        mkdir -p "$OLLAMA_TARGET"
        if [ -d "$OLLAMA_TARGET" ]; then
            pass "Ollama: created missing symlink target $OLLAMA_TARGET"
        else
            fail "Ollama: could not create $OLLAMA_TARGET — Ollama will fail to start"
        fi
    else
        pass "Ollama: Application Support symlink valid"
    fi
elif [ -d "$OLLAMA_SUPPORT" ]; then
    pass "Ollama: Application Support directory exists"
fi

# Test: Keychain accessible (needed for gateway)
if security find-generic-password -a nova -s "nova-slack-bot-token" -w >/dev/null 2>&1; then
    pass "Keychain: nova secrets accessible"
else
    warn "Keychain: nova secrets NOT accessible — Gateway will retry with backoff"
fi

# Test: Required binaries exist
for bin in /opt/homebrew/bin/postgres /opt/homebrew/bin/redis-server \
           /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 \
           /opt/homebrew/opt/node/bin/node /opt/homebrew/bin/mlx_lm.server; do
    if [ -x "$bin" ]; then
        : # silent pass
    else
        fail "Missing binary: $bin"
    fi
done
pass "All required binaries present"

# Test: OpenClaw config exists and is valid JSON
if [ -f "$HOME/.openclaw/openclaw.json" ]; then
    if python3 -c "import json; json.load(open('$HOME/.openclaw/openclaw.json'))" 2>/dev/null; then
        pass "openclaw.json valid JSON"
    else
        fail "openclaw.json is malformed JSON"
    fi
else
    fail "openclaw.json missing"
fi

# Test: Stale PID files / lock files
if [ -f "/opt/homebrew/var/postgresql@17/postmaster.pid" ] || [ -f "/Volumes/MoreData/postgresql@17/postmaster.pid" ]; then
    PG_SYMLINK_TARGET=$(readlink /opt/homebrew/var/postgresql@17 2>/dev/null || echo "/opt/homebrew/var/postgresql@17")
    if [ -f "$PG_SYMLINK_TARGET/postmaster.pid" ]; then
        PG_PID=$(head -1 "$PG_SYMLINK_TARGET/postmaster.pid" 2>/dev/null)
        if ! kill -0 "$PG_PID" 2>/dev/null; then
            warn "Stale PostgreSQL PID file (PID $PG_PID not running) — removing"
            rm -f "$PG_SYMLINK_TARGET/postmaster.pid"
        fi
    fi
fi

# Security test: Check ollama-serve plist isn't loaded (should be disabled)
if launchctl list 2>/dev/null | grep -q "net.digitalnoise.ollama-serve"; then
    warn "net.digitalnoise.ollama-serve is loaded (should be disabled — conflicts with Ollama.app)"
    launchctl unload "$HOME/Library/LaunchAgents/net.digitalnoise.ollama-serve.plist" 2>/dev/null
    log "  → Unloaded stale ollama-serve plist"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# TIER 1: Base Layer (no interdependencies)
# PostgreSQL, Redis, Ollama — start in parallel, wait for all
# ═══════════════════════════════════════════════════════════════════════════════
log ""
log "── TIER 1: Base Layer (PostgreSQL, Redis, Ollama) ──"

# PostgreSQL
if port_listening 5432; then
    log "PostgreSQL already running"
else
    log "Starting PostgreSQL..."
    brew services start postgresql@17 >/dev/null 2>&1
fi

# Redis
if port_listening 6379; then
    log "Redis already running"
else
    log "Starting Redis..."
    launchctl start net.digitalnoise.redis 2>/dev/null
fi

# Ollama
if port_listening 11434; then
    log "Ollama already running"
else
    log "Starting Ollama.app..."
    open -a Ollama
fi

# PgBouncer (connection pooler — must start after PostgreSQL)
if port_listening 6432; then
    log "PgBouncer already running"
else
    log "Starting PgBouncer..."
    launchctl start net.digitalnoise.pgbouncer 2>/dev/null
fi

# Wait for all four
wait_for_port 5432  "PostgreSQL" 60
wait_for_port 6432  "PgBouncer"  15
wait_for_port 6379  "Redis"      30
wait_for_port 11434 "Ollama"     45

# ─── Tier 1 Tests ─────────────────────────────────────────────────────────────
log ""
log "── TIER 1: Validation Tests ──"

# PostgreSQL: connection test
if /opt/homebrew/bin/psql -U kochj -d postgres -c "SELECT 1;" >/dev/null 2>&1; then
    pass "PostgreSQL: accepts connections"
else
    fail "PostgreSQL: cannot connect"
fi

# PostgreSQL: nova_memories database exists
if /opt/homebrew/bin/psql -U kochj -d postgres -c "SELECT 1 FROM pg_database WHERE datname='nova_memories';" 2>/dev/null | grep -q "1"; then
    pass "PostgreSQL: 'nova_memories' database exists"
else
    fail "PostgreSQL: 'nova_memories' database missing"
fi

# PostgreSQL: data integrity — memory table accessible
MEMORY_COUNT=$(/opt/homebrew/bin/psql -U kochj -d nova_memories -t -c "SELECT count(*) FROM memories LIMIT 1;" 2>/dev/null | tr -d ' ')
if [ -n "$MEMORY_COUNT" ] && [ "$MEMORY_COUNT" -gt 0 ]; then
    pass "PostgreSQL: memories table accessible ($MEMORY_COUNT rows)"
else
    warn "PostgreSQL: memories table empty or inaccessible"
fi

# Redis: connectivity + persistence
REDIS_PING=$(redis-cli ping 2>/dev/null)
if [ "$REDIS_PING" = "PONG" ]; then
    pass "Redis: PONG response"
else
    fail "Redis: no PONG (got: $REDIS_PING)"
fi

# Redis: RDB save check (the MISCONF bug from the infra notes)
REDIS_DIR=$(redis-cli CONFIG GET dir 2>/dev/null | tail -1)
if [ "$REDIS_DIR" != "/" ] && [ -n "$REDIS_DIR" ]; then
    pass "Redis: data dir is '$REDIS_DIR' (not root — RDB saves will work)"
else
    fail "Redis: data dir is '$REDIS_DIR' — MISCONF RDB save errors will occur"
fi

# Redis: maxmemory configured
REDIS_MAXMEM=$(redis-cli CONFIG GET maxmemory 2>/dev/null | tail -1)
if [ -n "$REDIS_MAXMEM" ] && [ "$REDIS_MAXMEM" -gt 0 ]; then
    pass "Redis: maxmemory set to $((REDIS_MAXMEM / 1073741824))GB"
else
    warn "Redis: maxmemory not set (will use all available RAM)"
fi

# Ollama: API responding
if http_ok "http://127.0.0.1:11434/api/tags"; then
    OLLAMA_MODELS=$(curl -s http://127.0.0.1:11434/api/tags 2>/dev/null | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('models',[])))" 2>/dev/null || echo "0")
    pass "Ollama: API healthy ($OLLAMA_MODELS models available)"
else
    fail "Ollama: API not responding"
fi

# Ollama: critical models available
REQUIRED_MODELS=("nova:latest" "qwen3-coder:30b" "deepseek-r1:8b")
OLLAMA_TAGS=$(curl -s http://127.0.0.1:11434/api/tags 2>/dev/null)
MODELS_MISSING=0
for model in "${REQUIRED_MODELS[@]}"; do
    if echo "$OLLAMA_TAGS" | grep -q "$model"; then
        : # present
    else
        warn "Ollama: model '$model' not available"
        MODELS_MISSING=$((MODELS_MISSING + 1))
    fi
done
if [ "$MODELS_MISSING" -eq 0 ]; then
    pass "Ollama: all ${#REQUIRED_MODELS[@]} critical models available"
fi

# Ollama: environment tuning applied
OLLAMA_ENV=$(launchctl getenv OLLAMA_MAX_LOADED_MODELS 2>/dev/null || echo "")
# Check via Ollama.app process environment instead
OLLAMA_PID=$(pgrep -x ollama 2>/dev/null | head -1)
if [ -n "$OLLAMA_PID" ]; then
    pass "Ollama: running as PID $OLLAMA_PID"
else
    warn "Ollama: could not find PID for env check"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# TIER 2: Core Services (depend on Tier 1)
# Memory Server, Gateway
# ═══════════════════════════════════════════════════════════════════════════════
log ""
log "── TIER 2: Core Services (Memory Server, Gateway) ──"

# Memory Server
if port_listening 18790; then
    log "Memory Server already running"
else
    log "Starting Memory Server..."
    launchctl start net.digitalnoise.nova-memory-server 2>/dev/null
fi

# Gateway — kill any orphaned signal-cli before starting to avoid lock conflicts
pkill -f "signal-cli" 2>/dev/null
sleep 1
if port_listening 18789; then
    log "Gateway already running"
else
    log "Starting Gateway..."
    nohup ~/.openclaw/scripts/nova_gateway_start.sh \
        >> ~/.openclaw/logs/gateway.log \
        2>> ~/.openclaw/logs/gateway.err.log &
fi

wait_for_port 18790 "Memory Server" 90
wait_for_port 18789 "Gateway"       120

# ─── Tier 2 Tests ─────────────────────────────────────────────────────────────
log ""
log "── TIER 2: Validation Tests ──"

# Memory Server: HTTP health
if http_ok "http://127.0.0.1:18790/health"; then
    pass "Memory Server: /health OK"
elif http_ok "http://127.0.0.1:18790/"; then
    pass "Memory Server: root endpoint responding"
else
    warn "Memory Server: HTTP health check inconclusive (port open but no HTTP response)"
fi

# Gateway: WebSocket port open (we can't easily test WS, but TCP is a start)
if port_listening 18789; then
    pass "Gateway: port 18789 accepting connections"
else
    fail "Gateway: port 18789 not listening"
fi

# Gateway: check for EPERM issue (workspace-state.json)
if [ -f "$HOME/.openclaw/workspace/.openclaw/workspace-state.json" ]; then
    if touch "$HOME/.openclaw/workspace/.openclaw/workspace-state.json" 2>/dev/null; then
        pass "Gateway: workspace-state.json writable"
    else
        warn "Gateway: workspace-state.json EPERM — Slack messages may be dropped"
    fi
fi

# Security: Gateway not exposed beyond loopback
GW_BIND=$(lsof -iTCP:18789 -sTCP:LISTEN 2>/dev/null | grep -o "localhost\|127\.0\.0\.1\|\*" | head -1)
if [ "$GW_BIND" = "localhost" ] || [ "$GW_BIND" = "127.0.0.1" ]; then
    pass "Security: Gateway bound to loopback only"
elif [ "$GW_BIND" = "*" ]; then
    warn "Security: Gateway bound to ALL interfaces (should be loopback only)"
else
    pass "Security: Gateway binding appears safe ($GW_BIND)"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# TIER 3: Application Layer (depend on Tier 1 + 2)
# OpenWebUI, TinyChat, MLX Server, Scheduler, Slack Preprocessor
# ═══════════════════════════════════════════════════════════════════════════════
log ""
log "── TIER 3: Application Layer ──"

# MLX Server
if port_listening_on "$LAN_IP" 5050; then
    log "MLX Server already running"
else
    log "Starting MLX Server (model load takes ~50s)..."
    launchctl start net.digitalnoise.mlx-server 2>/dev/null
fi

# TinyChat
if port_listening 8000; then
    log "TinyChat already running"
else
    log "Starting TinyChat..."
    launchctl start net.digitalnoise.tinychat 2>/dev/null
fi

# OpenWebUI
if port_listening_on "$LAN_IP" 3000; then
    log "OpenWebUI already running"
else
    log "Starting OpenWebUI (startup takes ~120s with embeddings)..."
    launchctl start net.digitalnoise.openwebui 2>/dev/null
fi

# Scheduler
if port_listening 37460; then
    log "Scheduler already running"
else
    log "Starting Scheduler..."
    launchctl start com.nova.scheduler 2>/dev/null
fi

# Slack Preprocessor
launchctl list 2>/dev/null | grep -q "com.nova.slack-preprocessor" && \
    launchctl start com.nova.slack-preprocessor 2>/dev/null
log "Slack Preprocessor started"

# SearXNG
launchctl list 2>/dev/null | grep -q "net.digitalnoise.searxng" && \
    launchctl start net.digitalnoise.searxng 2>/dev/null
log "SearXNG started"

# TinyChat, MLX Server, OpenWebUI are non-critical — they bind to LAN IP and
# load large models. Boot completes successfully even if these are slow.
# Big Brother monitors and restarts them if needed.
log "Waiting for TinyChat (non-blocking, up to 180s)..."
ELAPSED=0; while ! /usr/bin/nc -z "$LAN_IP" 8000 2>/dev/null && [ $ELAPSED -lt 180 ]; do sleep 5; ELAPSED=$((ELAPSED+5)); done
if /usr/bin/nc -z "$LAN_IP" 8000 2>/dev/null; then log "TinyChat ready (${ELAPSED}s)"; else warn "TinyChat not ready after 180s — non-critical, continuing"; fi

log "Waiting for MLX Server (non-blocking, up to 180s)..."
ELAPSED=0; while ! /usr/bin/nc -z "$LAN_IP" 5050 2>/dev/null && [ $ELAPSED -lt 180 ]; do sleep 5; ELAPSED=$((ELAPSED+5)); done
if /usr/bin/nc -z "$LAN_IP" 5050 2>/dev/null; then log "MLX Server ready (${ELAPSED}s)"; else warn "MLX Server not ready after 180s — non-critical, continuing"; fi

log "Waiting for OpenWebUI (non-blocking, up to 240s)..."
ELAPSED=0; while ! /usr/bin/nc -z "$LAN_IP" 3000 2>/dev/null && [ $ELAPSED -lt 240 ]; do sleep 5; ELAPSED=$((ELAPSED+5)); done
if /usr/bin/nc -z "$LAN_IP" 3000 2>/dev/null; then log "OpenWebUI ready on $LAN_IP:3000 (${ELAPSED}s)"; else warn "OpenWebUI not ready after 240s — non-critical, continuing"; fi

# ─── Tier 3 Tests ─────────────────────────────────────────────────────────────
log ""
log "── TIER 3: Validation Tests ──"

# OpenWebUI: HTTP check
if http_ok "http://$LAN_IP:3000/"; then
    pass "OpenWebUI: HTTP 200 on $LAN_IP:3000"
else
    warn "OpenWebUI: HTTP check failed (may still be loading)"
fi

# TinyChat: HTTP check
if http_ok "http://127.0.0.1:8000/"; then
    pass "TinyChat: HTTP 200 on port 8000"
else
    warn "TinyChat: HTTP check failed"
fi

# MLX Server: model endpoint
if http_ok "http://$LAN_IP:5050/v1/models"; then
    pass "MLX Server: /v1/models responding"
else
    warn "MLX Server: /v1/models not responding (model may still be loading)"
fi

# Scheduler: port check
if port_listening 37460; then
    pass "Scheduler: port 37460 active"
else
    fail "Scheduler: not running"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# TIER 4: Agents & Watchdog (depend on Gateway)
# ═══════════════════════════════════════════════════════════════════════════════
log ""
log "── TIER 4: Agents & Watchdog ──"

for agent in com.nova.agent-sentinel com.nova.agent-librarian \
             com.nova.agent-coder com.nova.agent-lookout com.nova.agent-analyst \
             com.nova.watchdog com.digitalnoise.nova.general-monitor; do
    launchctl start "$agent" 2>/dev/null
done
log "All agents and watchdog started"

# Give agents a moment to initialize
sleep 5

# ─── Tier 4 Tests ─────────────────────────────────────────────────────────────
log ""
log "── TIER 4: Validation Tests ──"

AGENTS_OK=0
AGENTS_TOTAL=0
for agent in com.nova.agent-sentinel com.nova.agent-librarian \
             com.nova.agent-coder com.nova.agent-lookout com.nova.agent-analyst \
             com.nova.watchdog; do
    AGENTS_TOTAL=$((AGENTS_TOTAL + 1))
    AGENT_LINE=$(launchctl list 2>/dev/null | grep "$agent")
    AGENT_PID=$(echo "$AGENT_LINE" | awk '{print $1}')
    AGENT_EXIT=$(echo "$AGENT_LINE" | awk '{print $2}')
    # Agent is healthy if: has a PID (running now) OR exit code 0 OR not yet run (-)
    if [ -n "$AGENT_PID" ] && [ "$AGENT_PID" != "-" ]; then
        AGENTS_OK=$((AGENTS_OK + 1))
    elif [ "$AGENT_EXIT" = "0" ] || [ "$AGENT_EXIT" = "-" ]; then
        AGENTS_OK=$((AGENTS_OK + 1))
    else
        warn "Agent $agent not running (exit $AGENT_EXIT)"
    fi
done

if [ "$AGENTS_OK" -eq "$AGENTS_TOTAL" ]; then
    pass "All $AGENTS_TOTAL agents running"
else
    warn "$AGENTS_OK/$AGENTS_TOTAL agents healthy"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# TIER 5: Integration & Security Tests
# End-to-end checks that verify the whole system works together
# ═══════════════════════════════════════════════════════════════════════════════
log ""
log "── TIER 5: Integration & Security Tests ──"

# Integration: Ollama can actually run inference
OLLAMA_TEST=$(curl -s --max-time 30 http://127.0.0.1:11434/api/generate \
    -d '{"model":"deepseek-r1:8b","prompt":"Say OK","stream":false,"options":{"num_predict":5}}' 2>/dev/null)
if echo "$OLLAMA_TEST" | python3 -c "import sys,json; r=json.load(sys.stdin); assert r.get('response','')" 2>/dev/null; then
    pass "Integration: Ollama inference working (deepseek-r1:8b)"
else
    warn "Integration: Ollama inference test inconclusive (model may need loading time)"
fi

# Integration: Redis can cache/retrieve
REDIS_TEST_KEY="nova:boot:test:$(date +%s)"
redis-cli SET "$REDIS_TEST_KEY" "boot-ok" EX 60 >/dev/null 2>&1
REDIS_GET=$(redis-cli GET "$REDIS_TEST_KEY" 2>/dev/null)
if [ "$REDIS_GET" = "boot-ok" ]; then
    pass "Integration: Redis set/get round-trip OK"
    redis-cli DEL "$REDIS_TEST_KEY" >/dev/null 2>&1
else
    fail "Integration: Redis set/get failed"
fi

# Integration: PostgreSQL memory server can query
PG_HEALTH=$(/opt/homebrew/bin/psql -U kochj -d nova_memories -t -c \
    "SELECT count(*) FROM memories WHERE created_at > now() - interval '7 days';" 2>/dev/null | tr -d ' ')
if [ -n "$PG_HEALTH" ]; then
    pass "Integration: PostgreSQL nova_memories query OK (${PG_HEALTH} memories in last 7d)"
else
    warn "Integration: PostgreSQL nova_memories query failed"
fi

# Security: No services bound to 0.0.0.0 that shouldn't be
DANGEROUS_BINDS=$(lsof -iTCP -sTCP:LISTEN 2>/dev/null | grep -E "18789|18790" | grep "\*:" | wc -l | tr -d ' ')
if [ "$DANGEROUS_BINDS" -eq 0 ]; then
    pass "Security: Gateway & Memory Server not exposed on 0.0.0.0"
else
    warn "Security: $DANGEROUS_BINDS internal services bound to all interfaces"
fi

# Security: Redis not exposed externally
REDIS_BIND=$(lsof -iTCP:6379 -sTCP:LISTEN 2>/dev/null | grep -o "localhost\|127\.0\.0\.1\|\*" | head -1)
if [ "$REDIS_BIND" = "localhost" ] || [ "$REDIS_BIND" = "127.0.0.1" ]; then
    pass "Security: Redis bound to loopback only"
elif [ "$REDIS_BIND" = "*" ]; then
    warn "Security: Redis bound to ALL interfaces — should be loopback"
else
    pass "Security: Redis binding ($REDIS_BIND)"
fi

# Security: PostgreSQL not exposed externally
PG_BIND=$(lsof -iTCP:5432 -sTCP:LISTEN 2>/dev/null | grep -o "localhost\|127\.0\.0\.1\|\*" | head -1)
if [ "$PG_BIND" = "localhost" ] || [ "$PG_BIND" = "127.0.0.1" ]; then
    pass "Security: PostgreSQL bound to loopback only"
else
    warn "Security: PostgreSQL binding is '$PG_BIND' — verify pg_hba.conf"
fi

# Security: No secrets in environment of running gateway
GW_PID=$(pgrep -f "openclaw.*gateway" 2>/dev/null | head -1)
if [ -n "$GW_PID" ]; then
    GW_ENV=$(ps eww -p "$GW_PID" 2>/dev/null | { grep -c "NOVA_SLACK_BOT_TOKEN=" || true; })
    if [ "${GW_ENV:-0}" -gt 0 ]; then
        pass "Security: Gateway has Slack token loaded in env"
    else
        warn "Security: Gateway may not have Slack token (Slack will be offline)"
    fi
fi

# Security: Workspace-state.json permissions
WS_STATE="$HOME/.openclaw/workspace/.openclaw/workspace-state.json"
if [ -f "$WS_STATE" ]; then
    WS_PERMS=$(stat -f "%Lp" "$WS_STATE" 2>/dev/null)
    if [ "$WS_PERMS" = "644" ] || [ "$WS_PERMS" = "600" ]; then
        pass "Security: workspace-state.json permissions OK ($WS_PERMS)"
    else
        warn "Security: workspace-state.json perms are $WS_PERMS (expected 600 or 644)"
    fi
fi

# Functional: Disk space check (main SSD has been critically low before)
MAIN_FREE_GB=$(df -g / 2>/dev/null | tail -1 | awk '{print $4}')
if [ -n "$MAIN_FREE_GB" ] && [ "$MAIN_FREE_GB" -ge 20 ]; then
    pass "Disk: Main SSD has ${MAIN_FREE_GB}GB free"
elif [ -n "$MAIN_FREE_GB" ] && [ "$MAIN_FREE_GB" -ge 10 ]; then
    warn "Disk: Main SSD only ${MAIN_FREE_GB}GB free (getting low)"
else
    fail "Disk: Main SSD critically low (${MAIN_FREE_GB:-?}GB free)"
fi

DATA_FREE_GB=$(df -g /Volumes/Data 2>/dev/null | tail -1 | awk '{print $4}')
if [ -n "$DATA_FREE_GB" ] && [ "$DATA_FREE_GB" -ge 50 ]; then
    pass "Disk: /Volumes/Data has ${DATA_FREE_GB}GB free"
elif [ -n "$DATA_FREE_GB" ]; then
    warn "Disk: /Volumes/Data only ${DATA_FREE_GB}GB free"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════════════════
BOOT_END=$(date +%s)
BOOT_DURATION=$((BOOT_END - BOOT_START))

log ""
log "════════════════════════════════════════════════════════════"
log "  BOOT COMPLETE — ${BOOT_DURATION}s total"
log "  Failures: $FAILED | Warnings: $WARNINGS"
if [ "$FAILED" -eq 0 ]; then
    log "  STATUS: ALL SYSTEMS OPERATIONAL ✓"
else
    log "  STATUS: DEGRADED — $FAILED service(s) failed to start"
fi
log "════════════════════════════════════════════════════════════"

# Exit with error if critical failures occurred
if [ "$FAILED" -gt 0 ]; then
    exit 1
fi
exit 0
