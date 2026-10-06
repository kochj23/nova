#!/usr/bin/env python3
"""nova_alert_patterns.py — WEEKLY "alert patterns" review (Sundays 08:30).

Not another firehose of individual alerts — this steps back and reports the PATTERNS
over a rolling 14 days: what keeps firing (chronic vs noise), which incidents recur,
week-over-week trend, and the red/blue/purple picture. Two outputs:
  * FULL detail  -> Slack #nova-alerts (numbers, signatures, hosts — private).
  * SANITIZED narrative -> public /operations column (high-level patterns + Nova's
    take, with internal IPs/hostnames/red-team specifics stripped).

Reads existing telemetry (telemetry.events / incidents, security_scan_results, the
wazuh/strix/purple event streams, known_devices) + the rogue-AP sentinel. Scheduled
cron 30 8.
"""
import json
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
try:
    import nova_rogue_ap_sentinel as sentinel
except Exception:
    sentinel = None

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"


def log(m):
    print(f"[alert-patterns {datetime.now():%H:%M:%S}] {m}", flush=True)


def _q(sql, params=None, one=False):
    try:
        c = psycopg2.connect(DSN)
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        c.close()
        return (dict(rows[0]) if rows else {}) if one else [dict(r) for r in rows]
    except Exception as e:
        log(f"query failed: {e}")
        return {} if one else []


def gather_patterns() -> dict:
    p = {}
    # 1) Recurring alert signatures over 14d, with this-week vs last-week (the core).
    p["recurring"] = _q("""
        SELECT coalesce(dedup_key, category, 'uncategorized') AS sig,
               count(*) FILTER (WHERE ts > now()-interval '7 days')  AS this_week,
               count(*) FILTER (WHERE ts <= now()-interval '7 days') AS last_week,
               count(*) AS total
        FROM telemetry.events
        WHERE ts > now()-interval '14 days' AND level IN ('warning','critical','error')
        GROUP BY 1 ORDER BY total DESC LIMIT 12
    """)
    # 2) Overall alert volume trend
    p["volume"] = _q("""
        SELECT count(*) FILTER (WHERE ts > now()-interval '7 days')  AS this_week,
               count(*) FILTER (WHERE ts <= now()-interval '7 days') AS last_week
        FROM telemetry.events
        WHERE ts > now()-interval '14 days' AND level IN ('warning','critical','error')
    """, one=True)
    # 3) Incidents
    p["incidents"] = _q("""
        SELECT count(*) FILTER (WHERE status='open') AS open_now,
               count(*) FILTER (WHERE opened_at > now()-interval '7 days') AS opened_7d,
               count(*) FILTER (WHERE resolved_at > now()-interval '7 days') AS resolved_7d,
               round(avg(mttr_s) FILTER (WHERE resolved_at > now()-interval '7 days')/60.0) AS avg_mttr_min
        FROM telemetry.incidents
    """, one=True)
    p["incident_recurrence"] = _q("""
        SELECT recurrence_key, count(*) AS n, max(severity) AS sev
        FROM telemetry.incidents
        WHERE opened_at > now()-interval '14 days' AND recurrence_key IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 6
    """)
    # 4) Red (Strix pentest)
    p["red"] = _q("""
        SELECT scan_type, status, count(*) AS n, max(scan_time)::date AS last
        FROM security_scan_results WHERE scan_time > now()-interval '14 days'
        GROUP BY 1,2 ORDER BY 3 DESC LIMIT 8
    """)
    # 5) Blue (Wazuh) — ILIKE pattern as a PARAM (a literal % in the SQL string is a
    # psycopg2 placeholder and blows up with 'tuple index out of range').
    p["blue"] = _q("SELECT count(*) AS events, max(to_char(ts,'MM-DD')) AS last "
                   "FROM telemetry.events WHERE source ILIKE %s AND ts > now()-interval '14 days'",
                   ("%wazuh%",), one=True)
    # 6) Purple (detection-validation)
    p["purple"] = _q("SELECT title, left(body,300) AS body, to_char(ts,'MM-DD') AS d "
                     "FROM telemetry.events WHERE source ILIKE %s ORDER BY ts DESC LIMIT 1",
                     ("%purple%",), one=True)
    # 7) Network: new IDENTIFIABLE devices (skip unnamed DHCP-randomized noise) + rogue-AP flags
    p["new_devices"] = _q("SELECT client_name, ip FROM telemetry.known_devices "
                          "WHERE first_seen > now()-interval '7 days' "
                          "AND coalesce(client_name,'') NOT ILIKE 'unknown' AND coalesce(client_name,'') <> '' "
                          "ORDER BY first_seen DESC LIMIT 15")
    p["rogue_flags"] = sentinel.get_current_flags() if sentinel else []
    return p


def _delta(this, last):
    if not last:
        return "new" if this else "—"
    d = this - last
    arrow = "▲" if d > 0 else ("▼" if d < 0 else "▬")
    return f"{arrow}{abs(d)} ({'+' if d>=0 else ''}{round(100*d/last)}%)"


def render_slack(p) -> str:
    L = ["*Alert Patterns — 14-day rolling* (full detail)"]
    v = p.get("volume", {})
    L.append(f"\n*Volume:* {v.get('this_week',0)} warning+ this week vs {v.get('last_week',0)} last "
             f"({_delta(v.get('this_week',0), v.get('last_week',0))})")
    L.append("\n*Chronic signatures* (the ones that keep firing):")
    for r in p.get("recurring", [])[:10]:
        L.append(f"  • `{r['sig']}` — {r['total']} in 14d "
                 f"[{r['this_week']} this wk, {_delta(r['this_week'], r['last_week'])}]")
    inc = p.get("incidents", {})
    L.append(f"\n*Incidents:* {inc.get('open_now',0)} open | {inc.get('opened_7d',0)} opened / "
             f"{inc.get('resolved_7d',0)} resolved this week | avg MTTR {inc.get('avg_mttr_min','?')} min")
    if p.get("incident_recurrence"):
        L.append("  recurring: " + ", ".join(f"{r['recurrence_key']}×{r['n']}" for r in p['incident_recurrence'][:5]))
    L.append("\n*Red/Blue/Purple:*")
    L.append("  red (Strix): " + (", ".join(f"{r['scan_type']}:{r['status']}×{r['n']}" for r in p.get('red', [])[:5]) or "no scans"))
    L.append(f"  blue (Wazuh): {p.get('blue',{}).get('events',0)} events (14d, last {p.get('blue',{}).get('last','?')})")
    pur = p.get("purple", {})
    L.append(f"  purple: {pur.get('title','(no run)')} [{pur.get('d','')}]")
    nd = p.get("new_devices", [])
    L.append(f"\n*Network:* {len(nd)} new device(s) this week" + (": " + ", ".join(f"{d['client_name']}({d['ip']})" for d in nd[:8]) if nd else ""))
    rf = p.get("rogue_flags", [])
    L.append(f"*Rogue/open APs:* {len(rf)} flagged" + ("" if not rf else " ⚠️ " + "; ".join(f"{f['kind']}:{f['ssid']}" for f in rf)))
    return "\n".join(L)


def _sanitized_brief(p) -> str:
    """High-level, attacker-useless view for the PUBLIC article: generalize signatures,
    drop IPs/hostnames/red-team specifics; keep the shape of the patterns and the trend."""
    v = p.get("volume", {})
    lines = [f"Warning-level+ alerts this week vs last: {v.get('this_week',0)} vs {v.get('last_week',0)} "
             f"({_delta(v.get('this_week',0), v.get('last_week',0))} trend)."]
    lines.append("Most chronic alert themes (name generalized, counts as magnitude):")
    for r in p.get("recurring", [])[:8]:
        theme = r["sig"].split(":")[0].split("-")[0]  # generalize (e.g. backup-stale:x -> backup)
        band = "hundreds" if r["total"] >= 200 else ("dozens" if r["total"] >= 30 else "a handful")
        trend = "rising" if r["this_week"] > r["last_week"] else ("easing" if r["this_week"] < r["last_week"] else "flat")
        lines.append(f"  - a recurring '{theme}' alert, {band} of times, {trend} week-over-week")
    inc = p.get("incidents", {})
    lines.append(f"Incidents: {inc.get('open_now',0)} open; {inc.get('opened_7d',0)} opened and "
                 f"{inc.get('resolved_7d',0)} resolved this week; typical time-to-resolve ~{inc.get('avg_mttr_min','?')} min.")
    lines.append(f"Security posture: red-team (automated pentest) and blue-team (SIEM) both running; "
                 f"purple-team detection-validation last reported: {p.get('purple',{}).get('title','n/a')}.")
    lines.append(f"Network: {len(p.get('new_devices',[]))} new device(s) joined this week; "
                 f"{len(p.get('rogue_flags',[]))} open/rogue AP(s) currently flagged.")
    return "\n".join(lines)


def generate_article(p):
    system = nova_voice.system_prompt(
        "You are writing your WEEKLY ALERT-PATTERNS review for the public /operations page. This is "
        "PATTERN ANALYSIS over the past two weeks — NOT a list of individual alerts. Read the "
        "aggregated brief, then tell the reader what the SHAPE of the noise is: what's chronically "
        "firing (and whether it's a real problem or just noise that should be tuned out), what's "
        "trending up or down, what the incident/security cadence looks like. Be honest and analytical "
        "UNDER the snark — call out the alerts that are clearly just crying wolf. 700-1100 words. This "
        "is PUBLIC: never invent or expose internal IPs, hostnames, device names, or specific "
        "vulnerabilities — speak in patterns and magnitudes, which is all the brief gives you. Do NOT "
        "print a title or date line.",
        section="operations")
    body = nj.call_openrouter(system, f"Here is today's aggregated alert-pattern brief:\n\n{_sanitized_brief(p)}").strip()
    title = nj.call_openrouter(
        "Generate one sharp, funny, ironic title for a column about recurring infrastructure ALERT "
        "PATTERNS — dry and a little tired of the same alerts. Max 13 words. Output ONLY the title, no quotes.",
        body[:800]).strip().strip('"').replace('"', '')
    return title, body


def main():
    log("gathering alert patterns")
    p = gather_patterns()
    # 1) full detail -> Slack #nova-alerts
    try:
        nova_config.post_both(render_slack(p), slack_channel=nova_config.SLACK_ALERTS, discord_channel=None)
        log("posted full digest to #nova-alerts")
    except Exception as e:
        log(f"slack post failed: {e}")
    # 2) sanitized narrative -> public /operations
    try:
        title, body = generate_article(p)
        log(f"article: {title} ({len(body)} chars)")
        img = None
        try:
            img = generate_image(
                "A tired AI operator at a wall of blinking alert dashboards, most alarms clearly false, "
                "a few genuinely red, week-over-week trend lines glowing behind. Moody control-room "
                "lighting, data-viz aesthetic, cyberpunk-lite, amber and red, no text.", section="operations")
        except Exception as e:
            log(f"image gen failed: {e}")
        nj.publish_hugo(title, body, "operations",
                        ["ops", "alerts", "patterns", "security", "weekly"],
                        "Nova's weekly read on what the alerts are actually saying — chronic noise vs real signal.",
                        image_path=img, emoji="🚨", sources=_sanitized_brief(p),
                        profile="alert-patterns")
        nj.git_push("operations", title)   # commit + push the article AND its image (was missing)
        log(f"published sanitized article to /operations (image: {'yes' if img else 'none'})")
    except Exception as e:
        log(f"article publish failed: {e}")
    log("done")


if __name__ == "__main__":
    sys.exit(main())
