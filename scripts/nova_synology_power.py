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


def expected_off(dsn: str = DSN) -> bool:
    """True only when the cycle has deliberately powered the Synology off."""
    try:
        with psycopg2.connect(dsn, connect_timeout=5) as c, c.cursor() as cur:
            cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s", (SERVICE, KEY))
            row = cur.fetchone()
            return bool(row) and row[0] is True
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
