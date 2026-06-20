#!/bin/bash
# nova_nas_backup.sh — Synology -> UNAS additive replication (NO deletions).
#   SOURCE (Synology): /volume1/nas        /volume1/external
#   DEST   (UNAS CIFS): /volume1/docker/nas /volume1/docker/external
# Additive-only (no --delete) until space becomes an issue. Reports full rsync
# stats (files, transferred, sizes, speed, errors, duration) into Nova's memory
# so she can report on the backup daily. Written by Jordan Koch (via Claude).

RSYNC=/usr/bin/rsync
LOG=/volume1/homes/kochj/nova_nas_backup.log
MEM_URL="http://192.168.1.6:18790/remember"
STAMP=$(date '+%Y-%m-%d %H:%M:%S')
START=$(date +%s)

# -rltD: recurse, links, times, devices (NO -pog: CIFS can't hold unix perms/owner).
# NO --delete (additive). Exclude recycle bins / Synology indexer / snapshots.
FLAGS=(-rltD --stats --human-readable --no-perms --no-owner --no-group
       --exclude='#recycle' --exclude='@eaDir' --exclude='#snapshot' --exclude='.DS_Store')

SUMMARY=""
OVERALL_RC=0

sync_share() {
  name="$1"; src="$2"; dst="$3"
  echo "[$STAMP] === $name: $src/ -> $dst/ ===" >> "$LOG"
  t0=$(date +%s)
  out=$("$RSYNC" "${FLAGS[@]}" "$src/" "$dst/" 2>&1); rc=$?
  dur=$(( $(date +%s) - t0 ))
  echo "$out" >> "$LOG"
  echo "[$STAMP] $name rc=$rc dur=${dur}s" >> "$LOG"
  nfiles=$(echo "$out"  | grep -i 'Number of files:' | head -1 | sed 's/.*: *//')
  nxfer=$(echo "$out"   | grep -iE 'files transferred:' | head -1 | sed 's/.*: *//')
  xsize=$(echo "$out"   | grep -i 'Total transferred file size:' | head -1 | sed 's/.*: *//')
  tsize=$(echo "$out"   | grep -i 'Total file size:' | head -1 | sed 's/.*: *//')
  speed=$(echo "$out"   | grep -iE '^sent .*bytes/sec' | head -1)
  nerr=$(echo "$out"    | grep -icE 'rsync error|Permission denied|cannot delete|failed')
  [ "$rc" -ne 0 ] && OVERALL_RC=$rc
  SUMMARY="${SUMMARY}[${name}: rc=${rc}, ${dur}s, files=${nfiles:-?}, transferred=${nxfer:-0} (${xsize:-0}), totalsize=${tsize:-?}, ${speed:-no transfer}, errors=${nerr}] "
}

sync_share nas      /volume1/nas      /volume1/docker/nas
sync_share external /volume1/external /volume1/docker/external

ELAPSED=$(( $(date +%s) - START ))
TEXT="Nova NAS backup (Synology->UNAS, additive, no deletions) ${STAMP}: completed in ${ELAPSED}s, overall rc=${OVERALL_RC}. ${SUMMARY}"
# sanitize for JSON (strip quotes/backslashes/newlines)
SAFE=$(printf '%s' "$TEXT" | tr -d '"\\\n\r' | sed "s/'/ /g")
JSON=$(printf '{"text":"%s","source":"operations","metadata":{"type":"nas_backup","rc":%s,"elapsed_s":%s,"date":"%s"}}' "$SAFE" "$OVERALL_RC" "$ELAPSED" "$STAMP")
curl -s -m 25 -X POST "$MEM_URL" -H "Content-Type: application/json" -d "$JSON" >> "$LOG" 2>&1
echo "" >> "$LOG"
echo "[$STAMP] reported to Nova memory (rc=$OVERALL_RC, ${ELAPSED}s)" >> "$LOG"
exit "$OVERALL_RC"
