#!/bin/bash
# nova_gateway_watchdog.sh — Restart OpenClaw gateway if workspace-state.json EPERM
# is detected in the last 3 minutes (indicating Slack inbound flush is broken).
# Written by Jordan Koch.

LOG="/tmp/openclaw/openclaw-$(date +%Y-%m-%d).log"
WINDOW=180  # seconds to look back

if [ ! -f "$LOG" ]; then
    exit 0
fi

# Check for EPERM on workspace-state.json in the last WINDOW seconds
CUTOFF=$(date -v -${WINDOW}S +%s)
FOUND=$(python3 -c "
import json, sys, os, time

log = '$LOG'
cutoff = $CUTOFF
found = False
try:
    with open(log) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                msg = str(d.get('0','')) + str(d.get('1',''))
                ts = d.get('_meta',{}).get('date','')
                if 'workspace-state.json' in msg and 'EPERM' in msg and ts:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(ts.replace('Z','+00:00'))
                    if int(dt.timestamp()) >= cutoff:
                        found = True
                        break
            except:
                pass
except:
    pass
print('1' if found else '0')
" 2>/dev/null)

if [ "$FOUND" = "1" ]; then
    echo "[watchdog $(date)] EPERM detected on workspace-state.json — restarting gateway" >> $HOME/.openclaw/logs/nova_watchdog.log
    openclaw gateway restart >> $HOME/.openclaw/logs/nova_watchdog.log 2>&1
fi
