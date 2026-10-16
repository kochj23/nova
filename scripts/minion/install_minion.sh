#!/bin/bash
# install_minion.sh — make THIS Mac a Nova minion (run on the minion; enroll_minion.sh on the Studio runs it for you).
#   install_minion.sh NODE HUB_URL TOKEN [--dedicated] [--hours 22-07] [--idle-min 10]
# Installs: ~/.nova-minion/{nova_minion_agent.py,config.json}, the token in the login Keychain (service nova-minion),
# LaunchAgent net.digitalnoise.nova-minion, the Nova Minion screensaver if NovaMinion.saver sits next to this script,
# and pulls the reader's model into Ollama. Ollama must listen on the LAN (OLLAMA_HOST=0.0.0.0) so Nova can reach it.
# Written by Jordan Koch (via Claude).
set -euo pipefail
NODE="${1:?node}"; HUB="${2:?hub url}"; TOKEN="${3:?token}"; shift 3
DEDICATED=false; HOURS=""; IDLE_MIN=10; MODEL="qwen3:30b-a3b"; MINFREE=0
while [ $# -gt 0 ]; do case "$1" in
  --dedicated) DEDICATED=true ;; --hours) HOURS="$2"; shift ;; --idle-min) IDLE_MIN="$2"; shift ;;
  --model) MODEL="$2"; shift ;; --min-free-gb) MINFREE="$2"; shift ;; *) echo "unknown option $1"; exit 2 ;; esac; shift; done
HERE="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/.nova-minion"
mkdir -p "$DIR"
cp "$HERE/nova_minion_agent.py" "$DIR/"
/usr/bin/python3 "$DIR/nova_minion_agent.py" --selftest

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
[ -n "$IP" ] || { echo "no LAN address on en0/en1"; exit 3; }
OLLAMA_URL="http://$IP:11434"
if ! curl -s -m 3 "$OLLAMA_URL/api/version" >/dev/null; then
  echo "Ollama is not reachable on $OLLAMA_URL — it must listen on the LAN:"
  echo "  launchctl setenv OLLAMA_HOST 0.0.0.0   (then restart Ollama)"; exit 3
fi
# Ollama may live anywhere (a custom LaunchDaemon path, the app, brew): talk to its API, not its binary
if ! curl -s -m 5 "$OLLAMA_URL/api/tags" | grep -q "\"$MODEL\""; then
  echo "pulling $MODEL into Ollama on $OLLAMA_URL ..."
  curl -s -m 3600 "$OLLAMA_URL/api/pull" -d "{\"name\": \"$MODEL\", \"stream\": false}" | grep -q success \
    || { echo "pull of $MODEL failed"; exit 3; }
fi

/usr/bin/python3 - "$DIR/config.json" "$NODE" "$HUB" "$OLLAMA_URL" "$IDLE_MIN" "$HOURS" "$DEDICATED" "$MODEL" "$MINFREE" <<'PY'
import json, sys
p, node, hub, url, idle, hours, ded, model, minfree = sys.argv[1:]
c = {"node": node, "hub": hub.rstrip("/"), "ollama_url": url, "idle_after_min": int(idle),
     "dedicated": ded == "true", "models": [model], "min_free_gb": float(minfree)}
if hours:
    c["hours"] = hours
json.dump(c, open(p, "w"), indent=2)
PY
# Keychain first; over SSH the login keychain is locked ("User interaction is not allowed"), so fall back to a
# 0600 file. The token can only report this Mac's state and read its own status.
if ! /usr/bin/security add-generic-password -U -s nova-minion -a "$NODE" -w "$TOKEN" >/dev/null 2>&1; then
  ( umask 077; printf '%s' "$TOKEN" > "$DIR/token" )
  echo "keychain locked (SSH session) — token stored in $DIR/token (0600)"
else
  rm -f "$DIR/token"
fi

PL="$HOME/Library/LaunchAgents/net.digitalnoise.nova-minion.plist"
sed "s|__HOME__|$HOME|g" "$HERE/net.digitalnoise.nova-minion.plist.template" > "$PL"
launchctl bootout "gui/$(id -u)/net.digitalnoise.nova-minion" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PL"

if [ -d "$HERE/NovaMinion.saver" ]; then
  mkdir -p "$HOME/Library/Screen Savers"
  /usr/bin/ditto "$HERE/NovaMinion.saver" "$HOME/Library/Screen Savers/NovaMinion.saver"
  echo "screensaver installed: choose 'NovaMinion' in System Settings > Screen Saver"
fi
/usr/bin/python3 "$DIR/nova_minion_agent.py" --once
echo "minion $NODE installed (dedicated=$DEDICATED hours=${HOURS:-any} idle=${IDLE_MIN}m) — log: $DIR/agent.log"
