#!/bin/zsh
# nova_subagent_ctl.sh — Safe subagent lifecycle control for Nova.
#
# SCOPED: Only operates on com.nova.agent-* launchd labels.
# Nova can call this via exec approval to start/stop/restart/status subagents.
#
# Usage:
#   nova_subagent_ctl.sh status              — List all subagent states
#   nova_subagent_ctl.sh start <name>        — Start a subagent (e.g., "analyst")
#   nova_subagent_ctl.sh stop <name>         — Stop a subagent
#   nova_subagent_ctl.sh restart <name>      — Stop + start a subagent
#   nova_subagent_ctl.sh health              — Check Redis heartbeats for all agents
#
# Written by Jordan Koch.

set -uo pipefail

LABEL_PREFIX="com.nova.agent-"
PLIST_DIR="$HOME/Library/LaunchAgents"

_validate_name() {
    local name="$1"
    # Only allow alphanumeric + hyphen — no path traversal, no arbitrary labels
    if [[ ! "$name" =~ ^[a-z][a-z0-9-]{0,30}$ ]]; then
        echo "ERROR: Invalid agent name: $name" >&2
        exit 1
    fi
    local plist="$PLIST_DIR/${LABEL_PREFIX}${name}.plist"
    if [[ ! -f "$plist" ]]; then
        echo "ERROR: No plist found for agent '$name' at $plist" >&2
        exit 1
    fi
}

case "${1:-help}" in
    status)
        echo "=== Nova Subagents ==="
        launchctl list 2>/dev/null | grep "$LABEL_PREFIX" | while read pid exitcode label; do
            name="${label#$LABEL_PREFIX}"
            if [[ "$pid" == "-" ]]; then
                echo "  $name: stopped (exit: $exitcode)"
            else
                echo "  $name: running (pid: $pid)"
            fi
        done
        # Also check Redis heartbeats
        echo ""
        echo "=== Redis Heartbeats ==="
        for key in $(redis-cli --no-auth-warning KEYS "nova:agent:*:status" 2>/dev/null); do
            name=$(echo "$key" | sed 's/nova:agent://;s/:status//')
            val=$(redis-cli --no-auth-warning GET "$key" 2>/dev/null)
            echo "  $name: $val"
        done
        ;;

    start)
        name="${2:?Usage: $0 start <agent-name>}"
        _validate_name "$name"
        launchctl start "${LABEL_PREFIX}${name}" 2>&1
        echo "Started ${LABEL_PREFIX}${name}"
        ;;

    stop)
        name="${2:?Usage: $0 stop <agent-name>}"
        _validate_name "$name"
        launchctl stop "${LABEL_PREFIX}${name}" 2>&1
        echo "Stopped ${LABEL_PREFIX}${name}"
        ;;

    restart)
        name="${2:?Usage: $0 restart <agent-name>}"
        _validate_name "$name"
        launchctl stop "${LABEL_PREFIX}${name}" 2>/dev/null || true
        sleep 2
        launchctl start "${LABEL_PREFIX}${name}" 2>&1
        echo "Restarted ${LABEL_PREFIX}${name}"
        ;;

    health)
        echo "=== Subagent Health ==="
        redis-cli --no-auth-warning KEYS "nova:agent:*:meta" 2>/dev/null | while read key; do
            [[ -z "$key" ]] && continue
            name=$(echo "$key" | sed 's/nova:agent://;s/:meta//')
            tasks=$(redis-cli --no-auth-warning HGET "$key" tasks_completed 2>/dev/null || echo "0")
            uptime=$(redis-cli --no-auth-warning HGET "$key" uptime_s 2>/dev/null || echo "0")
            model=$(redis-cli --no-auth-warning HGET "$key" model 2>/dev/null || echo "?")
            error=$(redis-cli --no-auth-warning HGET "$key" last_error 2>/dev/null || echo "")
            agent_status=$(redis-cli --no-auth-warning GET "nova:agent:${name}:status" 2>/dev/null || echo "unknown")
            echo "  $name:"
            echo "    status: ${agent_status:-unknown}"
            echo "    model: ${model:-?}"
            echo "    tasks: ${tasks:-0}"
            echo "    uptime: ${uptime:-0}s"
            if [[ -n "${error:-}" ]]; then
                echo "    last_error: $error"
            fi
        done
        exit 0
        ;;

    *)
        echo "Usage: $0 {status|start|stop|restart|health} [agent-name]"
        echo ""
        echo "Agents: analyst, coder, lookout, librarian, gardener, sentinel, briefer"
        exit 1
        ;;
esac
