#!/usr/bin/env python3
"""
nova_service_down_watch.py — alert on services down >2h (scheduler: service_down_watch).

Scans nova_ops.health_checks for (service_name, node_name) pairs whose LATEST
check is 'down' and whose last 'up' was more than 2 hours ago (or never in the
lookback window). Posts ONE warning per service per 12 hours via the central
notify() bus; last-alert times persist in telemetry.service_down_alerts so
restarts don't re-flood.

Closes the gap where mac-mini Ollama sat down for 736 hours (Aug 14 → Sep 12
2026) with nobody paged. Runs on nova-core (.2) every 30m. Added 2026-09-13.
Written by Jordan Koch.
"""
import sys

import psycopg2

from nova_notify import notify

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
DOWN_AFTER_HOURS = 2      # service must be down this long before we alert
REALERT_HOURS = 12        # at most one alert per service per this window
LOOKBACK_DAYS = 7         # ignore services with no checks at all in this window

DDL = """
CREATE SCHEMA IF NOT EXISTS telemetry;
CREATE TABLE IF NOT EXISTS telemetry.service_down_alerts (
    service_name  text NOT NULL,
    node_name     text NOT NULL,
    last_alert_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (service_name, node_name)
);
"""

DOWN_QUERY = """
WITH latest AS (
    SELECT DISTINCT ON (service_name, node_name)
           service_name, node_name, status, checked_at, error_message
      FROM health_checks
     WHERE checked_at > now() - interval '%(lookback)s days'
     ORDER BY service_name, node_name, checked_at DESC
), last_up AS (
    SELECT service_name, node_name, max(checked_at) AS last_up_at
      FROM health_checks
     WHERE status = 'up'
       AND checked_at > now() - interval '%(lookback)s days'
     GROUP BY 1, 2
)
SELECT l.service_name, l.node_name, l.error_message,
       round(extract(epoch FROM (now() - coalesce(u.last_up_at,
             now() - interval '%(lookback)s days'))) / 3600.0, 1) AS down_hours
  FROM latest l
  LEFT JOIN last_up u USING (service_name, node_name)
 WHERE l.status = 'down'
   AND coalesce(u.last_up_at, now() - interval '%(lookback)s days')
       < now() - interval '%(down_after)s hours'
""" % {"lookback": LOOKBACK_DAYS, "down_after": DOWN_AFTER_HOURS}

SHOULD_ALERT = """
SELECT NOT EXISTS (
    SELECT 1 FROM telemetry.service_down_alerts
     WHERE service_name = %s AND node_name = %s
       AND last_alert_at > now() - interval '{} hours'
)
""".format(REALERT_HOURS)

RECORD_ALERT = """
INSERT INTO telemetry.service_down_alerts (service_name, node_name, last_alert_at)
VALUES (%s, %s, now())
ON CONFLICT (service_name, node_name) DO UPDATE SET last_alert_at = now()
"""


def main():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    alerted = 0
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.execute(DOWN_QUERY)
            down = cur.fetchall()
            for service, node, err, down_hours in down:
                cur.execute(SHOULD_ALERT, (service, node))
                if not cur.fetchone()[0]:
                    continue  # alerted within the last REALERT_HOURS
                title = f"SERVICE DOWN: {service} on {node} ({down_hours}h)"
                body = (f"health_checks shows {service}@{node} down for "
                        f"~{down_hours}h (threshold {DOWN_AFTER_HOURS}h)."
                        + (f" Last error: {err[:200]}" if err else ""))
                notify(title, body=body, level="warning", category="fleet",
                       dedup_key=f"service-down-{service}-{node}",
                       meta={"service": service, "node": node,
                             "down_hours": float(down_hours)})
                cur.execute(RECORD_ALERT, (service, node))
                alerted += 1
    finally:
        conn.close()
    print(f"[service_down_watch] down_over_{DOWN_AFTER_HOURS}h={len(down)} "
          f"alerted={alerted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
