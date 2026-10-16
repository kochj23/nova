#!/bin/bash
# install_minion_linux.sh — make THIS Linux server a Nova minion (enroll_minion.sh on the Studio runs it for you).
#   install_minion_linux.sh NODE HUB_URL TOKEN [--dedicated] [--hours 01-07] [--min-free-gb 8] [--model qwen3:8b]
# Linux minions have no screensaver: idle = low 5-min load and nobody at the console (see nova_minion_agent.py).
# Installs ~/.nova-minion/{nova_minion_agent.py,config.json,token(0600)} and systemd unit nova-minion (User=$USER).
# Ollama must be installed and listening on the LAN (OLLAMA_HOST=0.0.0.0 drop-in on ollama.service).
# Written by Jordan Koch (via Claude).
set -euo pipefail
NODE="${1:?node}"; HUB="${2:?hub url}"; TOKEN="${3:?token}"; shift 3
DEDICATED=false; HOURS=""; MINFREE=8; MODEL="qwen3:8b"
while [ $# -gt 0 ]; do case "$1" in
  --dedicated) DEDICATED=true ;; --hours) HOURS="$2"; shift ;; --min-free-gb) MINFREE="$2"; shift ;;
  --model) MODEL="$2"; shift ;; --idle-min) shift ;; *) echo "unknown option $1"; exit 2 ;; esac; shift; done
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/.nova-minion"
mkdir -p "$DIR"
cp "$HERE/nova_minion_agent.py" "$DIR/"
PY="$(command -v python3)"
"$PY" "$DIR/nova_minion_agent.py" --selftest
IP="$(hostname -I | awk '{print $1}')"
OLLAMA_URL="http://$IP:11434"
curl -s -m 3 "$OLLAMA_URL/api/version" >/dev/null || { echo "Ollama not reachable on $OLLAMA_URL (needs OLLAMA_HOST=0.0.0.0)"; exit 3; }
# Ollama may live anywhere (a custom LaunchDaemon path, the app, brew): talk to its API, not its binary
if ! curl -s -m 5 "$OLLAMA_URL/api/tags" | grep -q "\"$MODEL\""; then
  echo "pulling $MODEL into Ollama on $OLLAMA_URL ..."
  curl -s -m 3600 "$OLLAMA_URL/api/pull" -d "{\"name\": \"$MODEL\", \"stream\": false}" | grep -q success \
    || { echo "pull of $MODEL failed"; exit 3; }
fi
"$PY" - "$DIR/config.json" "$NODE" "$HUB" "$OLLAMA_URL" "$HOURS" "$DEDICATED" "$MODEL" "$MINFREE" <<'PY'
import json, sys
p, node, hub, url, hours, ded, model, minfree = sys.argv[1:]
c = {"node": node, "hub": hub.rstrip("/"), "ollama_url": url, "dedicated": ded == "true", "models": [model],
     "min_free_gb": float(minfree), "max_load_per_core": 0.5}
if hours:
    c["hours"] = hours
json.dump(c, open(p, "w"), indent=2)
PY
( umask 077; printf '%s' "$TOKEN" > "$DIR/token" )
sudo -n tee /etc/systemd/system/nova-minion.service >/dev/null <<UNIT
[Unit]
Description=Nova minion agent (lends this server to Nova's work reader when idle)
After=network-online.target ollama.service
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
ExecStart=$PY $DIR/nova_minion_agent.py
Restart=always
RestartSec=10
Nice=10

[Install]
WantedBy=multi-user.target
UNIT
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now nova-minion
sudo -n systemctl restart nova-minion
"$PY" "$DIR/nova_minion_agent.py" --once
echo "minion $NODE installed on $(hostname) (dedicated=$DEDICATED hours=${HOURS:-any} min_free=${MINFREE}G model=$MODEL)"
