#!/bin/bash
# nova_backup_retention.sh — enforce a ~2-week (14-day) retention on file-backup
# dirs that don't self-prune. Policy (Jordan, 2026-07-01): backups older than two
# weeks are never restored — we roll forward — so keep 14 days and delete the rest.
#
# NOTE: PG dumps (nova_pg_backup.sh) and NAS Google-Drive snapshots
# (nova_backup_nas.sh) already manage their own 14-day retention. This covers the
# remaining accumulating dirs (config/deploy snapshots under ~/.openclaw/backups).
# Scheduled daily via scheduler.yaml (backup_retention).
set -uo pipefail

RETENTION_DAYS=14
DIRS=(
  "$HOME/.openclaw/backups"
)

for d in "${DIRS[@]}"; do
  [ -d "$d" ] || continue
  before=$(du -sh "$d" 2>/dev/null | cut -f1)
  n_before=$(find "$d" -maxdepth 1 -mindepth 1 2>/dev/null | wc -l | tr -d ' ')
  # delete top-level items (files + snapshot dirs) older than the retention window
  find "$d" -maxdepth 1 -mindepth 1 -mtime +"$RETENTION_DAYS" -exec rm -rf {} + 2>/dev/null
  after=$(du -sh "$d" 2>/dev/null | cut -f1)
  n_after=$(find "$d" -maxdepth 1 -mindepth 1 2>/dev/null | wc -l | tr -d ' ')
  echo "[backup-retention] $(date '+%Y-%m-%d %H:%M') $d: ${before} -> ${after} (items ${n_before} -> ${n_after}, kept ${RETENTION_DAYS}d)"
done
