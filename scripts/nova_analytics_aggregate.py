#!/usr/bin/env python3
"""
nova_analytics_aggregate.py — Hourly analytics aggregation + anomaly alerting.

Runs at 5 minutes past every hour. Aggregates the previous hour's raw
analytics_pageviews into analytics_hourly for fast dashboard queries.

Also detects anomalies and fires alerts:
  - Traffic spike (10x hourly average for that site)
  - Referrer bomb (single domain sending >50% of traffic)
  - Site goes dark (no events for 30+ min during 06:00-23:00)

Written by Jordan Koch.
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_WARN, LOG_ERROR

PG_DSN = "host=192.168.1.6 dbname=nova_ops user=kochj"

SITES = ["nova.digitalnoise.net", "digitalnoise.net", "chat.digitalnoise.net", "gauges.digitalnoise.net"]
SPIKE_MULTIPLIER = 10
REFERRER_BOMB_THRESHOLD = 0.5
DARK_MINUTES = 30


def get_conn():
    return psycopg2.connect(PG_DSN)


def aggregate_hour(conn, hour_start, hour_end):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Site-level aggregates
    cur.execute("""
        SELECT
            site,
            path,
            COUNT(*) as views,
            COUNT(DISTINCT visitor_hash) as unique_visitors,
            AVG(response_ms) FILTER (WHERE response_ms > 0) as avg_response_ms
        FROM analytics_pageviews
        WHERE ts >= %s AND ts < %s
        GROUP BY site, path
    """, (hour_start, hour_end))
    rows = cur.fetchall()

    if not rows:
        log("No pageviews to aggregate for this hour", level=LOG_INFO, source="analytics_agg")
        cur.close()
        return {}

    # Get engagement data
    cur.execute("""
        SELECT site, path,
            AVG((event_data->>'seconds')::float) as avg_engagement_s
        FROM analytics_events
        WHERE ts >= %s AND ts < %s AND event_type = 'engagement'
        AND event_data->>'seconds' IS NOT NULL
        GROUP BY site, path
    """, (hour_start, hour_end))
    engagement = {(r["site"], r["path"]): r["avg_engagement_s"] for r in cur.fetchall()}

    # Get scroll depth
    cur.execute("""
        SELECT site, path,
            AVG((event_data->>'depth')::float) as avg_scroll_pct
        FROM analytics_events
        WHERE ts >= %s AND ts < %s AND event_type = 'scroll'
        AND event_data->>'depth' IS NOT NULL
        GROUP BY site, path
    """, (hour_start, hour_end))
    scroll = {(r["site"], r["path"]): r["avg_scroll_pct"] for r in cur.fetchall()}

    # Get top referrers per site
    cur.execute("""
        SELECT site, referrer_domain, COUNT(*) as cnt
        FROM analytics_pageviews
        WHERE ts >= %s AND ts < %s AND referrer_domain IS NOT NULL AND referrer_domain != ''
        GROUP BY site, referrer_domain
        ORDER BY cnt DESC
    """, (hour_start, hour_end))
    referrers_raw = cur.fetchall()
    referrers_by_site = {}
    for r in referrers_raw:
        referrers_by_site.setdefault(r["site"], []).append({"domain": r["referrer_domain"], "count": r["cnt"]})

    # Get country breakdown per site
    cur.execute("""
        SELECT site, country, COUNT(*) as cnt
        FROM analytics_pageviews
        WHERE ts >= %s AND ts < %s AND country IS NOT NULL AND country != ''
        GROUP BY site, country
        ORDER BY cnt DESC
    """, (hour_start, hour_end))
    countries_raw = cur.fetchall()
    countries_by_site = {}
    for r in countries_raw:
        countries_by_site.setdefault(r["site"], []).append({"country": r["country"], "count": r["cnt"]})

    # Get UA breakdown per site
    cur.execute("""
        SELECT site, ua_bucket, COUNT(*) as cnt
        FROM analytics_pageviews
        WHERE ts >= %s AND ts < %s AND ua_bucket IS NOT NULL
        GROUP BY site, ua_bucket
        ORDER BY cnt DESC
    """, (hour_start, hour_end))
    ua_raw = cur.fetchall()
    ua_by_site = {}
    for r in ua_raw:
        ua_by_site.setdefault(r["site"], {})[r["ua_bucket"]] = r["cnt"]

    # Upsert into analytics_hourly
    insert_cur = conn.cursor()
    site_views = {}
    for row in rows:
        site = row["site"]
        path = row["path"]
        key = (site, path)
        site_views.setdefault(site, 0)
        site_views[site] += row["views"]

        insert_cur.execute("""
            INSERT INTO analytics_hourly (hour, site, path, views, unique_visitors, avg_engagement_s, avg_scroll_pct, avg_response_ms, top_referrers, top_countries, ua_breakdown)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (hour, site, path) DO UPDATE SET
                views = EXCLUDED.views,
                unique_visitors = EXCLUDED.unique_visitors,
                avg_engagement_s = EXCLUDED.avg_engagement_s,
                avg_scroll_pct = EXCLUDED.avg_scroll_pct,
                avg_response_ms = EXCLUDED.avg_response_ms,
                top_referrers = EXCLUDED.top_referrers,
                top_countries = EXCLUDED.top_countries,
                ua_breakdown = EXCLUDED.ua_breakdown
        """, (
            hour_start,
            site,
            path,
            row["views"],
            row["unique_visitors"],
            engagement.get(key),
            scroll.get(key),
            int(row["avg_response_ms"]) if row["avg_response_ms"] else None,
            json.dumps(referrers_by_site.get(site, [])[:10]),
            json.dumps(countries_by_site.get(site, [])[:10]),
            json.dumps(ua_by_site.get(site, {})),
        ))

    conn.commit()
    insert_cur.close()
    cur.close()

    total = sum(r["views"] for r in rows)
    log(f"Aggregated {total} pageviews across {len(site_views)} sites for {hour_start}", level=LOG_INFO, source="analytics_agg")
    return site_views


def check_anomalies(conn, hour_start, site_views):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    alerts = []

    for site, current_views in site_views.items():
        # Get average hourly views for this site over last 7 days
        cur.execute("""
            SELECT AVG(views) as avg_views
            FROM analytics_hourly
            WHERE site = %s AND path IS NOT NULL
            AND hour >= %s - interval '7 days' AND hour < %s
        """, (site, hour_start, hour_start))
        row = cur.fetchone()
        avg_views = row["avg_views"] if row and row["avg_views"] else 0

        # Traffic spike
        if avg_views > 0 and current_views > avg_views * SPIKE_MULTIPLIER:
            alerts.append({
                "type": "traffic_spike",
                "site": site,
                "detail": {"current": current_views, "average": round(avg_views, 1), "multiplier": round(current_views / avg_views, 1)},
            })

    # Referrer bomb check
    cur.execute("""
        SELECT site, referrer_domain, COUNT(*) as cnt,
            COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (PARTITION BY site) as pct
        FROM analytics_pageviews
        WHERE ts >= %s AND ts < %s + interval '1 hour'
        AND referrer_domain IS NOT NULL AND referrer_domain != ''
        GROUP BY site, referrer_domain
        HAVING COUNT(*) > 20
        ORDER BY pct DESC
    """, (hour_start, hour_start))
    for row in cur.fetchall():
        if row["pct"] > REFERRER_BOMB_THRESHOLD * 100:
            alerts.append({
                "type": "referrer_bomb",
                "site": row["site"],
                "detail": {"domain": row["referrer_domain"], "count": row["cnt"], "pct": round(row["pct"], 1)},
            })

    # Site goes dark (only during daytime 06:00-23:00 local)
    hour_local = hour_start.astimezone().hour
    if 6 <= hour_local <= 23:
        for site in SITES:
            if site not in site_views or site_views[site] == 0:
                cur.execute("""
                    SELECT MAX(ts) as last_event FROM analytics_pageviews WHERE site = %s
                """, (site,))
                row = cur.fetchone()
                if row and row["last_event"]:
                    minutes_dark = (datetime.now(timezone.utc) - row["last_event"]).total_seconds() / 60
                    if minutes_dark > DARK_MINUTES:
                        alerts.append({
                            "type": "site_dark",
                            "site": site,
                            "detail": {"minutes_silent": round(minutes_dark)},
                        })

    cur.close()
    return alerts


def fire_alerts(conn, alerts):
    if not alerts:
        return
    cur = conn.cursor()
    for alert in alerts:
        cur.execute(
            "INSERT INTO analytics_alerts (alert_type, site, detail) VALUES (%s, %s, %s)",
            (alert["type"], alert.get("site"), json.dumps(alert.get("detail", {})))
        )

        # Slack notification
        emoji = {"traffic_spike": ":chart_with_upwards_trend:", "referrer_bomb": ":rotating_light:", "site_dark": ":ghost:"}.get(alert["type"], ":warning:")
        msg = f"{emoji} *Analytics Alert — {alert['type'].replace('_', ' ').title()}*\nSite: {alert.get('site', 'unknown')}\nDetail: {json.dumps(alert.get('detail', {}))}"
        try:
            nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)
        except Exception as e:
            log(f"Alert notification failed: {e}", level=LOG_ERROR, source="analytics_agg")

    conn.commit()
    cur.close()
    log(f"Fired {len(alerts)} analytics alerts", level=LOG_WARN, source="analytics_agg")


def run():
    log("Analytics aggregation starting...", level=LOG_INFO, source="analytics_agg")
    conn = get_conn()

    now = datetime.now(timezone.utc)
    hour_end = now.replace(minute=0, second=0, microsecond=0)
    hour_start = hour_end - timedelta(hours=1)

    site_views = aggregate_hour(conn, hour_start, hour_end)

    if site_views:
        alerts = check_anomalies(conn, hour_start, site_views)
        fire_alerts(conn, alerts)

    # Retention: delete hourly aggregates older than 2 years
    cur = conn.cursor()
    cur.execute("DELETE FROM analytics_hourly WHERE hour < now() - interval '2 years'")
    cur.execute("DELETE FROM analytics_alerts WHERE ts < now() - interval '90 days'")
    conn.commit()
    cur.close()

    conn.close()
    log("Analytics aggregation complete", level=LOG_INFO, source="analytics_agg")


if __name__ == "__main__":
    run()
