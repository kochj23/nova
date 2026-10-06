#!/usr/bin/env python3
"""nova_claude_lease_reaper.py — returns dead Claude sessions' work to the queue.

Claude sessions across the cluster claim claude_queue tasks with a 20-minute lease that their
session-logger hook renews on every tool call (claude_heartbeat). If a node reboots or a session
dies, the lease lapses; this calls claude_reap() to requeue those tasks (progress notes kept),
drop expired repo locks, and tell #nova-claude so any live session can pick the work up.
All logic is in SQL (nova_ops functions, agent_docs claude-fleet-coordination); this is the clock.

scheduler-core every 5m (.2, standby .5).
"""
import psycopg2

from nova_notify import notify

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"


def main():
    with psycopg2.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, description, was FROM claude_reap()")
        rows = cur.fetchall()
    for qid, desc, was in rows:
        notify(f"Orphaned Claude task #{qid} requeued",
               body=f"{desc[:200]}\nwas claimed by {was} (lease expired). Pick up with "
                    f"SELECT * FROM claude_claim('<your claim id>', {qid});",
               level="info", category="claude_fleet", source="nova_claude_lease_reaper",
               dedup_key=f"claude-reap:{qid}:{was}")
    print(f"reaped {len(rows)}")


if __name__ == "__main__":
    main()
