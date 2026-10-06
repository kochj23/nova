#!/usr/bin/env python3
"""nova_network_health.py — WEEKLY Network-Health review (Sundays 08:40).

The over-time counterpart to nova_watchtower.py (the real-time sentinel): instead of
"what's down right now?", this asks "what's been FLAKY over the past week?" — reading the
net_liveness time-series + net_problems episodes watchtower records. Per-device uptime,
per-FEED reliability, chronic offenders, longest outages, new/vanished devices. Two
outputs, same pattern as the alert-patterns / local-trends weeklies:
  * FULL detail  -> Slack #nova-alerts (names, percentages).
  * SANITIZED narrative -> public /operations column (patterns + magnitudes, Nova's voice;
    no internal IPs/hostnames). cron 40 8 * * 0.
"""
import sys
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
import nova_journal as nj
import nova_voice
from nova_image_utils import generate_image

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"


def log(m):
    print(f"[net-health {datetime.now():%H:%M:%S}] {m}", flush=True)


def _q(sql, one=False):
    try:
        c = psycopg2.connect(DSN)
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql)
        rows = cur.fetchall()
        c.close()
        return (dict(rows[0]) if rows else {}) if one else [dict(r) for r in rows]
    except Exception as e:
        log(f"query failed: {e}")
        return {} if one else []


def gather():
    m = {}
    m["tiers"] = _q("SELECT tier, count(*) AS n FROM telemetry.net_inventory GROUP BY tier ORDER BY 2 DESC")
    # worst device uptime over the week (monitored, wired tiers)
    m["worst_devices"] = _q("""
        SELECT name, tier,
               round(100.0*count(*) FILTER (WHERE online)/nullif(count(*),0),1) AS pct,
               count(*) AS n
        FROM telemetry.net_liveness
        WHERE ts > now()-interval '7 days' AND tier IN ('infra','coordinator','camera')
        GROUP BY name, tier HAVING count(*) >= 3
        ORDER BY pct ASC NULLS LAST LIMIT 12
    """)
    # per-feed reliability
    m["feeds"] = _q("""
        SELECT name,
               round(100.0*count(*) FILTER (WHERE online)/nullif(count(*),0),1) AS pct,
               count(*) AS n
        FROM telemetry.net_liveness
        WHERE ts > now()-interval '7 days' AND tier='feed'
        GROUP BY name ORDER BY pct ASC NULLS LAST
    """)
    m["open_problems"] = _q("""
        SELECT entity, tier, detail,
               round(EXTRACT(EPOCH FROM (now()-opened_at))/3600,1) AS hours_open
        FROM telemetry.net_problems WHERE status='open' ORDER BY opened_at
    """)
    m["resolved"] = _q("""
        SELECT entity, tier,
               round(EXTRACT(EPOCH FROM (cleared_at-opened_at))/3600,1) AS hours_down
        FROM telemetry.net_problems
        WHERE status='cleared' AND cleared_at > now()-interval '7 days'
        ORDER BY hours_down DESC LIMIT 10
    """)
    m["new_devices"] = _q("""
        SELECT name, tier FROM telemetry.net_inventory
        WHERE first_seen > now()-interval '7 days'
          AND tier NOT IN ('transient') AND coalesce(name,'') NOT IN ('','(unnamed)')
        ORDER BY first_seen DESC LIMIT 15
    """)
    m["vanished"] = _q("""
        SELECT name, tier, last_online::date AS since FROM telemetry.net_inventory
        WHERE tier IN ('infra','coordinator','camera') AND last_online IS NOT NULL
          AND last_online < now()-interval '2 days' ORDER BY last_online DESC LIMIT 10
    """)
    return m


def render_slack(m) -> str:
    L = ["*Network Health — 7-day review* (full detail)"]
    op = m.get("open_problems", [])
    L.append(f"\n*Currently open:* {len(op)}" + ("" if not op else ""))
    for p in op[:12]:
        L.append(f"  🔴 {p['entity']} ({p['tier']}) — {p['detail']} [{p['hours_open']}h]")
    fe = m.get("feeds", [])
    L.append("\n*Feed reliability (uptime % this week):*")
    for f in fe:
        icon = "🟢" if (f["pct"] or 0) >= 99 else ("🟡" if (f["pct"] or 0) >= 80 else "🔴")
        L.append(f"  {icon} {f['name']}: {f['pct']}% ({f['n']} samples)")
    wd = m.get("worst_devices", [])
    if wd:
        L.append("\n*Least-reliable devices:*")
        for d in wd[:10]:
            L.append(f"  • {d['name']} ({d['tier']}): {d['pct']}% up")
    rv = m.get("resolved", [])
    if rv:
        L.append("\n*Recovered this week (downtime):* " +
                 ", ".join(f"{r['entity']} {r['hours_down']}h" for r in rv[:8]))
    if m.get("new_devices"):
        L.append("\n*New devices:* " + ", ".join(f"{d['name']}({d['tier']})" for d in m["new_devices"][:10]))
    if m.get("vanished"):
        L.append("*Vanished (monitored, silent >2d):* " +
                 ", ".join(f"{d['name']} since {d['since']}" for d in m["vanished"][:8]))
    return "\n".join(L)


def _sanitized_brief(m) -> str:
    fe = m.get("feeds", [])
    healthy = [f for f in fe if (f["pct"] or 0) >= 99]
    flaky = [f for f in fe if (f["pct"] or 0) < 99]
    lines = [f"Data feeds tracked: {len(fe)}. Rock-solid (>=99% this week): {len(healthy)}. "
             f"Flaky or dark: {len(flaky)}."]
    for f in flaky[:8]:
        band = "totally dark" if (f["pct"] or 0) < 5 else ("badly flaky" if (f["pct"] or 0) < 80 else "occasionally dropping")
        lines.append(f"  - a '{f['name'].split(':')[0]}' data feed, {band} ({f['pct']}% of the week healthy)")
    op = m.get("open_problems", [])
    lines.append(f"Open problems right now: {len(op)} (mix of dropped devices and stale feeds).")
    rv = m.get("resolved", [])
    if rv:
        lines.append(f"Recovered this week: {len(rv)}, worst had ~{rv[0]['hours_down']} hours of downtime.")
    tiers = {t["tier"]: t["n"] for t in m.get("tiers", [])}
    lines.append(f"Fleet size on the network: ~{sum(tiers.values())} devices "
                 f"({tiers.get('infra',0)} infra, {tiers.get('coordinator',0)} hubs, "
                 f"{tiers.get('camera',0)} cameras, {tiers.get('smart_home',0)} smart-home).")
    if m.get("vanished"):
        lines.append(f"{len(m['vanished'])} monitored device(s) have gone silent for over two days.")
    return "\n".join(lines)


def generate_article(m):
    system = nova_voice.system_prompt(
        "You are writing your WEEKLY NETWORK-HEALTH review for the public /operations page. This is "
        "RELIABILITY analysis over the past week — which of your data feeds and devices held up and "
        "which kept dropping — NOT a live outage list. Read the brief and tell the reader the SHAPE of "
        "the reliability: what's rock-solid, what's chronically flaky, what silently died, and whether "
        "the week was better or worse. Be honest and a little tired under the snark — you are the "
        "machine grading your own nervous system. 700-1100 words. PUBLIC: never invent or expose "
        "internal IPs, hostnames, or device names — speak in feed types, tiers, and magnitudes. Do NOT "
        "print a title or date line.", section="operations")
    hist = ""
    try:
        import nova_article_history
        hist = nova_article_history.recent_articles_context("operations")
    except Exception:
        pass
    user = f"Here is this week's network-health brief:\n\n{_sanitized_brief(m)}"
    if hist:
        user += f"\n\n{hist}"
    body = nj.call_openrouter(system, user).strip()
    title = nj.call_openrouter(
        "Generate one sharp, funny, tired title for a column where an AI grades the reliability of its "
        "OWN sensor network and data feeds over a week. Max 13 words. Output ONLY the title, no quotes.",
        body[:800]).strip().strip('"').replace('"', '')
    return title, body


def main():
    log("gathering network health")
    m = gather()
    try:
        nova_config.post_both(render_slack(m), slack_channel=nova_config.SLACK_ALERTS, discord_channel=None)
        log("posted full detail to #nova-alerts")
    except Exception as e:
        log(f"slack post failed: {e}")
    try:
        title, body = generate_article(m)
        log(f"article: {title} ({len(body)} chars)")
        img = None
        try:
            img = generate_image(
                "A tired AI reviewing a wall of uptime dashboards and reliability graphs for its own home "
                "sensor network — some feeds glowing steady green, several flatlined dark, a few flickering "
                "amber. Moody control-room lighting, data-viz aesthetic, cyberpunk-lite, green and amber, "
                "no text.", section="operations")
        except Exception as e:
            log(f"image gen failed: {e}")
        nj.publish_hugo(title, body, "operations",
                        ["ops", "network", "reliability", "uptime", "weekly"],
                        "Nova's weekly reliability report card on her own network and data feeds.",
                        image_path=img, emoji="📶", sources=_sanitized_brief(m),
                        profile="network-health")
        nj.git_push("operations", title)
        log(f"published to /operations (image: {'yes' if img else 'none'})")
    except Exception as e:
        log(f"article publish failed: {e}")
    log("done")


if __name__ == "__main__":
    sys.exit(main())
