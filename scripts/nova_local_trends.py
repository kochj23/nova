#!/usr/bin/env python3
"""nova_local_trends.py — WEEKLY "Local Trends" review (Sundays 08:35 -> /local).

The local counterpart to the weekly alert-patterns review: step back from the daily
blotter and report the SHAPE of the neighborhood over the past two weeks — LoRa mesh
growth/churn, the RF neighborhood (new/open APs, incl. the rogue-AP sentinel), and
overhead-flight activity, week-over-week. One public /local article in Nova's voice.
(The DAILY local columns already avoid goldfish-memory repetition via the 14-day
self-history; this is the weekly zoom-out.) Reads existing telemetry. cron 35 8 * * 0.
"""
import sys
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice
try:
    import nova_rogue_ap_sentinel as sentinel
except Exception:
    sentinel = None

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"


def log(m):
    print(f"[local-trends {datetime.now():%H:%M:%S}] {m}", flush=True)


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


def _delta(this, last):
    if not last:
        return "no prior-week baseline"  # e.g. LoRa mesh_nodes retains ~1wk; don't imply all are 'new'
    d = this - last
    return f"{'up' if d>0 else ('down' if d<0 else 'flat')} {abs(d)} ({'+' if d>=0 else ''}{round(100*d/last)}%)"


def gather_local_trends() -> dict:
    p = {}
    # LoRa mesh — distinct nodes heard this week vs last, currently active, brand new.
    p["lora"] = _q("""
        SELECT count(distinct node_id) FILTER (WHERE ts > now()-interval '7 days')  AS this_week,
               count(distinct node_id) FILTER (WHERE ts <= now()-interval '7 days') AS last_week,
               count(distinct node_id) FILTER (WHERE last_heard > now()-interval '24 hours') AS active_24h
        FROM telemetry.mesh_nodes WHERE ts > now()-interval '14 days'
    """, one=True)
    p["lora_new"] = _q("""
        SELECT long_name, min(ts)::date AS since FROM telemetry.mesh_nodes
        GROUP BY long_name HAVING min(ts) > now()-interval '7 days'
        ORDER BY 2 DESC LIMIT 8
    """)
    # RF neighborhood — distinct SSIDs, open APs, and what NEWLY appeared this week.
    p["rf"] = _q("""
        SELECT count(distinct ssid) FILTER (WHERE ts > now()-interval '7 days')  AS ssids_this_week,
               count(distinct ssid) FILTER (WHERE ts <= now()-interval '7 days') AS ssids_last_week,
               count(distinct bssid) FILTER (WHERE security='Open' AND ts > now()-interval '7 days') AS open_aps
        FROM wifi_aps WHERE ts > now()-interval '14 days'
    """, one=True)
    p["rf_new_ssids"] = _q("""
        SELECT ssid, min(ts)::date AS since FROM wifi_aps
        WHERE ssid IS NOT NULL AND ssid <> ''
        GROUP BY ssid HAVING min(ts) > now()-interval '7 days'
        ORDER BY 2 DESC LIMIT 10
    """)
    p["rogue_flags"] = sentinel.get_current_flags() if sentinel else []
    # Overhead flights — volume this week vs last.
    p["flights"] = _q("""
        SELECT count(*) FILTER (WHERE ts > now()-interval '7 days')  AS this_week,
               count(*) FILTER (WHERE ts <= now()-interval '7 days') AS last_week
        FROM telemetry.events WHERE category='flights' AND ts > now()-interval '14 days'
    """, one=True)
    # Airwaves (scanner/police/fire/rail) — best-effort via the local generator's own
    # fetcher, which knows the separate memories store. Degrade silently if unavailable.
    try:
        import nova_local_burbank as lb
        if hasattr(lb, "get_airwaves_summary"):
            p["airwaves"] = lb.get_airwaves_summary()
    except Exception:
        pass
    return p


def _brief(p) -> str:
    lora, rf, fl = p.get("lora", {}), p.get("rf", {}), p.get("flights", {})
    L = [f"LoRa mesh: {lora.get('this_week',0)} distinct nodes heard this week ({_delta(lora.get('this_week',0), lora.get('last_week',0))} vs last), "
         f"{lora.get('active_24h',0)} active in the last 24h."]
    if p.get("lora_new"):
        L.append("New LoRa nodes this week: " + ", ".join(f"{n['long_name']}" for n in p["lora_new"][:8] if n.get("long_name")) + ".")
    L.append(f"RF neighborhood: {rf.get('ssids_this_week',0)} distinct WiFi networks in range "
             f"({_delta(rf.get('ssids_this_week',0), rf.get('ssids_last_week',0))} vs last), {rf.get('open_aps',0)} of them OPEN.")
    if p.get("rf_new_ssids"):
        L.append("New networks that appeared this week: " + ", ".join(f"'{s['ssid']}'" for s in p["rf_new_ssids"][:8]) + ".")
    rfl = p.get("rogue_flags", [])
    L.append(f"Rogue/your-own open APs currently flagged: {len(rfl)}" + ("" if not rfl else " — " + "; ".join(f"{f['ssid']} ({f['kind']})" for f in rfl)) + ".")
    L.append(f"Overhead: {fl.get('this_week',0)} flight events tracked this week ({_delta(fl.get('this_week',0), fl.get('last_week',0))} vs last).")
    if p.get("airwaves"):
        L.append(f"Airwaves: {p['airwaves']}")
    return "\n".join(L)


def generate_article(p):
    system = nova_voice.system_prompt(
        "You are writing your WEEKLY LOCAL TRENDS review for the public /local page — Burbank / LA. "
        "This is TREND analysis over the past two weeks, NOT a single day's blotter: what's the shape "
        "of the neighborhood? The LoRa mesh (is it growing, who joined), the RF neighborhood (new "
        "networks, open/misconfigured APs — including our OWN gear when it misbehaves), and what's "
        "overhead. Read the brief and tell the reader the PATTERNS and what changed week-over-week — "
        "call out anything genuinely new or rising. 700-1100 words. Do NOT print a title or date line.",
        section="local")
    hist = ""
    try:
        import nova_article_history
        hist = nova_article_history.recent_articles_context("local")
    except Exception:
        pass
    user = f"Here is today's local-trends brief:\n\n{_brief(p)}"
    if hist:
        user += f"\n\n{hist}"
    body = nj.call_openrouter(system, user).strip()
    title = nj.call_openrouter(
        "Generate one sharp, funny title for a column about LOCAL neighborhood TRENDS (LoRa mesh, "
        "WiFi, overhead flights) in Burbank/LA. Max 13 words. Output ONLY the title, no quotes.",
        body[:800]).strip().strip('"').replace('"', '')
    return title, body


def main():
    log("gathering local trends")
    p = gather_local_trends()
    try:
        title, body = generate_article(p)
        log(f"article: {title} ({len(body)} chars)")
        nj.publish_hugo(title, body, "local",
                        ["local", "trends", "burbank", "lora", "rf", "daily"],
                        "Nova's rolling two-week read on the neighborhood — the mesh, the airwaves, and what's overhead.",
                        emoji="📡")
        log("published to /local")
    except Exception as e:
        log(f"publish failed: {e}")
    log("done")


if __name__ == "__main__":
    sys.exit(main())
