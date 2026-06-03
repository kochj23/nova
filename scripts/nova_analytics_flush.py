#!/usr/bin/env python3
"""
nova_analytics_flush.py — Flush analytics events from Redis stream to PostgreSQL.

Runs every 5 minutes via scheduler. Reads up to 1000 entries from the
analytics:events Redis stream, batch-inserts into analytics_pageviews and
analytics_events tables, then trims consumed entries.

Written by Jordan Koch.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import redis

sys.path.insert(0, str(Path(__file__).parent))
from nova_logger import log, LOG_INFO, LOG_ERROR

REDIS_URL = "redis://192.168.1.6:6379"
PG_DSN = "host=192.168.1.6 dbname=nova_ops user=kochj"
STREAM_KEY = "analytics:events"
BATCH_SIZE = 1000


def flush():
    log("Analytics flush starting...", level=LOG_INFO, source="analytics_flush")

    r = redis.from_url(REDIS_URL, decode_responses=True)
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()

    entries = r.xrange(STREAM_KEY, count=BATCH_SIZE)
    if not entries:
        log("No entries to flush", level=LOG_INFO, source="analytics_flush")
        r.close()
        conn.close()
        return

    pageviews = []
    events = []
    last_id = None

    for entry_id, data in entries:
        last_id = entry_id
        ts_epoch = int(data.get("ts", 0))
        ts = datetime.fromtimestamp(ts_epoch, tz=timezone.utc) if ts_epoch else datetime.now(timezone.utc)

        if data.get("type") == "pageview":
            pageviews.append((
                ts,
                data.get("site", ""),
                data.get("path", ""),
                data.get("referrer_domain", "") or None,
                data.get("country", "") or None,
                data.get("ua_bucket", "") or None,
                data.get("visitor_hash", ""),
                int(data.get("response_ms", 0)) or None,
            ))
        elif data.get("type") == "event":
            event_data = data.get("event_data", "{}")
            if isinstance(event_data, str):
                try:
                    event_data = json.loads(event_data)
                except (json.JSONDecodeError, TypeError):
                    event_data = {}
            events.append((
                ts,
                data.get("site", ""),
                data.get("path", ""),
                data.get("event_type", "custom"),
                json.dumps(event_data),
                data.get("visitor_hash", ""),
                data.get("country", "") or None,
            ))

    if pageviews:
        cur.executemany(
            "INSERT INTO analytics_pageviews (ts, site, path, referrer_domain, country, ua_bucket, visitor_hash, response_ms) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            pageviews
        )

    if events:
        cur.executemany(
            "INSERT INTO analytics_events (ts, site, path, event_type, event_data, visitor_hash, country) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            events
        )

    conn.commit()

    # Trim consumed entries
    if last_id:
        r.xtrim(STREAM_KEY, minid=last_id, approximate=False)

    log(f"Flushed {len(pageviews)} pageviews + {len(events)} events", level=LOG_INFO, source="analytics_flush")

    # Retention: delete pageviews/events older than 90 days
    cur.execute("DELETE FROM analytics_pageviews WHERE ts < now() - interval '90 days'")
    cur.execute("DELETE FROM analytics_events WHERE ts < now() - interval '90 days'")
    deleted = cur.rowcount
    if deleted:
        log(f"Retention cleanup: removed {deleted} old rows", level=LOG_INFO, source="analytics_flush")
    conn.commit()

    cur.close()
    conn.close()
    r.close()


if __name__ == "__main__":
    flush()
