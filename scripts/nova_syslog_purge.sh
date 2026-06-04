#!/bin/bash
# nova_syslog_purge.sh — Daily retention cleanup for syslog_events table.
# Keeps non-threat events for 90 days, threat events for 365 days.
# Written by Jordan Koch.

set -euo pipefail

DELETED_NORMAL=$(psql -d nova_ops -t -c "DELETE FROM syslog_events WHERE received_at < now() - interval '90 days' AND threat_type IS NULL RETURNING 1" | wc -l | tr -d ' ')
DELETED_THREAT=$(psql -d nova_ops -t -c "DELETE FROM syslog_events WHERE received_at < now() - interval '365 days' AND threat_type IS NOT NULL RETURNING 1" | wc -l | tr -d ' ')

echo "[syslog_purge] Deleted: ${DELETED_NORMAL} normal events (>90d), ${DELETED_THREAT} threat events (>365d)"
