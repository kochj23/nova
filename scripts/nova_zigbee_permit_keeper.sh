#!/bin/bash
# nova_zigbee_permit_keeper.sh [hours] — hold the Zigbee network open for onboarding.
# Zigbee caps each permit_join at 254s, so re-permit every 200s (no gap), log device
# joins, then CLOSE permit_join when the window ends (don't leave the network open).
# Usage: nohup nova_zigbee_permit_keeper.sh 6 > ... &
set -uo pipefail
HOURS=${1:-6}
MB=192.168.1.6
PUB=/opt/homebrew/bin/mosquitto_pub
SUB=/opt/homebrew/bin/mosquitto_sub
LOG="$HOME/.openclaw/logs/zigbee_permit_keeper.log"
END=$(( $(date +%s) + HOURS*3600 ))

echo "[$(date)] permit-keeper START — open ${HOURS}h (until $(date -r $END '+%H:%M'))" | tee -a "$LOG"
# log joins/interviews in the background
"$SUB" -h "$MB" -t 'zigbee2mqtt/bridge/event' >> "$LOG" 2>&1 &
SUBPID=$!
trap '"$PUB" -h "$MB" -t zigbee2mqtt/bridge/request/permit_join -m "{\"time\":0}"; kill $SUBPID 2>/dev/null; echo "[$(date)] permit-keeper STOPPED, network CLOSED" >> "$LOG"' EXIT

while [ "$(date +%s)" -lt "$END" ]; do
  "$PUB" -h "$MB" -t zigbee2mqtt/bridge/request/permit_join -m '{"time":254}'
  sleep 200
done
echo "[$(date)] ${HOURS}h elapsed — closing network" >> "$LOG"
