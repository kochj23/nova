#!/usr/bin/env python3
"""
nova_queue_aging.py — weekly claude_queue hygiene (scheduler: queue_aging).

Two passes, both idempotent:
  1. DELETE terminal rows (resolved/completed/aged_out/superseded/cancelled)
     not touched in 7 days — they've been actioned; keep the queue lean.
  2. Mark status='aged_out' any still-'queued' AUTO-GENERATED alerts
     (TASK FAILING: / OVERNIGHT: / MAINTENANCE: prefixes) older than 14 days —
     if nobody picked them up in two weeks they're stale noise, not work.

Prints counts; exit 0 on success. Runs on nova-core (.2), Mondays 07:00.
Added 2026-09-13 as part of the queue-flood cleanup (Jordan: "do it all").
Written by Jordan Koch.
"""
import sys

import psycopg2

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

DELETE_TERMINAL = """
DELETE FROM claude_queue
 WHERE status IN ('resolved','completed','aged_out','superseded','cancelled')
   AND updated_at < now() - interval '7 days'
"""

AGE_OUT_STALE_ALERTS = """
UPDATE claude_queue
   SET status = 'aged_out', updated_at = now()
 WHERE status = 'queued'
   AND (description LIKE 'TASK FAILING:%'
        OR description LIKE 'OVERNIGHT:%'
        OR description LIKE 'MAINTENANCE:%')
   AND created_at < now() - interval '14 days'
"""


def main():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(DELETE_TERMINAL)
            deleted = cur.rowcount
            cur.execute(AGE_OUT_STALE_ALERTS)
            aged = cur.rowcount
    finally:
        conn.close()
    print(f"[queue_aging] deleted_terminal={deleted} aged_out_stale_alerts={aged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
