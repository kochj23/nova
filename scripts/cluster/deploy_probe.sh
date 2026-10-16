#!/bin/bash
# deploy_probe.sh — run on the Studio: put nova_llm_probe.py on a machine that runs LLMs (macOS or Linux).
#   deploy_probe.sh SSH_HOST NODE [--enroll]   (--enroll mints a probe-only token; minions reuse their own)
# The probe pushes to the hub, so the machine needs no inbound port. Written by Jordan Koch (via Claude).
set -euo pipefail
HOST="${1:?ssh host}"; NODE="${2:?node}"; ENROLL="${3:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STUDIO_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1)"
HUB="http://$STUDIO_IP:37590"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST")
"${SSH[@]}" 'mkdir -p ~/.nova-probe && chmod 700 ~/.nova-probe' </dev/null
scp -q -o BatchMode=yes "$HERE/nova_llm_probe.py" "$HOST:.nova-probe/nova_llm_probe.py"
if [ "$ENROLL" = "--enroll" ]; then
  /opt/homebrew/bin/python3 "$HERE/../nova_minion_hub.py" --enroll "$NODE" --probe \
    | "${SSH[@]}" 'umask 077; cat > ~/.nova-probe/token'
fi
"${SSH[@]}" "printf '{\"node\": \"%s\", \"hub\": \"%s\", \"ollama_ports\": [11434]}\n' '$NODE' '$HUB' > ~/.nova-probe/config.json" </dev/null
OS="$("${SSH[@]}" uname -s </dev/null)"
if [ "$OS" = "Linux" ]; then
  "${SSH[@]}" 'PY=$(command -v python3); python3 ~/.nova-probe/nova_llm_probe.py --selftest >/dev/null &&
    sudo -n tee /etc/systemd/system/nova-llm-probe.service >/dev/null <<UNIT
[Unit]
Description=Nova LLM probe (pushes this machine'"'"'s LLM activity to the hub)
After=network-online.target ollama.service
[Service]
User=$USER
ExecStart=$PY $HOME/.nova-probe/nova_llm_probe.py
Restart=always
RestartSec=10
Nice=10
[Install]
WantedBy=multi-user.target
UNIT
    sudo -n systemctl daemon-reload && sudo -n systemctl enable --now nova-llm-probe >/dev/null 2>&1 && sudo -n systemctl restart nova-llm-probe && systemctl is-active nova-llm-probe' </dev/null
else
  scp -q -o BatchMode=yes "$HERE/net.digitalnoise.nova-llm-probe.plist.template" "$HOST:.nova-probe/"
  "${SSH[@]}" '/usr/bin/python3 ~/.nova-probe/nova_llm_probe.py --selftest >/dev/null &&
    sed "s|__HOME__|$HOME|g" ~/.nova-probe/net.digitalnoise.nova-llm-probe.plist.template > ~/Library/LaunchAgents/net.digitalnoise.nova-llm-probe.plist &&
    (launchctl bootout gui/$(id -u)/net.digitalnoise.nova-llm-probe 2>/dev/null || true) &&
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/net.digitalnoise.nova-llm-probe.plist && echo running' </dev/null
fi
echo "probe $NODE on $HOST -> $HUB"
