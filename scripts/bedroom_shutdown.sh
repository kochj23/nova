#!/bin/sh
# NUT upsmon SHUTDOWNCMD for the Master Bedroom APC BGM1500 UPS.
#
# This host (.77 Jordans-Mac-mini) is BOTH the NUT host and one of the loads on
# this UPS, so it must halt LAST: once it goes down the driver tells the UPS to
# cut its outlets, which would strand any node that had not finished shutting
# down yet.
#
# Moved here from nova-core4 (.250) on 2026-10-21 when the UPS USB was replugged
# into the M4 mini.
#
# The restricted key can only run "sudo -n shutdown -h now" on each peer
# (from=192.168.1.77, restrict, forced command).
#
# Lives under /var/root/nut, NOT /Volumes/Data: that volume is mounted
# "noowners", so a root-executed script and a private key stored there would be
# writable/readable by any local user.
#
# NUT_SHUTDOWN_DRYRUN=1 prints the plan, in order, and changes nothing.
# PEERS / SETTLE_SECONDS can be overridden for tests.

DIR=/var/root/nut
KEY=$DIR/bedroom_shutdown_key
KH=$DIR/known_hosts
LOG=/var/log/nut/shutdown.log
SELF=jordans-mini.digitalnoise.net

: "${PEERS:=nova-core6.digitalnoise.net nova-core7.digitalnoise.net nova-core4.digitalnoise.net}"
: "${SETTLE_SECONDS:=60}"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >>"$LOG" 2>/dev/null; }

if [ "${NUT_SHUTDOWN_DRYRUN:-0}" = "1" ]; then
  for h in $PEERS; do echo "PEER $h"; done
  echo "SETTLE $SETTLE_SECONDS"
  echo "SELF $SELF"
  exit 0
fi

mkdir -p /var/log/nut 2>/dev/null
log "SHUTDOWNCMD fired; peers: $PEERS"

for h in $PEERS; do
  (
    ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=5 \
        -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
        -o UserKnownHostsFile="$KH" -o StrictHostKeyChecking=yes \
        kochj@"$h" >>"$LOG" 2>&1
    log "$h ssh rc=$?"
  ) &
done
wait

# Let the peers actually finish powering off before this box goes down and the
# UPS cuts the outlets.
sleep "$SETTLE_SECONDS"
log "peers done; halting $SELF last"
exec /sbin/shutdown -h now
