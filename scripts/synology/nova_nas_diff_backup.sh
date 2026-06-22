#!/bin/bash
# nova_nas_diff_backup.sh — FAST diff-based Synology -> UNAS sync.
#
# Instead of letting rsync stat 110k files over the slow CIFS mount (9-19h), this:
#   1. `find`s the SOURCE locally (fast, seconds)
#   2. `find`s the DEST over CIFS for path+size only (~90s, not hours)
#   3. diffs the two lists -> the exact set of new/size-changed files
#   4. rsyncs ONLY those via --files-from
# Result: ~2-minute reconcile + a tiny transfer instead of an all-night grind.
#
# Compares by path+SIZE (robust over CIFS; mtime is unreliable there). A file
# edited in place at the *same size* won't be caught here — that's what the
# WEEKLY full rsync (the safety net) is for. Additive only: never deletes.
set -u
LOG=/volume1/homes/kochj/nova_nas_diff.log
LOCK=/volume1/homes/kochj/.nova_nas_backup.lock   # shared w/ full backup: never overlap
STAMP() { date '+%Y-%m-%d %H:%M:%S'; }

# single-instance, shared with the full backup so the two never hit the dest at once
exec 9>"$LOCK"
if ! flock -n 9; then echo "[$(STAMP)] another NAS job holds the lock — skipping" >> "$LOG"; exit 0; fi

# src -> dst pairs
SRCS=(/volume1/nas        /volume1/external)
DSTS=(/volume1/docker/nas /volume1/docker/external)
EXCLUDE_RE='@eaDir|/#recycle|/#snapshot|\.DS_Store$|GoogleDriveBackups/Pics/Pictures/\.com-apple-bird-noname-'

overall=0
for i in "${!SRCS[@]}"; do
  SRC="${SRCS[$i]}"; DST="${DSTS[$i]}"; name="$(basename "$SRC")"
  echo "[$(STAMP)] === diff $name: $SRC -> $DST ===" >> "$LOG"
  if [ ! -d "$DST" ]; then echo "[$(STAMP)] $name: DEST not mounted, skipping" >> "$LOG"; overall=1; continue; fi

  src_lst="/tmp/nasdiff_src_$name.lst"; dst_lst="/tmp/nasdiff_dst_$name.lst"; to="/tmp/nasdiff_to_$name.lst"
  t0=$(date +%s)
  ( cd "$SRC" && find . -type f -printf '%P\t%s\n' 2>/dev/null | sort ) > "$src_lst"
  ( cd "$DST" && find . -type f -printf '%P\t%s\n' 2>/dev/null | sort ) > "$dst_lst"
  # paths in src that are missing in dst OR differ in size; drop poison/system files
  awk -F'\t' 'NR==FNR{d[$1]=$2; next} {if(!($1 in d) || d[$1]!=$2) print $1}' "$dst_lst" "$src_lst" \
    | grep -vE "$EXCLUDE_RE" > "$to"
  nsrc=$(wc -l < "$src_lst"); ndst=$(wc -l < "$dst_lst"); nto=$(wc -l < "$to"); t1=$(date +%s)
  echo "[$(STAMP)] $name: src=$nsrc dst=$ndst to_sync=$nto (diff took $((t1-t0))s)" >> "$LOG"
  echo "  $name: src=$nsrc dst=$ndst -> $nto files need sync (diff $((t1-t0))s)"

  if [ "$nto" -eq 0 ]; then echo "[$(STAMP)] $name: already in sync" >> "$LOG"; continue; fi
  rsync -rlt --no-perms --no-owner --stats --files-from="$to" "$SRC/" "$DST/" >> "$LOG" 2>&1
  rc=$?; [ "$rc" -ne 0 ] && [ "$rc" -ne 24 ] && overall="$rc"
  echo "[$(STAMP)] $name: rsync rc=$rc ($nto files)" >> "$LOG"
  echo "  $name: rsync rc=$rc on $nto files"
done
echo "[$(STAMP)] diff-backup done, overall rc=$overall" >> "$LOG"
exit "$overall"
