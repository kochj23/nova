#!/bin/zsh
# nova-services.sh — Comprehensive start/stop/healthcheck for the Nova/OpenClaw service stack.
#
# Usage:
#   nova start       Start all services in dependency order with health verification
#   nova stop        Graceful shutdown in reverse dependency order
#   nova restart     Stop then start
#   nova status      Health check all services with UP/DOWN indicators
#
# Services (dependency order):
#   1. PostgreSQL (5432)   — brew services
#   2. Redis (6379)        — brew services
#   3. Ollama (11434)      — macOS app + serve
#   4. Gateway (18789)     — OpenClaw node process (needs Keychain secrets)
#   5. OpenWebUI (3000)    — Python web UI (~2 min startup)
#   6. TinyChat (8000)     — Python chatbot (~50s warmup)
#
# Exit codes:
#   0 = all healthy
#   1 = one or more services failed
#
# Log: /tmp/nova-services.log
#
# Written by Jordan Koch.

set -o pipefail

# ── Color output ──────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    CYAN='\033[0;36m'
    BOLD='\033[1m'
    DIM='\033[2m'
    RESET='\033[0m'
else
    RED='' GREEN='' YELLOW='' BLUE='' CYAN='' BOLD='' DIM='' RESET=''
fi

# ── Paths & Config ───────────────────────────────────────────────────────────
LOGFILE="/tmp/nova-services.log"
NODE_BIN="/opt/homebrew/opt/node/bin/node"
GATEWAY_ENTRY="/opt/homebrew/lib/node_modules/openclaw/dist/entry.js"
GATEWAY_PORT=18789
OLLAMA_BIN="/usr/local/bin/ollama"
REDIS_CLI="/opt/homebrew/bin/redis-cli"
PSQL_BIN="/opt/homebrew/bin/psql"
OPENWEBUI_BIN="/Volumes/Data/openwebui/venv/bin/open-webui"
OPENWEBUI_PORT=3000
TINYCHAT_DIR="/Volumes/Data/tinychat/chatbot"
TINYCHAT_PYTHON="/Volumes/Data/tinychat/venv/bin/python3"
TINYCHAT_PORT=8000
PG_DATABASE="nova_memories"

# Keychain account
KC_ACCOUNT="nova"
KC_SERVICES=(nova-openrouter-api-key nova-slack-bot-token nova-slack-app-token nova-gateway-auth-token)
KC_ENVVARS=(NOVA_OPENROUTER_API_KEY NOVA_SLACK_BOT_TOKEN NOVA_SLACK_APP_TOKEN NOVA_GATEWAY_AUTH_TOKEN)

# ── Logging ───────────────────────────────────────────────────────────────────
_log() {
    local ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$ts] $*" >> "$LOGFILE"
}

_info() {
    echo "${BLUE}[INFO]${RESET} $*"
    _log "INFO: $*"
}

_ok() {
    echo "${GREEN}[  OK]${RESET} $*"
    _log "OK: $*"
}

_warn() {
    echo "${YELLOW}[WARN]${RESET} $*"
    _log "WARN: $*"
}

_fail() {
    echo "${RED}[FAIL]${RESET} $*"
    _log "FAIL: $*"
}

_header() {
    echo ""
    echo "${BOLD}${CYAN}═══ $* ═══${RESET}"
    echo ""
    _log "=== $* ==="
}

# ── Utility ───────────────────────────────────────────────────────────────────

# Wait for a TCP port to accept connections. Returns 0 on success, 1 on timeout.
_wait_port() {
    local port=$1 timeout=${2:-30} label=${3:-"port $1"}
    local elapsed=0
    while (( elapsed < timeout )); do
        if nc -z 127.0.0.1 "$port" 2>/dev/null; then
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        printf "${DIM}.${RESET}"
    done
    echo ""
    return 1
}

# Wait for an HTTP endpoint to return 200. Returns 0 on success, 1 on timeout.
_wait_http() {
    local url=$1 timeout=${2:-30} label=${3:-"$1"}
    local elapsed=0
    while (( elapsed < timeout )); do
        if curl -sf -o /dev/null --max-time 5 "$url" 2>/dev/null; then
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
        printf "${DIM}.${RESET}"
    done
    echo ""
    return 1
}

# Load secrets from macOS Keychain with retry logic (Keychain may be locked after reboot).
_load_keychain_secrets() {
    local max_retries=5 attempt=0 all_loaded=false

    while (( attempt < max_retries )); do
        all_loaded=true
        for i in {1..${#KC_SERVICES[@]}}; do
            local svc="${KC_SERVICES[$i]}"
            local var="${KC_ENVVARS[$i]}"
            local val
            val="$(security find-generic-password -a "$KC_ACCOUNT" -s "$svc" -w 2>/dev/null)"
            if [[ -z "$val" ]]; then
                all_loaded=false
            else
                export "$var"="$val"
            fi
        done

        if $all_loaded; then
            return 0
        fi

        attempt=$((attempt + 1))
        if (( attempt < max_retries )); then
            _warn "Keychain not ready (attempt $attempt/$max_retries), retrying in 10s..."
            sleep 10
        fi
    done

    return 1
}

# Check if a process is listening on a port. Returns PID or empty.
_pid_on_port() {
    lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null | head -1
}

# ── Health Checks ─────────────────────────────────────────────────────────────

_health_postgresql() {
    "$PSQL_BIN" -h 127.0.0.1 -d "$PG_DATABASE" -c "SELECT 1" >/dev/null 2>&1
}

_health_redis() {
    local reply
    reply="$("$REDIS_CLI" ping 2>/dev/null)"
    [[ "$reply" == "PONG" ]]
}

_health_ollama() {
    curl -sf -o /dev/null --max-time 5 "http://127.0.0.1:11434/api/tags" 2>/dev/null
}

_health_gateway() {
    [[ -n "$(_pid_on_port $GATEWAY_PORT)" ]]
}

_health_openwebui() {
    curl -sf -o /dev/null --max-time 10 "http://127.0.0.1:$OPENWEBUI_PORT" 2>/dev/null
}

_health_tinychat() {
    [[ -n "$(_pid_on_port $TINYCHAT_PORT)" ]]
}

# ── Start Functions ───────────────────────────────────────────────────────────

_start_postgresql() {
    _info "Starting PostgreSQL..."
    if _health_postgresql; then
        _ok "PostgreSQL already running on port 5432"
        return 0
    fi
    brew services start postgresql@17 >/dev/null 2>&1
    _wait_port 5432 20 "PostgreSQL"
    sleep 1
    if _health_postgresql; then
        _ok "PostgreSQL started (port 5432)"
        return 0
    else
        _fail "PostgreSQL failed to start"
        return 1
    fi
}

_start_redis() {
    _info "Starting Redis..."
    if _health_redis; then
        _ok "Redis already running on port 6379"
        return 0
    fi
    launchctl kickstart gui/$(id -u)/net.digitalnoise.redis 2>/dev/null
    _wait_port 6379 15 "Redis"
    sleep 1
    if _health_redis; then
        _ok "Redis started (port 6379)"
        return 0
    else
        _fail "Redis failed to start"
        return 1
    fi
}

_start_ollama() {
    _info "Starting Ollama..."
    if _health_ollama; then
        _ok "Ollama already running on port 11434"
        return 0
    fi

    # Start Ollama.app (which spawns the serve process)
    open -a Ollama 2>/dev/null

    # Wait for the API to become responsive
    _wait_http "http://127.0.0.1:11434/api/tags" 45 "Ollama API"

    if _health_ollama; then
        _ok "Ollama started (port 11434)"
        return 0
    else
        _fail "Ollama failed to start"
        return 1
    fi
}

_start_gateway() {
    _info "Starting OpenClaw Gateway..."
    if _health_gateway; then
        _ok "Gateway already running on port $GATEWAY_PORT"
        return 0
    fi

    # Load Keychain secrets
    _info "Loading secrets from macOS Keychain..."
    if _load_keychain_secrets; then
        _ok "All 4 Keychain secrets loaded"
    else
        _fail "Could not load all Keychain secrets after 5 retries"
        # List which are missing
        for i in {1..${#KC_ENVVARS[@]}}; do
            local var="${KC_ENVVARS[$i]}"
            if [[ -z "${(P)var}" ]]; then
                _warn "  Missing: $var"
            fi
        done
        return 1
    fi

    # Start gateway in background
    nohup "$NODE_BIN" "$GATEWAY_ENTRY" gateway --port "$GATEWAY_PORT" \
        >> /tmp/nova-gateway.log 2>&1 &
    local gw_pid=$!
    disown $gw_pid 2>/dev/null

    _wait_port "$GATEWAY_PORT" 30 "Gateway"

    if _health_gateway; then
        _ok "Gateway started (port $GATEWAY_PORT, PID $gw_pid)"
        return 0
    else
        _fail "Gateway failed to start (check /tmp/nova-gateway.log)"
        return 1
    fi
}

_start_openwebui() {
    _info "Starting OpenWebUI (this takes ~2 minutes)..."
    if _health_openwebui; then
        _ok "OpenWebUI already running on port $OPENWEBUI_PORT"
        return 0
    fi

    export DATA_DIR="/Volumes/Data/openwebui/data"
    export OLLAMA_BASE_URL="http://127.0.0.1:11434"
    export WEBUI_AUTH="false"
    export HOME="$HOME"

    nohup "$OPENWEBUI_BIN" serve --host 127.0.0.1 --port "$OPENWEBUI_PORT" \
        >> /tmp/nova-openwebui.log 2>&1 &
    local owui_pid=$!
    disown $owui_pid 2>/dev/null

    # OpenWebUI loads PyTorch + embedding model on startup — give it up to 180s
    _wait_http "http://127.0.0.1:$OPENWEBUI_PORT" 180 "OpenWebUI"

    if _health_openwebui; then
        _ok "OpenWebUI started (port $OPENWEBUI_PORT, PID $owui_pid)"
        return 0
    else
        _fail "OpenWebUI failed to start within 3 minutes (check /tmp/nova-openwebui.log)"
        return 1
    fi
}

_start_tinychat() {
    _info "Starting TinyChat (~50s warmup)..."
    if _health_tinychat; then
        _ok "TinyChat already running on port $TINYCHAT_PORT"
        return 0
    fi

    export PORT="$TINYCHAT_PORT"
    export OPENAI_API_BASE="http://127.0.0.1:11434/v1"
    export OPENAI_API_KEY="ollama"
    export LLM_MODEL="deepseek-r1:8b"
    export HOME="$HOME"

    (
        cd "$TINYCHAT_DIR"
        nohup "$TINYCHAT_PYTHON" run.py >> /tmp/nova-tinychat.log 2>&1 &
        disown $! 2>/dev/null
    )

    _wait_port "$TINYCHAT_PORT" 90 "TinyChat"

    if _health_tinychat; then
        _ok "TinyChat started (port $TINYCHAT_PORT)"
        return 0
    else
        _fail "TinyChat failed to start within 90s (check /tmp/nova-tinychat.log)"
        return 1
    fi
}

# ── Stop Functions ────────────────────────────────────────────────────────────

_stop_tinychat() {
    _info "Stopping TinyChat..."
    local pid
    pid="$(_pid_on_port $TINYCHAT_PORT)"
    if [[ -z "$pid" ]]; then
        _ok "TinyChat not running"
        return 0
    fi
    kill "$pid" 2>/dev/null
    sleep 2
    if [[ -n "$(_pid_on_port $TINYCHAT_PORT)" ]]; then
        kill -9 "$pid" 2>/dev/null
        sleep 1
    fi
    _ok "TinyChat stopped (was PID $pid)"
}

_stop_openwebui() {
    _info "Stopping OpenWebUI..."
    local pid
    pid="$(_pid_on_port $OPENWEBUI_PORT)"
    if [[ -z "$pid" ]]; then
        _ok "OpenWebUI not running"
        return 0
    fi
    kill "$pid" 2>/dev/null
    sleep 3
    if [[ -n "$(_pid_on_port $OPENWEBUI_PORT)" ]]; then
        kill -9 "$pid" 2>/dev/null
        sleep 1
    fi
    _ok "OpenWebUI stopped (was PID $pid)"
}

_stop_gateway() {
    _info "Stopping OpenClaw Gateway..."
    local pid
    pid="$(_pid_on_port $GATEWAY_PORT)"
    if [[ -z "$pid" ]]; then
        _ok "Gateway not running"
        return 0
    fi
    kill "$pid" 2>/dev/null
    sleep 2
    if [[ -n "$(_pid_on_port $GATEWAY_PORT)" ]]; then
        kill -9 "$pid" 2>/dev/null
        sleep 1
    fi
    _ok "Gateway stopped (was PID $pid)"
}

_stop_ollama() {
    _info "Stopping Ollama..."
    if ! _health_ollama && [[ -z "$(_pid_on_port 11434)" ]]; then
        _ok "Ollama not running"
        return 0
    fi
    # Quit the Ollama app gracefully
    osascript -e 'tell application "Ollama" to quit' 2>/dev/null
    sleep 3
    # If still running, force kill
    local pid
    pid="$(_pid_on_port 11434)"
    if [[ -n "$pid" ]]; then
        kill "$pid" 2>/dev/null
        sleep 2
    fi
    pid="$(_pid_on_port 11434)"
    if [[ -n "$pid" ]]; then
        kill -9 "$pid" 2>/dev/null
    fi
    _ok "Ollama stopped"
}

_stop_redis() {
    _info "Stopping Redis..."
    if ! _health_redis; then
        _ok "Redis not running"
        return 0
    fi
    "$REDIS_CLI" shutdown nosave 2>/dev/null
    sleep 2
    _ok "Redis stopped"
}

_stop_postgresql() {
    _info "Stopping PostgreSQL..."
    if ! _health_postgresql && [[ -z "$(_pid_on_port 5432)" ]]; then
        _ok "PostgreSQL not running"
        return 0
    fi
    brew services stop postgresql@17 >/dev/null 2>&1
    sleep 2
    _ok "PostgreSQL stopped"
}

# ── Commands ──────────────────────────────────────────────────────────────────

cmd_start() {
    _header "Starting Nova Service Stack"
    _log "START command issued"

    local failures=0

    _start_postgresql  || ((failures++))
    _start_redis       || ((failures++))
    _start_ollama      || ((failures++))
    _start_gateway     || ((failures++))
    _start_openwebui   || ((failures++))
    _start_tinychat    || ((failures++))

    echo ""
    if (( failures == 0 )); then
        _ok "${BOLD}All 6 services started successfully${RESET}"
        _log "All services started OK"
        return 0
    else
        _fail "${BOLD}$failures service(s) failed to start${RESET}"
        _log "$failures services failed"
        return 1
    fi
}

cmd_stop() {
    _header "Stopping Nova Service Stack"
    _log "STOP command issued"

    # Reverse dependency order
    _stop_tinychat
    _stop_openwebui
    _stop_gateway
    _stop_ollama
    _stop_redis
    _stop_postgresql

    echo ""
    _ok "${BOLD}All services stopped${RESET}"
    _log "All services stopped"
    return 0
}

cmd_restart() {
    _header "Restarting Nova Service Stack"
    _log "RESTART command issued"
    cmd_stop
    echo ""
    sleep 2
    cmd_start
}

cmd_status() {
    _header "Nova Service Stack Status"
    _log "STATUS command issued"

    local total=6 up=0

    # PostgreSQL
    printf "  %-20s " "PostgreSQL (5432)"
    if _health_postgresql; then
        echo "${GREEN}UP${RESET}  ${DIM}— SELECT 1 OK on nova_memories${RESET}"
        ((up++))
    else
        local pg_pid="$(_pid_on_port 5432)"
        if [[ -n "$pg_pid" ]]; then
            echo "${YELLOW}DEGRADED${RESET}  ${DIM}— port open but query failed${RESET}"
        else
            echo "${RED}DOWN${RESET}"
        fi
    fi

    # Redis
    printf "  %-20s " "Redis (6379)"
    if _health_redis; then
        echo "${GREEN}UP${RESET}  ${DIM}— PONG${RESET}"
        ((up++))
    else
        local rd_pid="$(_pid_on_port 6379)"
        if [[ -n "$rd_pid" ]]; then
            echo "${YELLOW}DEGRADED${RESET}  ${DIM}— port open but PING failed${RESET}"
        else
            echo "${RED}DOWN${RESET}"
        fi
    fi

    # Ollama
    printf "  %-20s " "Ollama (11434)"
    if _health_ollama; then
        # Count loaded models
        local model_count
        model_count="$(curl -sf http://127.0.0.1:11434/api/tags 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("models",[])))' 2>/dev/null)"
        echo "${GREEN}UP${RESET}  ${DIM}— ${model_count:-?} models available${RESET}"
        ((up++))
    else
        echo "${RED}DOWN${RESET}"
    fi

    # Gateway
    printf "  %-20s " "Gateway (18789)"
    if _health_gateway; then
        local gw_pid="$(_pid_on_port $GATEWAY_PORT)"
        echo "${GREEN}UP${RESET}  ${DIM}— PID $gw_pid${RESET}"
        ((up++))
    else
        echo "${RED}DOWN${RESET}"
    fi

    # OpenWebUI
    printf "  %-20s " "OpenWebUI (3000)"
    if _health_openwebui; then
        echo "${GREEN}UP${RESET}  ${DIM}— HTTP 200${RESET}"
        ((up++))
    else
        local ow_pid="$(_pid_on_port $OPENWEBUI_PORT)"
        if [[ -n "$ow_pid" ]]; then
            echo "${YELLOW}STARTING${RESET}  ${DIM}— process running (PID $ow_pid), not yet responding${RESET}"
        else
            echo "${RED}DOWN${RESET}"
        fi
    fi

    # TinyChat
    printf "  %-20s " "TinyChat (8000)"
    if _health_tinychat; then
        echo "${GREEN}UP${RESET}  ${DIM}— port listening${RESET}"
        ((up++))
    else
        echo "${RED}DOWN${RESET}"
    fi

    echo ""
    if (( up == total )); then
        echo "  ${GREEN}${BOLD}$up/$total services healthy${RESET}"
        _log "STATUS: $up/$total healthy"
        return 0
    else
        echo "  ${YELLOW}${BOLD}$up/$total services healthy${RESET}"
        _log "STATUS: $up/$total healthy"
        return 1
    fi
}

# ── Main ──────────────────────────────────────────────────────────────────────

_usage() {
    echo "${BOLD}Usage:${RESET} nova <command>"
    echo ""
    echo "${BOLD}Commands:${RESET}"
    echo "  ${GREEN}start${RESET}     Start all services in dependency order"
    echo "  ${RED}stop${RESET}      Graceful shutdown in reverse order"
    echo "  ${YELLOW}restart${RESET}   Stop then start all services"
    echo "  ${BLUE}status${RESET}    Health check with UP/DOWN indicators"
    echo ""
    echo "${DIM}Log: $LOGFILE${RESET}"
}

case "${1:-}" in
    start)   cmd_start   ;;
    stop)    cmd_stop    ;;
    restart) cmd_restart ;;
    status)  cmd_status  ;;
    -h|--help|help)
        _usage
        exit 0
        ;;
    "")
        _usage
        exit 0
        ;;
    *)
        _fail "Unknown command: $1"
        _usage
        exit 1
        ;;
esac
