#!/usr/bin/env python3
"""
nova_pihole_poller.py — Polls Pi-hole API and stores stats in PG.

Feeds the Pi-hole DNS Grafana dashboard with query counts, blocked domains,
client activity, and cache stats.
"""

import json
import os
import signal
import sys
import time
import urllib.request

PIHOLE_API = "http://192.168.1.2/admin/api.php"
POLL_INTERVAL = 60  # seconds
# DSN is env-overridable so the poller is portable: on .6 'localhost' = the PG
# primary; when migrated to a peer (e.g. .2) set NOVA_PG_DSN to the .6 primary so
# writes never hit a read-only replica. (#694)
PG_DSN = os.environ.get("NOVA_PG_DSN", "dbname=nova_ops user=kochj host=localhost")

_running = True


def _create_table():
    """Create pihole_stats table if it doesn't exist."""
    import psycopg2
    conn = psycopg2.connect(PG_DSN, connect_timeout=5)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pihole_stats (
            timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
            dns_queries_today INT,
            ads_blocked_today INT,
            ads_percentage_today REAL,
            domains_being_blocked INT,
            queries_cached INT,
            queries_forwarded INT,
            unique_clients INT,
            status TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pihole_top_domains (
            timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
            domain TEXT NOT NULL,
            count INT NOT NULL,
            blocked BOOLEAN DEFAULT false
        )
    """)
    conn.close()


def _fetch_summary() -> dict | None:
    """Fetch Pi-hole summary stats."""
    try:
        req = urllib.request.Request(f"{PIHOLE_API}?summary")
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception as e:
        print(f"[pihole-poller] API fetch failed: {e}", file=sys.stderr, flush=True)
        return None


def _fetch_top_items() -> dict | None:
    """Fetch top queries and blocked domains."""
    try:
        req = urllib.request.Request(f"{PIHOLE_API}?topItems=20")
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception:
        return None


def _store_stats(summary: dict):
    """Store summary stats in PG."""
    import psycopg2
    conn = psycopg2.connect(PG_DSN, connect_timeout=5)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO pihole_stats (
            dns_queries_today, ads_blocked_today, ads_percentage_today,
            domains_being_blocked, queries_cached, queries_forwarded,
            unique_clients, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        int(summary.get("dns_queries_today", 0)),
        int(summary.get("ads_blocked_today", 0)),
        float(summary.get("ads_percentage_today", 0)),
        int(summary.get("domains_being_blocked", 0)),
        int(summary.get("queries_cached", 0)),
        int(summary.get("queries_forwarded", 0)),
        int(summary.get("unique_clients", 0)),
        summary.get("status", "unknown"),
    ))
    conn.close()


def _store_top_items(top_items: dict):
    """Store top queried and blocked domains."""
    import psycopg2
    conn = psycopg2.connect(PG_DSN, connect_timeout=5)
    conn.autocommit = True
    cur = conn.cursor()

    # Top queries
    for domain, count in (top_items.get("top_queries", {}) or {}).items():
        cur.execute("""
            INSERT INTO pihole_top_domains (domain, count, blocked)
            VALUES (%s, %s, false)
        """, (domain, int(count)))

    # Top ads/blocked
    for domain, count in (top_items.get("top_ads", {}) or {}).items():
        cur.execute("""
            INSERT INTO pihole_top_domains (domain, count, blocked)
            VALUES (%s, %s, true)
        """, (domain, int(count)))

    conn.close()


def main():
    global _running
    print("[pihole-poller] Starting (polling every 60s)", flush=True)

    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))

    _create_table()

    while _running:
        summary = _fetch_summary()
        if summary:
            _store_stats(summary)
            print(f"[pihole-poller] {summary.get('dns_queries_today', '?')} queries, "
                  f"{summary.get('ads_blocked_today', '?')} blocked, "
                  f"{summary.get('unique_clients', '?')} clients", flush=True)

        top = _fetch_top_items()
        if top:
            _store_top_items(top)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
