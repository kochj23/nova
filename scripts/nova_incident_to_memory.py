#!/usr/bin/env python3
"""nova_incident_to_memory.py — feed resolved incidents into the memory corpus.

The triage brain (nova_alert_triage.py) retrieves "what did something like this
turn out to be last time, and what fixed it" — which only works if resolved
incidents are recallable. This ingests public.incidents (title + severity +
affected services + root_cause) as source='incident' memories. Idempotent via
text_hash dedup at the memory server; a stable metadata.incident_id lets the
feedback loop find them. Run once to backfill, then daily to catch newly-resolved.
"""
import json
import sys
import urllib.request
from datetime import datetime

import psycopg2

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
MEMSRV = "http://memory-server.digitalnoise.net:18790"


def remember(text, metadata):
    req = urllib.request.Request(
        f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"text": text, "source": "incident", "metadata": metadata}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("id")


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else "2000-01-01"
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    oc.execute("""SELECT id, title, severity, root_cause, affected_services,
                         started_at::date, resolved_at
                  FROM incidents
                  WHERE status='resolved' AND root_cause IS NOT NULL
                  AND coalesce(resolved_at, started_at) >= %s
                  ORDER BY resolved_at NULLS LAST""", (since,))
    rows = oc.fetchall()
    n = 0
    for iid, title, sev, cause, svcs, day, resolved in rows:
        svc = ", ".join(svcs) if svcs else "—"
        text = (f"[Incident {day}] {title}\n"
                f"Severity: {sev}. Affected: {svc}.\n"
                f"Root cause / resolution: {cause}")
        try:
            remember(text, {"type": "incident", "incident_id": str(iid),
                            "severity": sev, "affected": svcs or [],
                            "resolved": str(resolved), "privacy": "private"})
            n += 1
        except Exception as e:
            print(f"[incident-mem] failed {iid}: {e}", flush=True)
    print(f"[incident-mem] ingested {n}/{len(rows)} resolved incidents (since {since})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
