#!/bin/zsh
# homebridge_start.sh — Start Homebridge for ADT+ → HomeKit bridge.
# Injects Keychain password into config at startup. Written by Jordan Koch.

export HOME="${HOME:-/Users/$(whoami)}"
export PATH="/opt/homebrew/bin:$PATH"

# Pull password from Keychain and inject into config
KEYCHAIN_ACCT=$(security find-generic-password -s "homebridge-alarmdotcom" 2>/dev/null | grep "acct" | sed 's/.*"\(.*\)"/\1/')
ADT_PASS=$(security find-generic-password -a "$KEYCHAIN_ACCT" -s "homebridge-alarmdotcom" -w 2>/dev/null)
if [ -n "$ADT_PASS" ]; then
    /opt/homebrew/bin/python3 -c "
import json, sys
f = '$HOME/homebridge/config.json'
d = json.load(open(f))
for p in d.get('platforms', []):
    if p.get('platform') == 'Alarmdotcom':
        p['password'] = sys.argv[1]
json.dump(d, open(f, 'w'), indent=4)
" "$ADT_PASS"
fi

exec /opt/homebrew/bin/homebridge -U $HOME/homebridge
