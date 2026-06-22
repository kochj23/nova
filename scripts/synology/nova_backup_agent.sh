#!/bin/bash
# nova_backup_agent.sh — Nova's diff-based backup engine (the "better rsync").
# Runs on the Synology (where source + CIFS dest live). Reports telemetry to PG on .6.
#
# MODES:
#   incremental (default): sync only files changed since the last SUCCESSFUL run
#       (find -newer <marker>). No dest scan -> fast (~minutes). The marker only
#       advances on success, so a missed/failed night can never silently drop a file.
#   full: complete rsync reconcile (weekly safety net). Catches same-size content
#       edits, permission drift, and anything the incremental's mtime check misses.
#
# Additive only (no --delete). Per-job marker. Telemetry -> telemetry.backup_runs.
set -u
MODE="${1:-incremental}"
PGURL="postgresql://kochj@192.168.1.6:5432/nova_ops"
LOG=/volume1/homes/kochj/nova_backup.log
LOCK=/volume1/homes/kochj/.nova_nas_backup.lock
stamp(){ date '+%Y-%m-%d %H:%M:%S'; }

# job: name|source|dest|marker
JOBS=(
  "nas|/volume1/nas|/volume1/docker/nas|/volume1/homes/kochj/.nbk_nas"
  "external|/volume1/external|/volume1/docker/external|/volume1/homes/kochj/.nbk_external"
)
EXCL=(--exclude='@eaDir' --exclude='#recycle' --exclude='#snapshot' --exclude='.DS_Store'
      --exclude='GoogleDriveBackups/Pics/Pictures/.com-apple-bird-noname-*')

# single instance, shared with any other NAS job
exec 9>"$LOCK"
if ! flock -n 9; then echo "[$(stamp)] another NAS job holds the lock — skipping" >> "$LOG"; exit 0; fi

overall=0
for job in "${JOBS[@]}"; do
  IFS='|' read -r name src dst marker <<< "$job"
  if [ ! -d "$dst" ]; then echo "[$(stamp)] $name: DEST not mounted, skip" >> "$LOG"; overall=1; continue; fi

  t0=$(date +%s)
  startmark="${marker}.start"; : > "$startmark"   # stamp run-start BEFORE work (becomes new marker on success)

  if [ "$MODE" = "full" ] || [ ! -f "$marker" ]; then
    used="full"
    out=$(rsync -rlt --no-perms --no-owner --stats "${EXCL[@]}" "$src/" "$dst/" 2>&1); rc=$?
  else
    used="incremental"
    list=$(mktemp /tmp/nbk_XXXX)
    ( cd "$src" && find . -type f -newer "$marker" 2>/dev/null | sed 's|^\./||' ) > "$list"
    nchg=$(wc -l < "$list" | tr -d ' ')
    if [ "$nchg" -gt 0 ]; then
      out=$(rsync -rlt --no-perms --no-owner --stats "${EXCL[@]}" --files-from="$list" "$src/" "$dst/" 2>&1); rc=$?
    else out="(no files changed since last run)"; rc=0; fi
    rm -f "$list"; used="incremental:$nchg"
  fi

  t1=$(date +%s); dur=$((t1-t0))
  files=$(printf '%s' "$out" | grep -oE 'Number of regular files transferred: [0-9,]+' | grep -oE '[0-9,]+' | tr -d ',' | tail -1)
  bytes=$(printf '%s' "$out" | grep -oE 'Total transferred file size: [0-9,]+' | grep -oE '[0-9,]+' | tr -d ',' | tail -1)
  files=${files:-0}; bytes=${bytes:-0}

  if [ "$rc" = "0" ] || [ "$rc" = "24" ]; then mv -f "$startmark" "$marker"; ok="true"; else rm -f "$startmark"; ok="false"; overall="$rc"; fi
  echo "[$(stamp)] $name [$used] rc=$rc dur=${dur}s files=$files bytes=$bytes ok=$ok" >> "$LOG"

  psql "$PGURL" -tAc "INSERT INTO telemetry.backup_runs (ts,job,rc,elapsed_s,files,bytes,errors,ok) VALUES (now(),'nova-backup:$name:$MODE',$rc,$dur,$files,$bytes,$([ "$ok" = true ] && echo 0 || echo 1),$ok)" >/dev/null 2>&1 \
    && echo "[$(stamp)] $name: telemetry written" >> "$LOG" || echo "[$(stamp)] $name: telemetry FAILED" >> "$LOG"
done
echo "[$(stamp)] nova-backup ($MODE) done, overall rc=$overall" >> "$LOG"
exit "$overall"
