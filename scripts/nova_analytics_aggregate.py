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
from nova_notify import notify

PG_DSN = "host=192.168.1.6 dbname=nova_ops user=kochj"

# chat.digitalnoise.net is the public TinyChat route: it legitimately returns 404
# at "/" and generates ~no analytics pageviews, so it would ALWAYS look "quiet".
# It is intentionally NOT in the dark-site watchlist — its uptime is covered by
# the TinyChat service check in nova_big_brother / nova_watchdog, not by pageviews.
SITES = ["nova.digitalnoise.net", "digitalnoise.net", "gauges.digitalnoise.net"]
SPIKE_MULTIPLIER = 10
REFERRER_BOMB_THRESHOLD = 0.5
DARK_MINUTES = 30

# Per-site silence thresholds — low-traffic sites get longer grace periods
# before alerting. "expected_quiet_hours" = hours of zero traffic that are normal.
SITE_DARK_CONFIG = {
    "nova.digitalnoise.net": {"threshold_min": 1440, "description": "Nova's public journal — low traffic is normal, only alert after 24h"},
    "digitalnoise.net": {"threshold_min": 1440, "description": "Personal site — low traffic is normal, only alert after 24h"},
    "chat.digitalnoise.net": {"threshold_min": 120, "description": "Active chat interface — silence over 2h during daytime is unusual"},
    "gauges.digitalnoise.net": {"threshold_min": 1440, "description": "Dashboard — internal use only, long silence is normal"},
}


def _probe_site(site: str) -> bool:
    """Check if a site is actually reachable.

    A site is "reachable" if it answers with any non-5xx HTTP status — a 200, a
    redirect, or even a 404 means the server is up and serving; only a 5xx or a
    connection/timeout failure means it's actually down.

    Tries HEAD first (cheap), then falls back to GET, because some servers/CDNs
    don't implement HEAD and return 405/403 or close the connection — that must
    NOT be misread as "site down" (which would fire a false critical). A
    urllib HTTPError still carries a real status code, so we honour it.
    """
    import urllib.request
    import urllib.error
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(f"https://{site}/", method=method)
            req.add_header("User-Agent", "Nova-Analytics/1.0")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status < 500
        except urllib.error.HTTPError as e:
            # Got a real HTTP response (e.g. 404/403/405) — server is up.
            return e.code < 500
        except Exception:
            continue  # try the next method before declaring it unreachable
    return False


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
        -- only cast valid numbers; a malformed client value like "1.2.3" (multiple
        -- decimal points) otherwise crashes the ENTIRE aggregation query (#631).
        AND event_data->>'seconds' ~ '^-?[0-9]+([.][0-9]+)?$'
        GROUP BY site, path
    """, (hour_start, hour_end))
    engagement = {(r["site"], r["path"]): r["avg_engagement_s"] for r in cur.fetchall()}

    # Get scroll depth
    cur.execute("""
        SELECT site, path,
            AVG((event_data->>'depth')::float) as avg_scroll_pct
        FROM analytics_events
        WHERE ts >= %s AND ts < %s AND event_type = 'scroll'
        -- only cast valid numbers (see #631 — malformed "1.2.3" crashes the query)
        AND event_data->>'depth' ~ '^-?[0-9]+([.][0-9]+)?$'
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
        # AVG() returns a Decimal; cast to float so alert detail is JSON-serializable.
        avg_views = float(row["avg_views"]) if row and row["avg_views"] else 0.0

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

    # Site goes dark — per-site thresholds, HTTP probe to distinguish "quiet" from "down"
    hour_local = hour_start.astimezone().hour
    if 6 <= hour_local <= 23:
        for site in SITES:
            if site not in site_views or site_views[site] == 0:
                site_cfg = SITE_DARK_CONFIG.get(site, {"threshold_min": DARK_MINUTES})
                cur.execute("""
                    SELECT MAX(ts) as last_event FROM analytics_pageviews WHERE site = %s
                """, (site,))
                row = cur.fetchone()
                if row and row["last_event"]:
                    minutes_dark = (datetime.now(timezone.utc) - row["last_event"]).total_seconds() / 60
                    if minutes_dark > site_cfg["threshold_min"]:
                        # HTTP probe to check if site is actually reachable
                        site_up = _probe_site(site)
                        status = "up but no visitors" if site_up else "UNREACHABLE"
                        hours_dark = round(minutes_dark / 60, 1)
                        alerts.append({
                            "type": "site_dark",
                            "site": site,
                            "detail": {
                                "minutes_silent": round(minutes_dark),
                                "hours_silent": hours_dark,
                                "site_reachable": site_up,
                                "status": status,
                                "explanation": f"Zero pageviews for {hours_dark}h. Site is {status}.",
                            },
                        })

    cur.close()
    return alerts


def _json_default(o):
    """Make psycopg2 Decimals (from AVG/pct SQL exprs) JSON-serializable."""
    from decimal import Decimal
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def fire_alerts(conn, alerts):
    if not alerts:
        return
    # An UNREACHABLE site is an ongoing condition — re-page no more than once per
    # this window even though aggregation runs hourly. (The central notifier only
    # dedups for 1h, which let an hourly run re-fire endlessly for days.)
    UNREACHABLE_COOLDOWN_H = 12

    cur = conn.cursor()
    for alert in alerts:
        cur.execute(
            "INSERT INTO analytics_alerts (alert_type, site, detail) VALUES (%s, %s, %s)",
            (alert["type"], alert.get("site"), json.dumps(alert.get("detail", {}), default=_json_default))
        )

        # Central notification bus: declare intent (level + category), not a channel.
        site = alert.get("site", "unknown")
        if alert["type"] == "site_dark":
            detail = alert.get("detail", {})
            reachable = detail.get("site_reachable")
            # A reachable site (HTTP probe OK) with no pageviews is NORMAL for a
            # low-traffic personal/internal site — it is informational, not an
            # incident, so route it to #nova-info and never to #nova-warning.
            # Only an UNREACHABLE site is a genuine outage worth a critical alert.
            # (Previously reachable-but-quiet was "warning", which made healthy
            # low-traffic sites like digitalnoise.net cry wolf on #nova-warning.)
            level = "info" if reachable else "critical"
            if reachable:
                title = f"Site Quiet — {site}"
            else:
                # SITE/host prominent and the failure mode explicit in the title.
                title = f"Site UNREACHABLE — {site}"
                # De-dup the ongoing outage: skip the notify (DB row already logged
                # above) if we already paged for this same unreachable site recently.
                cur.execute(
                    "SELECT ts FROM analytics_alerts "
                    "WHERE alert_type='site_dark' AND site=%s "
                    "AND (detail->>'site_reachable')::bool IS FALSE "
                    "AND ts < now() "
                    "AND ts > now() - make_interval(hours => %s) "
                    "ORDER BY ts DESC LIMIT 1",
                    (site, UNREACHABLE_COOLDOWN_H))
                if cur.fetchone():
                    log(f"site_dark/{site} UNREACHABLE — suppressed (within "
                        f"{UNREACHABLE_COOLDOWN_H}h cooldown)",
                        level=LOG_INFO, source="analytics_agg")
                    continue
            body = (
                f"{detail.get('explanation', 'No pageviews detected')}\n"
                f"HTTP probe: {'reachable' if reachable else 'UNREACHABLE — possible outage'}\n"
                f"Silent for: {detail.get('hours_silent', '?')}h"
            )
        else:
            level = "warning"
            title = f"Analytics Alert — {alert['type'].replace('_', ' ').title()}"
            body = (
                f"Site: {site}\n"
                f"Detail: {json.dumps(alert.get('detail', {}), default=_json_default)}"
            )
        notify(
            title,
            body=body,
            level=level,
            category="analytics",
            dedup_key=f"analytics-{alert['type']}-{site}",
            meta={"host": "studio", "site": site},
        )

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
