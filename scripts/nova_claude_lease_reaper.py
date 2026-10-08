#!/usr/bin/env python3
"""nova_claude_lease_reaper.py — returns dead Claude sessions' work to the queue.

Claude sessions across the cluster claim claude_queue tasks with a 20-minute lease that their
session-logger hook renews on every tool call (claude_heartbeat). If a node reboots or a session
dies, the lease lapses; this calls claude_reap() to requeue those tasks (progress notes kept),
drop expired repo locks, and tell #nova-claude so any live session can pick the work up.
All logic is in SQL (nova_ops functions, agent_docs claude-fleet-coordination); this is the clock.

scheduler-core every 5m (.2, standby .5).
"""
import sys
import time

import psycopg2

from nova_notify import notify

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"


def _reap(attempts=3):
    """claude_reap() rows; retries a PG failover/blip with backoff, re-raises on the last try."""
    for attempt in range(attempts):
        try:
            with psycopg2.connect(DSN, connect_timeout=10) as conn, conn.cursor() as cur:
                cur.execute("SELECT id, description, was FROM claude_reap()")
                return cur.fetchall()
        except psycopg2.OperationalError as e:
            if attempt == attempts - 1:
                raise
            print(f"claude_reap failed ({e}); retry {attempt + 1}", file=sys.stderr)
            time.sleep(5 * 2 ** attempt)


def main():
    rows = _reap()
    failed = 0
    for qid, desc, was in rows:
        try:   # one failed notice must not swallow the rest: these tasks are already requeued
            notify(f"Orphaned Claude task #{qid} requeued",
                   body=f"{(desc or '')[:200]}\nwas claimed by {was} (lease expired). Pick up with "
                        f"SELECT * FROM claude_claim('<your claim id>', {qid});",
                   level="info", category="claude_fleet", source="nova_claude_lease_reaper",
                   dedup_key=f"claude-reap:{qid}:{was}")
        except Exception as e:
            failed += 1
            print(f"notify failed for #{qid}: {e}", file=sys.stderr)
    print(f"reaped {len(rows)}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
