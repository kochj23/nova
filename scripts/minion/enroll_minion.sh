#!/bin/bash
# enroll_minion.sh — run on the Studio: enroll a Mac as a Nova minion and install it over SSH.
#   enroll_minion.sh SSH_HOST NODE [--dedicated] [--hours 22-07] [--idle-min 10]
# Mints a token (only its hash stays in minion_nodes), copies the agent/installer/plist (and the screensaver from
# the binaries export when present) to the Mac, and runs install_minion.sh there.
# Written by Jordan Koch (via Claude).
set -euo pipefail
HOST="${1:?ssh host}"; NODE="${2:?node name}"; shift 2
HERE="$(cd "$(dirname "$0")" && pwd)"
STUDIO_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1)"
HUB="http://$STUDIO_IP:37590"
DED=""; for a in "$@"; do [ "$a" = "--dedicated" ] && DED="--dedicated"; done
TOKEN="$(/opt/homebrew/bin/python3 "$HERE/../nova_minion_hub.py" --enroll "$NODE" $DED)"
STAGE="$(mktemp -d)"
cp "$HERE/nova_minion_agent.py" "$HERE/install_minion.sh" "$HERE/net.digitalnoise.nova-minion.plist.template" "$STAGE/"
SAVER="/Volumes/Data/xcode/binaries/NovaMinion.saver"
[ -d "$SAVER" ] && /usr/bin/ditto "$SAVER" "$STAGE/NovaMinion.saver"
ssh -o BatchMode=yes "$HOST" 'mkdir -p ~/.nova-minion/install'
scp -q -r -o BatchMode=yes "$STAGE"/* "$HOST:.nova-minion/install/"
ssh -o BatchMode=yes "$HOST" "bash ~/.nova-minion/install/install_minion.sh '$NODE' '$HUB' '$TOKEN' $*" </dev/null
echo "enrolled $NODE on $HOST (hub $HUB)"
