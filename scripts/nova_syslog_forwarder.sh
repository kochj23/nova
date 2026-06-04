#!/bin/bash
# nova_syslog_forwarder.sh — Forward macOS unified log to Nova syslog server.
#
# Streams security-relevant events (auth failures, kernel errors, firewall,
# process crashes, sudo usage) and forwards them as syslog UDP packets.
#
# Install as a LaunchDaemon (requires root) on any Mac:
#   sudo cp nova_syslog_forwarder.sh /usr/local/bin/
#   sudo cp net.digitalnoise.nova-syslog-forwarder.plist /Library/LaunchDaemons/
#   sudo launchctl bootstrap system /Library/LaunchDaemons/net.digitalnoise.nova-syslog-forwarder.plist
#
# Written by Jordan Koch.

set -euo pipefail

SYSLOG_HOST="${NOVA_SYSLOG_HOST:-192.168.1.6}"
SYSLOG_PORT="${NOVA_SYSLOG_PORT:-1514}"
HOSTNAME=$(scutil --get LocalHostName 2>/dev/null || hostname -s)

send_syslog() {
    local severity="$1"
    local app="$2"
    local msg="$3"
    # RFC 3164: <PRI>TIMESTAMP HOSTNAME APP: MSG
    # facility=1 (user) for most, facility=4 (auth) for auth events, facility=0 (kern) for kernel
    local facility=1
    case "$app" in
        sshd|sudo|login*|auth*|security*|opendirectoryd) facility=4 ;;
        kernel*) facility=0 ;;
    esac
    local pri=$(( facility * 8 + severity ))
    local ts
    ts=$(date "+%b %d %H:%M:%S")
    printf "<%d>%s %s %s: %s" "$pri" "$ts" "$HOSTNAME" "$app" "$msg" | \
        nc -u -w0 "$SYSLOG_HOST" "$SYSLOG_PORT" 2>/dev/null || true
}

# Stream the unified log with a predicate covering security-relevant events
/usr/bin/log stream --style compact --predicate '
    (process == "sshd" AND eventMessage CONTAINS "Failed")
    OR (process == "sshd" AND eventMessage CONTAINS "Invalid user")
    OR (process == "sudo" AND eventMessage CONTAINS[c] "command")
    OR (process == "kernel" AND messageType == error)
    OR (process == "kernel" AND eventMessage CONTAINS "sandbox")
    OR (sender == "kernel" AND eventMessage CONTAINS "deny")
    OR (process == "socketfilterfw" AND eventMessage CONTAINS "Block")
    OR (process == "socketfilterfw" AND eventMessage CONTAINS "Deny")
    OR (process == "loginwindow" AND eventMessage CONTAINS "fail")
    OR (process == "opendirectoryd" AND eventMessage CONTAINS "auth")
    OR (process == "com.apple.xpc.launchd" AND eventMessage CONTAINS "crash")
    OR (process == "ReportCrash")
    OR (messageType == fault)
    OR (messageType == error AND process == "kernel")
' 2>/dev/null | while IFS= read -r line; do
    # Skip the header line
    [[ "$line" == Filtering* ]] && continue
    [[ -z "$line" ]] && continue

    app="system"
    severity=4
    msg="$line"

    # Extract process name if present
    if [[ "$line" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}[[:space:]][0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]+[[:space:]]([a-zA-Z0-9._-]+)\[?[0-9]*\]?[[:space:]]*(.*) ]]; then
        app="${BASH_REMATCH[1]}"
        msg="${BASH_REMATCH[2]}"
    fi

    # Assign severity based on content
    if [[ "$msg" == *"fault"* ]] || [[ "$msg" == *"crash"* ]] || [[ "$msg" == *"panic"* ]]; then
        severity=2
    elif [[ "$msg" == *"Failed"* ]] || [[ "$msg" == *"denied"* ]] || [[ "$msg" == *"Block"* ]]; then
        severity=4
    elif [[ "$msg" == *"error"* ]] || [[ "$msg" == *"Error"* ]]; then
        severity=3
    fi

    send_syslog "$severity" "$app" "$msg"
done
