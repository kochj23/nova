#!/bin/bash
# install.sh — Set up Nova-NextGen Gateway and register as LaunchAgent
#
# Author: Jordan Koch

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$HOME/.nova_gateway/venv"
PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3.12}"
PLIST_SRC="$SCRIPT_DIR/com.nova.gateway.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.nova.gateway.plist"
LOG_DIR="$HOME/.nova_gateway"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Nova-NextGen Gateway Installer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 1. Create venv and install dependencies
echo "→ Creating Python virtual environment (python3.12)..."
"$PYTHON_BIN" -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo "→ Installing Python dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo "  ✓ Dependencies installed"

# 2. Create log dir
mkdir -p "$LOG_DIR"
echo "  ✓ Log directory: $LOG_DIR"

# 3. Make scripts executable
chmod +x "$SCRIPT_DIR/run.sh"
echo "  ✓ run.sh is executable"

# 4. Generate LaunchAgent plist with correct paths
UVICORN_BIN="$VENV_DIR/bin/uvicorn"

cat > "$PLIST_SRC" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.nova.gateway</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UVICORN_BIN</string>
        <string>nova_gateway.main:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>34750</string>
        <string>--log-level</string>
        <string>info</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$VENV_DIR/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>PYTHONPATH</key>
        <string>$SCRIPT_DIR</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/gateway.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/gateway.error.log</string>
    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
EOF
echo "  ✓ LaunchAgent plist generated"

# 5. Install LaunchAgent
if [ -f "$PLIST_DEST" ]; then
    launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi
cp "$PLIST_SRC" "$PLIST_DEST"
launchctl load "$PLIST_DEST"
echo "  ✓ LaunchAgent installed and started"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Installation complete!"
echo ""
echo "  Gateway URL:  http://localhost:34750"
echo "  Status:       http://localhost:34750/api/ai/status"
echo "  Logs:         $LOG_DIR/gateway.log"
echo ""
echo "  Commands:"
echo "    launchctl stop com.nova.gateway    # stop"
echo "    launchctl start com.nova.gateway   # start"
echo "    launchctl unload ~/Library/LaunchAgents/com.nova.gateway.plist  # disable"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
