#!/usr/bin/env python3
"""nova_synology_power.py — the one flag that says "the Synology is OFF on purpose".

The Synology (192.168.1.11) is the cold replica: nova_synology_cycle.py wakes it once a day,
replicates UNAS->Synology, waits for Cloud Sync's off-site upload, then powers it off. While it
is deliberately off, monitors (nova_synology_monitor, nova_watchtower) must stay quiet.

State lives in PG: service_config(service='synology_cycle', key='expected_off') = true/false (jsonb).
Fail-open for monitors: if PG is unreachable, expected_off() returns False (= monitor as usual).
"""
import os

import psycopg2

DSN = os.environ.get("NOVA_OPS_DSN", "host=localhost dbname=nova_ops user=kochj")
SYNOLOGY_IP = "192.168.1.11"
SERVICE, KEY = "synology_cycle", "expected_off"


MAX_OFF_S = 26 * 3600   # a nightly cycle that has not come back by now is a failure, not a plan


def expected_off(dsn: str = DSN) -> bool:
    """True only when the cycle has deliberately powered the Synology off AND it is not overdue.

    The flag is cleared by the next SUCCESSFUL wake, so a failed wake used to suppress monitoring
    forever. 2026-11-16: the wake failed (WoL cannot reach the 10GbE add-in card), and for 19 hours
    every replication layer reported "skipped cleanly: Synology is off on purpose" as a SUCCESS
    while the only replica stayed down. An alarm you can silence indefinitely by failing is not an
    alarm, so the suppression now expires and the monitors go back to complaining.
    """
    try:
        with psycopg2.connect(dsn, connect_timeout=5) as c, c.cursor() as cur:
            cur.execute("SELECT value, updated_at FROM service_config WHERE service=%s AND key=%s",
                        (SERVICE, KEY))
            row = cur.fetchone()
            if not row or row[0] is not True:
                return False
            cur.execute("SELECT now() - %s > %s * interval '1 second'", (row[1], MAX_OFF_S))
            return not cur.fetchone()[0]
    except Exception:
        return False


def set_expected_off(value: bool, dsn: str = DSN) -> None:
    with psycopg2.connect(dsn, connect_timeout=5) as c, c.cursor() as cur:
        cur.execute("""
            INSERT INTO service_config (service, key, value, updated_at, updated_by)
            VALUES (%s, %s, %s::jsonb, now(), 'nova_synology_cycle')
            ON CONFLICT (service, key) DO UPDATE
              SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by
        """, (SERVICE, KEY, "true" if value else "false"))
