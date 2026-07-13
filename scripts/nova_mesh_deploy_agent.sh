#!/bin/bash
# nova_mesh_deploy_agent.sh — Deploy Nova Mesh agent to a remote node
# Usage: nova_mesh_deploy_agent.sh <node_name> <node_ip> <peer_ip> <peer_node_name> [services_json]
# Written by Jordan Koch.

set -e

NODE_NAME="${1:?Usage: $0 <node_name> <node_ip> <peer_ip> <peer_node_name>}"
NODE_IP="${2:?}"
PEER_IP="${3:?}"
PEER_NODE="${4:?}"
SERVICES_JSON="${5:-[]}"

AGENT_SCRIPT="$HOME/.openclaw/scripts/nova_mesh_agent.py"
REMOTE_DIR="/opt/nova-config"

echo "=== Deploying Nova Mesh Agent to $NODE_NAME ($NODE_IP) ==="

# Detect OS
OS_FAMILY=$(ssh -o ConnectTimeout=10 "$NODE_IP" "uname -s" 2>/dev/null)
if [ "$OS_FAMILY" = "Darwin" ]; then
    PYTHON="/opt/homebrew/bin/python3"
    INIT_SYSTEM="launchd"
else
    PYTHON="/usr/bin/python3"
    INIT_SYSTEM="systemd"
fi

echo "[1/4] Creating remote directory and copying agent..."
ssh "$NODE_IP" "sudo mkdir -p $REMOTE_DIR && sudo chown \$(whoami) $REMOTE_DIR"
scp "$AGENT_SCRIPT" "$NODE_IP:$REMOTE_DIR/nova_mesh_agent.py"

echo "[2/4] Writing config..."
ssh "$NODE_IP" "cat > $REMOTE_DIR/mesh-agent.yaml << EOF
node_name: $NODE_NAME
pg_dsn: \"dbname=nova_ops user=nova_mesh host=192.168.1.6 password=nova_mesh_2026\"
heartbeat_interval: 15
port: 37470
peer: $PEER_IP
peer_node_name: $PEER_NODE
services: $SERVICES_JSON
EOF"

echo "[3/4] Installing service ($INIT_SYSTEM)..."
if [ "$INIT_SYSTEM" = "launchd" ]; then
    ssh "$NODE_IP" "cat > ~/Library/LaunchAgents/net.digitalnoise.nova-mesh-agent.plist << 'PLIST'
<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
    <key>Label</key><string>net.digitalnoise.nova-mesh-agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$REMOTE_DIR/nova_mesh_agent.py</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><dict><key>Crashed</key><true/></dict>
    <key>StandardOutPath</key><string>/tmp/nova-mesh-agent.log</string>
    <key>StandardErrorPath</key><string>/tmp/nova-mesh-agent.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>\$HOME</string>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>ThrottleInterval</key><integer>10</integer>
</dict>
</plist>
PLIST
launchctl load ~/Library/LaunchAgents/net.digitalnoise.nova-mesh-agent.plist 2>/dev/null || true
launchctl kickstart gui/\$(id -u)/net.digitalnoise.nova-mesh-agent 2>/dev/null || $PYTHON $REMOTE_DIR/nova_mesh_agent.py &"
else
    ssh "$NODE_IP" "sudo tee /etc/systemd/system/nova-mesh-agent.service > /dev/null << UNIT
[Unit]
Description=Nova Mesh Agent
After=network.target

[Service]
Type=simple
User=\$(whoami)
ExecStart=$PYTHON $REMOTE_DIR/nova_mesh_agent.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable nova-mesh-agent
sudo systemctl start nova-mesh-agent"
fi

echo "[4/4] Verifying..."
sleep 3
if curl -s --connect-timeout 5 "http://$NODE_IP:37470/health" | grep -q '"status"'; then
    echo "SUCCESS: Mesh agent on $NODE_NAME is responding"
else
    echo "WARNING: Agent may still be starting — check http://$NODE_IP:37470/health"
fi

echo ""
echo "=== Deployment complete for $NODE_NAME ==="
