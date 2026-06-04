#!/bin/bash
# nova_syslog_deploy.sh — Deploy syslog forwarder to all Nova network devices.
#
# Deploys:
#   - macOS forwarder (LaunchDaemon) to Mac hosts
#   - rsyslog config to Linux/Pi hosts
#
# Usage:
#   ./nova_syslog_deploy.sh           # deploy to all
#   ./nova_syslog_deploy.sh macs      # only Macs
#   ./nova_syslog_deploy.sh linux     # only Linux/Pi
#   ./nova_syslog_deploy.sh 192.168.1.7  # single host
#
# Written by Jordan Koch.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYSLOG_HOST="192.168.1.6"
SYSLOG_PORT="1514"
FORWARDER_SCRIPT="$SCRIPT_DIR/nova_syslog_forwarder.sh"
FORWARDER_PLIST="$SCRIPT_DIR/net.digitalnoise.nova-syslog-forwarder.plist"

# ── Device inventory ──────────────────────────────────────────────────────────

MAC_HOSTS=(
    "kochj@192.168.1.7"
    "kochj@192.168.1.104"
)

LINUX_HOSTS=(
    "kochj@192.168.1.2"
    "kochj@192.168.1.8"
    "kochj@192.168.1.10"
)

# ── Helpers ───────────────────────────────────────────────────────────────────

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; }
info() { echo -e "${YELLOW}→${NC} $1"; }

deploy_mac() {
    local host="$1"
    local ip="${host#*@}"
    info "Deploying to Mac: $host"

    if ! ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$host" "echo ok" &>/dev/null; then
        fail "$ip — SSH connection failed (check key auth)"
        return 1
    fi

    scp -q "$FORWARDER_SCRIPT" "$host:/tmp/nova_syslog_forwarder.sh"
    scp -q "$FORWARDER_PLIST" "$host:/tmp/net.digitalnoise.nova-syslog-forwarder.plist"

    ssh "$host" bash <<'REMOTE'
        sudo cp /tmp/nova_syslog_forwarder.sh /usr/local/bin/
        sudo chmod +x /usr/local/bin/nova_syslog_forwarder.sh
        sudo cp /tmp/net.digitalnoise.nova-syslog-forwarder.plist /Library/LaunchDaemons/
        sudo chmod 644 /Library/LaunchDaemons/net.digitalnoise.nova-syslog-forwarder.plist
        sudo chown root:wheel /Library/LaunchDaemons/net.digitalnoise.nova-syslog-forwarder.plist

        # Stop if already running, then start
        sudo launchctl bootout system/net.digitalnoise.nova-syslog-forwarder 2>/dev/null || true
        sudo launchctl bootstrap system /Library/LaunchDaemons/net.digitalnoise.nova-syslog-forwarder.plist

        rm -f /tmp/nova_syslog_forwarder.sh /tmp/net.digitalnoise.nova-syslog-forwarder.plist
REMOTE

    if [ $? -eq 0 ]; then
        ok "$ip — Mac forwarder deployed and started"
    else
        fail "$ip — deployment failed"
        return 1
    fi
}

deploy_linux() {
    local host="$1"
    local ip="${host#*@}"
    info "Deploying to Linux: $host"

    if ! ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$host" "echo ok" &>/dev/null; then
        fail "$ip — SSH connection failed (check key auth)"
        return 1
    fi

    ssh "$host" bash <<REMOTE
        # Create rsyslog forwarding config
        echo '# Nova syslog forwarding — security-relevant events to Nova syslog server
*.warning @${SYSLOG_HOST}:${SYSLOG_PORT}
auth,authpriv.* @${SYSLOG_HOST}:${SYSLOG_PORT}
kern.* @${SYSLOG_HOST}:${SYSLOG_PORT}' | sudo tee /etc/rsyslog.d/60-nova.conf > /dev/null

        # Restart rsyslog
        if command -v systemctl &>/dev/null; then
            sudo systemctl restart rsyslog 2>/dev/null || sudo systemctl restart syslog 2>/dev/null || true
        else
            sudo service rsyslog restart 2>/dev/null || sudo /etc/init.d/rsyslog restart 2>/dev/null || true
        fi
REMOTE

    if [ $? -eq 0 ]; then
        ok "$ip — Linux rsyslog configured and restarted"
    else
        fail "$ip — deployment failed"
        return 1
    fi
}

# ── Main ──────────────────────────────────────────────────────────────────────

echo "═══════════════════════════════════════════════════"
echo " Nova Syslog Forwarder Deployment"
echo " Target: ${SYSLOG_HOST}:${SYSLOG_PORT}"
echo "═══════════════════════════════════════════════════"
echo ""

FILTER="${1:-all}"
SUCCESS=0
FAILED=0

if [[ "$FILTER" == "all" || "$FILTER" == "macs" ]]; then
    for host in "${MAC_HOSTS[@]}"; do
        if deploy_mac "$host"; then
            ((SUCCESS++))
        else
            ((FAILED++))
        fi
        echo ""
    done
fi

if [[ "$FILTER" == "all" || "$FILTER" == "linux" ]]; then
    for host in "${LINUX_HOSTS[@]}"; do
        if deploy_linux "$host"; then
            ((SUCCESS++))
        else
            ((FAILED++))
        fi
        echo ""
    done
fi

# Single host mode
if [[ "$FILTER" != "all" && "$FILTER" != "macs" && "$FILTER" != "linux" ]]; then
    # Guess type by trying ssh and checking OS
    host="kochj@${FILTER}"
    os=$(ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$host" "uname -s" 2>/dev/null || echo "unknown")
    if [[ "$os" == "Darwin" ]]; then
        deploy_mac "$host" && ((SUCCESS++)) || ((FAILED++))
    elif [[ "$os" == "Linux" ]]; then
        deploy_linux "$host" && ((SUCCESS++)) || ((FAILED++))
    else
        fail "$FILTER — could not determine OS (SSH failed?)"
        ((FAILED++))
    fi
fi

echo "═══════════════════════════════════════════════════"
echo " Results: ${SUCCESS} deployed, ${FAILED} failed"
echo "═══════════════════════════════════════════════════"
