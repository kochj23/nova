#!/bin/bash
# nova_la_to_ld.sh LABEL [PORT] — move one of .6's user LaunchAgents to a root-owned LaunchDaemon
# (still running as kochj) so it survives a GUI-session death like 2026-10-03 07:55. Uses Nova's
# plist_convert.py, keeps the logs where they were, verifies the service came back (port or pid),
# and rolls back to the agent if it did not. Idempotent. The retired agent plist is parked in
# ~/Library/LaunchAgents/disabled-20261003/ so it never loads at login again.
set -u
LABEL="$1"; PORT="${2:-}"
A="$HOME/Library/LaunchAgents/$LABEL.plist"
[ -f "$A" ] || A=$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -m1 -E "path = " | sed "s/.*path = //")
[ -n "$A" ] && [ -f "$A" ] || { echo "$LABEL: no agent plist found (already converted?)"; exit 0; }
CONV=/Volumes/nas/nova-fs/artifacts/spof-reduction-2026-10-03/launchd-generator/plist_convert.py
PARK="$HOME/Library/LaunchAgents/disabled-20261003"; mkdir -p "$PARK"
TMP=$(mktemp -d); python3 "$CONV" "$A" "$TMP/d.plist" >/dev/null || { echo "$LABEL: convert failed"; exit 1; }
NEW=$(plutil -extract Label raw "$TMP/d.plist")
for k in StandardOutPath StandardErrorPath; do v=$(plutil -extract $k raw "$A" 2>/dev/null); [ -n "$v" ] && plutil -replace $k -string "$v" "$TMP/d.plist"; done
D="/Library/LaunchDaemons/$NEW.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null; sleep 1
mv "$A" "$PARK/"
sudo -n install -m644 -o root -g wheel "$TMP/d.plist" "$D" && sudo -n launchctl bootstrap system "$D"
ok=0; for i in $(seq 1 90); do sleep 1
  if [ -n "$PORT" ]; then nc -z -w1 127.0.0.1 "$PORT" >/dev/null 2>&1 && { ok=1; break; }
  else sudo -n launchctl print "system/$NEW" 2>/dev/null | grep -qE "pid = [0-9]+" && { ok=1; break; }; fi; done
if [ $ok = 1 ]; then echo "$LABEL -> $NEW OK (daemon, $( [ -n "$PORT" ] && echo port $PORT open || echo running ))"
else echo "$LABEL -> $NEW FAILED — rolling back"; sudo -n launchctl print "system/$NEW" 2>/dev/null | grep -E "state|last exit" | head -2; E=$(plutil -extract StandardErrorPath raw "$TMP/d.plist" 2>/dev/null); [ -n "$E" ] && tail -n 5 "$E" | cut -c1-160; sudo -n launchctl bootout "system/$NEW" 2>/dev/null; sudo -n rm -f "$D"; mv "$PARK/$LABEL.plist" "$A"; launchctl bootstrap "gui/$(id -u)" "$A"; exit 1; fi
rm -f "$TMP/d.plist"; rmdir "$TMP"
