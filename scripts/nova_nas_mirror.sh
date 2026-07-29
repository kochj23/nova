#!/bin/sh
# nova_nas_mirror.sh — keep the UNAS a faithful replica of the Synology, nightly.
#
# WHY: on 2026-07-28 the two NAS boxes had drifted badly. The Synology held 3,568 files the UNAS
# had never seen (including 68GB of Postgres dumps — the nightly sync had been dead since 07-17),
# while the UNAS held 725,111 files the Synology did not, 5.45TB of which was a media library
# stranded under "Unconfirmed 1598815306.move" by an interrupted move. Neither box could have
# stood in for the other. Jordan's requirement: SYNOLOGY IS MASTER, UNAS is the standby, no more
# than 24 hours of drift, both the `nas` and `external` shares.
#
# TWO PHASES, in this order, and the order matters:
#   1. RESCUE  UNAS -> Synology, --ignore-existing. If we ever failed over and ran on the UNAS,
#              writes landed there. Phase 2 would destroy them. So they come home FIRST.
#   2. MIRROR  Synology -> UNAS, --delete. Now that master is a superset, make the replica match.
#
# THE GUARD THAT MATTERS: --delete against an empty or unmounted source erases the replica. That
# is the single way this script could destroy the thing it exists to protect. So phase 2 refuses
# to run unless the source still holds at least MIN_FILES entries — a mount that dropped looks
# exactly like "the user deleted everything", and only a floor can tell them apart.
#
# Runs ON the Synology (it is master and can reach the UNAS over ssh).

set -u
UNAS_HOST="root@192.168.1.69"
UNAS_ROOT="/volume/b37f2e84-517c-4a4f-92f0-4d642527ba17/.srv/.unifi-drive"
SSH="ssh -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=15"
LOG="/volume1/nas/backups/nas_mirror.log"
MIN_FILES=1000                 # refuse to mirror-with-delete from a source this small
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="--dry-run"

# share_name  synology_path  unas_subdir
SHARES="nas:/volume1/nas:nas external:/volume1/external:External"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

RC=0
log "=== nas mirror start ${DRY:+(DRY RUN)} ==="

for entry in $SHARES; do
    name=$(echo "$entry" | cut -d: -f1)
    src=$(echo "$entry" | cut -d: -f2)
    sub=$(echo "$entry" | cut -d: -f3)
    dst="$UNAS_ROOT/$sub/.data"

    if [ ! -d "$src" ]; then
        log "SKIP $name — source $src is not a directory (unmounted?)"; RC=1; continue
    fi

    # ---- phase 1: rescue anything that only exists on the replica ----
    log "[$name] phase 1 RESCUE  unas -> synology (never deletes)"
    rsync -rlt $DRY --ignore-existing --partial \
        --exclude='@eaDir/' --exclude='#recycle/' --exclude='.DS_Store' \
        --exclude='Unconfirmed *.move/' \
        -e "$SSH" "$UNAS_HOST:$dst/" "$src/" >>"$LOG" 2>&1
    r1=$?
    # 24 = "some files vanished during transfer", normal on a live filesystem, not a failure.
    [ $r1 -ne 0 ] && [ $r1 -ne 24 ] && { log "[$name] phase 1 FAILED rc=$r1 — skipping phase 2"; RC=1; continue; }

    # ---- guard: is the master still plausibly populated? ----
    n=$(find "$src" -maxdepth 3 -type f 2>/dev/null | head -$((MIN_FILES + 1)) | wc -l)
    if [ "$n" -lt "$MIN_FILES" ]; then
        log "[$name] REFUSING phase 2 — source has only $n files (<$MIN_FILES)."
        log "[$name] A dropped mount is indistinguishable from a mass delete. Not mirroring."
        RC=1; continue
    fi

    # ---- phase 2: make the replica match the master exactly ----
    log "[$name] phase 2 MIRROR  synology -> unas (--delete, source has >=$MIN_FILES files)"
    rsync -rlt $DRY --delete --partial --stats \
        --exclude='@eaDir/' --exclude='#recycle/' --exclude='.DS_Store' \
        --exclude='Unconfirmed *.move/' \
        -e "$SSH" "$src/" "$UNAS_HOST:$dst/" >>"$LOG" 2>&1
    r2=$?
    [ $r2 -ne 0 ] && [ $r2 -ne 24 ] && { log "[$name] phase 2 rc=$r2"; RC=1; }
    log "[$name] done (phase1=$r1 phase2=$r2)"
done

log "=== nas mirror end rc=$RC ==="
exit $RC
