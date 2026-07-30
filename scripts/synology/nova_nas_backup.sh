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

# Single-instance lock — NEVER let runs stack. Overlapping rsyncs to the same
# CIFS dest corrupt --stats and trigger spurious IO errors (this bit us when the
# 3:30 cron fired on top of a still-running manual sync). Exit quietly if held.
exec 9>/volume1/homes/kochj/.nova_nas_backup.lock
if ! flock -n 9; then
  echo "[$STAMP] another backup run holds the lock — skipping this invocation" >> "$LOG"
  exit 0
fi

# -rlt: recurse, links, times (NO -D, NO -pog: CIFS can't hold devices/perms/owner).
# NO --delete (additive). Exclude recycle bins / Synology indexer / snapshots.
# Last exclude: a Photos-library thumbnail package whose filenames embed giant
# captions and exceed the UNAS filesystem's max name length (caused the rc=11
# rename failures). Skip it rather than fail the whole share every run.
FLAGS=(-rlt --stats --human-readable --no-perms --no-owner --no-group
       --exclude='#recycle' --exclude='@eaDir' --exclude='#snapshot' --exclude='.DS_Store'
       --exclude='GoogleDriveBackups/Pics/Pictures/.com-apple-bird-noname-*')

UNAS_CREDS=/root/.unascreds
# Self-heal: bring the UNAS CIFS mount up if it dropped (e.g. after a reboot) BEFORE
# we try to write. The mountpoint is chattr +i (immutable) when unmounted, so even if
# this fails, the sync_share guard + the immutable dir make a local-disk fill
# impossible — this just restores the mirror automatically instead of waiting for a
# manual remount. (2026-07-30, with the immutable-mountpoint + guard defense.)
ensure_mount() {
  local unc="$1" mp="$2"
  grep -q " ${mp} cifs " /proc/mounts && return 0
  mount -t cifs "$unc" "$mp" -o "credentials=${UNAS_CREDS},iocharset=utf8,vers=3.0" 2>/dev/null
  grep -q " ${mp} cifs " /proc/mounts
}
ensure_mount //192.168.1.69/nas      /volume1/docker/nas
ensure_mount //192.168.1.69/External /volume1/docker/external

SUMMARY=""
OVERALL_RC=0
# Machine-readable totals accumulated across shares (for telemetry.backup_runs).
TOT_FILES=0
TOT_BYTES=0
TOT_ERRORS=0

# Pull the raw integer from an rsync --stats line, stripping the human-readable
# grouping (commas) so we get a plain number. Run WITHOUT --human-readable on the
# transferred-bytes line: rsync emits the exact byte count there.
_num() { echo "$1" | sed 's/[^0-9]//g'; }

sync_share() {
  name="$1"; src="$2"; dst="$3"
  echo "[$STAMP] === $name: $src/ -> $dst/ ===" >> "$LOG"
  # GUARD (added 2026-07-30 after the 391GB local-orphan incident): $dst is the UNAS
  # CIFS mountpoint. If that mount is DOWN, rsyncing here silently writes to the
  # Synology's LOCAL disk instead of the UNAS — a silent mirror failure that also
  # burns local space. Refuse to run the share unless $dst is a live CIFS mount.
  if ! grep -q " ${dst} cifs " /proc/mounts; then
    echo "[$STAMP] $name ABORT: $dst is NOT a live CIFS mount to the UNAS — refusing to write to local disk" >> "$LOG"
    SUMMARY="${SUMMARY}[${name}: ABORTED — UNAS mount down, wrote nothing] "
    OVERALL_RC=32
    TOT_ERRORS=$(( TOT_ERRORS + 1 ))
    return
  fi
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
  # Machine-readable accumulation. nxfer/xsize are human-readable (e.g. "1.23M",
  # "4,096"); strip non-digits for an integer floor — good enough for graphing
  # "did the backup move bytes?" and trending. Files transferred is an exact int.
  m_files=$(_num "$nxfer")
  m_bytes=$(_num "$xsize")
  TOT_FILES=$(( TOT_FILES + ${m_files:-0} ))
  TOT_BYTES=$(( TOT_BYTES + ${m_bytes:-0} ))
  TOT_ERRORS=$(( TOT_ERRORS + ${nerr:-0} ))
  [ "$rc" -ne 0 ] && OVERALL_RC=$rc
  SUMMARY="${SUMMARY}[${name}: rc=${rc}, ${dur}s, files=${nfiles:-?}, transferred=${nxfer:-0} (${xsize:-0}), totalsize=${tsize:-?}, ${speed:-no transfer}, errors=${nerr}] "
}

sync_share nas      /volume1/nas      /volume1/docker/nas
sync_share external /volume1/external /volume1/docker/external

ELAPSED=$(( $(date +%s) - START ))
TEXT="Nova NAS backup (Synology->UNAS, additive, no deletions) ${STAMP}: completed in ${ELAPSED}s, overall rc=${OVERALL_RC}. ${SUMMARY}"
# ok = overall rc==0 AND at least one byte moved OR an explicit no-change run with
# no errors. Treat rc==0 with bytes==0 as NOT ok only when files were expected —
# but a clean additive run can legitimately move 0 bytes. So: ok = (rc==0 AND
# errors==0). The "moved 0 bytes" silent-failure case is caught by the Grafana
# alert (bytes=0 on the LATEST run), not flipped here, so a genuine no-op night
# still reads green while a stuck/empty backup is flagged at the dashboard.
OK=true
{ [ "$OVERALL_RC" -ne 0 ] || [ "$TOT_ERRORS" -ne 0 ]; } && OK=false
# sanitize for JSON (strip quotes/backslashes/newlines)
SAFE=$(printf '%s' "$TEXT" | tr -d '"\\\n\r' | sed "s/'/ /g")
JSON=$(printf '{"text":"%s","source":"operations","metadata":{"type":"nas_backup","job":"nas_backup","rc":%s,"elapsed_s":%s,"files":%s,"bytes":%s,"errors":%s,"ok":%s,"date":"%s"}}' "$SAFE" "$OVERALL_RC" "$ELAPSED" "${TOT_FILES:-0}" "${TOT_BYTES:-0}" "${TOT_ERRORS:-0}" "$OK" "$STAMP")
curl -s -m 25 -X POST "$MEM_URL" -H "Content-Type: application/json" -d "$JSON" >> "$LOG" 2>&1
echo "" >> "$LOG"
echo "[$STAMP] reported to Nova memory (rc=$OVERALL_RC, ${ELAPSED}s)" >> "$LOG"
exit "$OVERALL_RC"
