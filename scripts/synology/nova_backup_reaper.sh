#!/bin/bash
# nova_backup_reaper.sh — garbage-collect orphans on the UNAS (dest) that no longer
# exist on the Synology (source), turning the additive backup into a soft mirror with
# a 15-DAY deletion delay (your accidental-delete safety net).
#
#   - Scope: the backup-managed shares only (nas, external).
#   - An orphan = file on UNAS but NOT on Synology. Its 15-day clock starts when first seen.
#   - If it reappears on the source (or is removed) the clock is cleared.
#   - After 15 days a still-orphaned file is PROPOSED for removal — never auto-deleted.
#     `--apply` removes ONLY rows a human marked status='approved'.
#   - Slack warnings via the notification bus (telemetry.events): new orphans + ready-to-reap.
set -u
PGURL="postgresql://kochj@192.168.1.6:5432/nova_ops"
WINDOW=15
LOG=/volume1/homes/kochj/nova_backup_reaper.log
MODE="${1:-scan}"
stamp(){ date '+%Y-%m-%d %H:%M:%S'; }
SHARES=("nas|/volume1/nas|/volume1/docker/nas" "external|/volume1/external|/volume1/docker/external")

psql "$PGURL" -tAc "CREATE TABLE IF NOT EXISTS backup_orphans (
  share text, relpath text, first_seen timestamptz DEFAULT now(), last_seen timestamptz DEFAULT now(),
  size_bytes bigint, status text DEFAULT 'tracking', PRIMARY KEY(share,relpath))" >/dev/null 2>&1

emit(){ # title | level  -> notification bus (#nova-warning routes warnings)
  psql "$PGURL" -tAc "INSERT INTO telemetry.events (ts,title,body,level,category,source) VALUES (now(),\$\$$1\$\$,'',\$\$$2\$\$,'backup','nova-backup-reaper')" >/dev/null 2>&1 || true
}

if [ "$MODE" = "apply" ]; then
  # delete ONLY approved orphans
  freed=0; done=0
  while IFS='|' read -r share relpath; do
    [ -z "$share" ] && continue
    for e in "${SHARES[@]}"; do IFS='|' read -r n s d <<< "$e"; [ "$n" = "$share" ] && f="$d/$relpath"; done
    if [ -f "$f" ]; then sz=$(stat -c %s "$f" 2>/dev/null||echo 0); rm -f "$f" && { freed=$((freed+sz)); done=$((done+1)); }; fi
    psql "$PGURL" -tAc "UPDATE backup_orphans SET status='reaped' WHERE share='$share' AND relpath=\$\$$relpath\$\$" >/dev/null 2>&1
  done < <(psql "$PGURL" -tAF'|' -c "SELECT share, relpath FROM backup_orphans WHERE status='approved'")
  echo "[$(stamp)] REAPED $done orphans, freed ~$((freed/1000000000))GB from UNAS" >> "$LOG"
  emit "Backup reaper: reaped $done orphans, freed ~$((freed/1000000000))GB from UNAS" "info"
  exit 0
fi

# scan mode (default): detect orphans, age them, propose >15d
new_total=0; prop_total=0
for entry in "${SHARES[@]}"; do
  IFS='|' read -r name src dst <<< "$entry"
  [ -d "$dst" ] || { echo "[$(stamp)] $name: dest not mounted, skip" >> "$LOG"; continue; }
  src_lst=$(mktemp); dst_lst=$(mktemp); orph=$(mktemp)
  ( cd "$src" && find . -type f 2>/dev/null | sort ) > "$src_lst"
  ( cd "$dst" && find . -type f 2>/dev/null | sort ) > "$dst_lst"
  comm -13 "$src_lst" "$dst_lst" | sed 's|^\./||' > "$orph"     # in dst, not in src
  norph=$(wc -l < "$orph" | tr -d ' ')
  psql "$PGURL" >/dev/null 2>&1 <<SQL
CREATE TEMP TABLE cur(relpath text);
\copy cur FROM '$orph'
INSERT INTO backup_orphans(share,relpath,last_seen) SELECT '$name',relpath,now() FROM cur
  ON CONFLICT(share,relpath) DO UPDATE SET last_seen=now();
DELETE FROM backup_orphans b WHERE b.share='$name' AND b.status<>'reaped'
  AND NOT EXISTS (SELECT 1 FROM cur c WHERE c.relpath=b.relpath);
UPDATE backup_orphans SET status='proposed'
  WHERE share='$name' AND status='tracking' AND first_seen < now()-interval '$WINDOW days';
SQL
  prop=$(psql "$PGURL" -tAc "SELECT count(*) FROM backup_orphans WHERE share='$name' AND status='proposed'")
  echo "[$(stamp)] $name: $norph orphans tracked, $prop proposed for removal (>$WINDOW days)" >> "$LOG"
  prop_total=$((prop_total + ${prop:-0}))
  rm -f "$src_lst" "$dst_lst" "$orph"
done
echo "[$(stamp)] reaper scan done — $prop_total orphan(s) proposed (propose-only, nothing deleted)" >> "$LOG"
[ "${prop_total:-0}" -gt 0 ] && emit "Backup reaper: $prop_total UNAS orphan(s) past their 15-day window — proposed for removal (review + approve)" "warning"
exit 0
