#!/bin/bash
# nova_wal_archive_cmd.sh — Postgres archive_command for the nova-core primary: ship one finished WAL
# segment to the NAS, zstd-compressed, never overwriting a different file.
#
# Little Mister, 2026-10-15 (#36): "PG WAL archiving to the NAS so Tier-0 RPO becomes minutes."
# With a weekly pg_basebackup (nova_wal_archive.py --basebackup) plus this archive, nova_ops can be
# restored to within one archive_timeout (300 s) of a failure instead of to last night's 02:00 dump.
#
#   archive_command = '<local copy of this file> %p %f'
#
# The LOCAL copy (installed by `nova_wal_archive.py --install`, default ~/.local/libexec/nova/) is
# what Postgres runs — never the repo copy, because the repo lives on the same NAS share whose loss
# this script must survive.
#
# Contract (Postgres docs, "Setting Up WAL Archiving"):
#   exit 0  only when the segment is durably in the archive (or an identical copy already is);
#   exit !0 otherwise -> Postgres keeps the segment in pg_wal and retries.
# SAFETY VALVE: if the NAS is unreachable AND pg_wal has grown past NOVA_WAL_CAP_GB or the data volume
# has less than NOVA_WAL_MIN_FREE_GB free, the segment is DROPPED (exit 0) and recorded in the gap
# log. A full disk takes down the Tier-0 primary; a gap only costs point-in-time recovery until the
# next base backup — nova_wal_archive.py --monitor sees the gap and takes a fresh base backup itself.
#
# Env: NOVA_WAL_ARCHIVE_DIR (default /mnt/nas/backups/pg_wal_archive), NOVA_WAL_GAP_LOG,
#      NOVA_WAL_CAP_GB (48), NOVA_WAL_MIN_FREE_GB (60), NOVA_WAL_TIMEOUT_S (120).
# Written by Jordan Koch (via Claude).
set -u
umask 077

SRC="${1:-}"; NAME="${2:-}"
DEST_DIR="${NOVA_WAL_ARCHIVE_DIR:-/mnt/nas/backups/pg_wal_archive}"
GAP_LOG="${NOVA_WAL_GAP_LOG:-$HOME/.local/state/nova/wal_gaps.log}"
CAP_GB="${NOVA_WAL_CAP_GB:-48}"
MIN_FREE_GB="${NOVA_WAL_MIN_FREE_GB:-60}"
TMO="${NOVA_WAL_TIMEOUT_S:-120}"

err() { echo "nova_wal_archive_cmd: $*" >&2; }

# Only ever touch names Postgres hands us: segments, .partial, .backup labels, .history files.
if [ -z "$SRC" ] || [ -z "$NAME" ] || ! [[ "$NAME" =~ ^[0-9A-F]{8}([0-9A-F]{16}(\.partial|\.[0-9A-F]{8}\.backup)?|\.history)$ ]]; then
    err "refusing bad arguments: '$SRC' '$NAME'"; exit 2
fi
[ -f "$SRC" ] || { err "source missing: $SRC"; exit 3; }

FINAL="$DEST_DIR/$NAME.zst"

valve() {   # NAS unusable: keep pg_wal unless it threatens the primary's disk
    local reason="$1" wal_dir used_kb free_kb
    wal_dir="$(dirname "$SRC")"
    used_kb=$(du -sk "$wal_dir" 2>/dev/null | awk '{print $1}')
    free_kb=$(df -Pk "$wal_dir" 2>/dev/null | awk 'NR==2{print $4}')
    if [ "${used_kb:-0}" -gt $((CAP_GB * 1024 * 1024)) ] || [ "${free_kb:-0}" -lt $((MIN_FREE_GB * 1024 * 1024)) ]; then
        mkdir -p "$(dirname "$GAP_LOG")" 2>/dev/null
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) DROPPED $NAME reason=$reason pg_wal_kb=${used_kb:-?} free_kb=${free_kb:-?}" >> "$GAP_LOG"
        err "SAFETY VALVE: dropped $NAME ($reason; pg_wal ${used_kb:-?} KB, free ${free_kb:-?} KB) — PITR chain broken until next base backup"
        exit 0
    fi
    err "$reason — Postgres will retry $NAME (pg_wal ${used_kb:-?} KB, free ${free_kb:-?} KB)"
    exit 1
}

if ! timeout "$TMO" test -d "$DEST_DIR"; then
    timeout "$TMO" mkdir -p "$DEST_DIR" 2>/dev/null || valve "archive dir unreachable: $DEST_DIR"
fi

# Already archived? Identical -> success (Postgres may re-send after a crash); different -> refuse.
if timeout "$TMO" test -e "$FINAL"; then
    if timeout "$TMO" zstd -q -d -c "$FINAL" 2>/dev/null | cmp -s - "$SRC"; then
        exit 0
    fi
    err "$FINAL exists with DIFFERENT contents — refusing to overwrite"; exit 4
fi

TMP="$DEST_DIR/.$NAME.zst.tmp.$$"
if timeout "$TMO" zstd -q -3 -f -o "$TMP" "$SRC" \
   && timeout "$TMO" zstd -q -t "$TMP" \
   && timeout "$TMO" mv -f "$TMP" "$FINAL"; then
    exit 0
fi
rm -f "$TMP" 2>/dev/null
valve "write to $DEST_DIR failed"
