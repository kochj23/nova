#!/usr/bin/env python3
"""nova_geo_alerts.py [--dry] — real-time proximity + convergence alerts for nearby incidents.

Runs every few minutes. On NEW distance-enriched scanner/fire transmissions it checks:
  • PROXIMITY : a SERIOUS incident (fire/structure/pursuit/weapon/rescue/shots) within ALERT_MI.
  • CONVERGENCE: police AND fire BOTH active within CONV_MI in a short window (+ an LAPD helicopter
    overhead) -> likely a major incident near home.
Dedupes per incident, tracks a last-seen cursor so each transmission is considered once, and posts
to Slack via nova_notify. Home = 508 S Glenwood Pl, Burbank. `--dry` prints instead of posting.
"""
import re
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
from nova_notify import notify

MEM_DSN = "host=localhost dbname=nova_memories user=kochj"
OPS_DSN = "host=localhost dbname=nova_ops user=kochj"
STATE = "/tmp/nova_geo_alerts.last"
ALERT_MI = 2.5        # a serious incident within this radius pings (walkable)
CONV_MI = 2.5         # convergence radius
WINDOW_MIN = 20       # convergence look-back window

SERIOUS = re.compile(
    r"structure|working fire|greater alarm|fully involved|\brescue\b|extricat|trapped|explosion|"
    r"\bpursuit\b|shots fired|\bshooting\b|\bADW\b|\bCDW\b|barricad|hostage|officer needs|"
    r"stabbing|\barmed\b", re.I)

DIRDEG = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}


def _wind_dir():
    """Current wind direction in degrees (meteorological — where the wind comes FROM), or None."""
    try:
        c = psycopg2.connect(OPS_DSN); c.autocommit = True; cur = c.cursor()
        cur.execute("SELECT wind_dir FROM telemetry.weather WHERE wind_dir IS NOT NULL "
                    "ORDER BY ts DESC LIMIT 1")
        r = cur.fetchone(); c.close()
        return float(r[0]) if r else None
    except Exception:
        return None


def _upwind(incident_dir, wind_dir):
    """True if a fire in incident_dir is UPWIND of home — wind comes from its direction, so smoke
    drifts toward home. (wind_dir is where wind comes from; incident_dir is bearing home->fire.)"""
    bd = DIRDEG.get(incident_dir)
    if bd is None or wind_dir is None:
        return False
    diff = abs(bd - wind_dir)
    return min(diff, 360 - diff) <= 45


def _last_ts(cur):
    try:
        return open(STATE).read().strip()
    except Exception:
        cur.execute("SELECT (now() - interval '10 min')::text AS ts")
        return cur.fetchone()["ts"]


def main():
    dry = "--dry" in sys.argv
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True
    mc = mem.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    since = _last_ts(mc)

    mc.execute(
        "SELECT id, source, text, created_at, "
        "(metadata->'geo'->>'nearest_mi')::float mi, metadata->'geo'->>'nearest_dir' dir "
        "FROM memories WHERE source IN ('scanner','fire') AND created_at > %s "
        "AND (metadata->'geo'->>'nearest_mi') IS NOT NULL ORDER BY created_at", (since,))
    rows = mc.fetchall()
    wind_dir = _wind_dir()

    newest, alerts, seen = since, [], set()
    for r in rows:
        newest = r["created_at"].isoformat()
        if r["mi"] is not None and r["mi"] <= ALERT_MI and SERIOUS.search(r["text"] or ""):
            key = (round(r["mi"]), r["dir"], r["source"])       # crude per-incident dedupe
            if key in seen:
                continue
            seen.add(key)
            kind = "\U0001F692 FIRE" if r["source"] == "fire" else "\U0001F693 POLICE"
            snippet = re.sub(r"^\[.*?\]\s*", "", (r["text"] or ""))[:130]
            tag = "  ⚠️ UPWIND — smoke may drift toward home" if (r["source"] == "fire" and _upwind(r["dir"], wind_dir)) else ""
            alerts.append(f"{kind} · ~{r['mi']} mi {r['dir'] or ''} · {snippet}{tag}")

    # convergence: police AND fire both close in the recent window
    mc.execute(
        "SELECT source, count(*) c FROM memories WHERE source IN ('scanner','fire') "
        "AND created_at > now() - interval '%d min' "
        "AND (metadata->'geo'->>'nearest_mi')::float <= %f GROUP BY source" % (WINDOW_MIN, CONV_MI))
    close = {row["source"]: row["c"] for row in mc.fetchall()}
    conv = close.get("scanner", 0) >= 1 and close.get("fire", 0) >= 1

    heli = False
    try:
        ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
        oc.execute("SELECT count(*) FROM telemetry.overhead_flights WHERE ts > now() - interval "
                   "'%d min' AND is_helicopter AND operator ILIKE '%%police%%' "
                   "AND dist_nm*1.1508 <= 2" % WINDOW_MIN)
        heli = oc.fetchone()[0] > 0
        ops.close()
    except Exception:
        pass

    # convergence -> auto-assemble the actual nearby incidents into a mini report
    conv_body = None
    if conv:
        mc.execute(
            "SELECT source, text, created_at, (metadata->'geo'->>'nearest_mi')::float mi, "
            "metadata->'geo'->>'nearest_dir' dir FROM memories WHERE source IN ('scanner','fire') "
            "AND created_at > now() - interval '%d min' "
            "AND (metadata->'geo'->>'nearest_mi')::float <= %f ORDER BY created_at" % (WINDOW_MIN, CONV_MI))
        rpt = []
        for it in mc.fetchall()[:8]:
            k = "\U0001F692" if it["source"] == "fire" else "\U0001F693"
            snip = re.sub(r"^\[.*?\]\s*", "", (it["text"] or ""))[:90]
            rpt.append(f"{k} {it['created_at'].strftime('%H:%M')} ~{it['mi']}mi {it['dir'] or ''}: {snip}")
        extra = "\n\U0001F681 LAPD helicopter overhead" if heli else ""
        conv_body = (f"Police AND fire both active within {CONV_MI} mi (last {WINDOW_MIN} min){extra}\n\n"
                     + "\n".join(rpt))

    if dry:
        for a in alerts:
            print("  PROXIMITY:", a)
        print(f"  CONVERGENCE={conv} (police={close.get('scanner',0)}, fire={close.get('fire',0)}), heli={heli}")
        if conv_body:
            print("  --- convergence report ---\n" + conv_body)
    else:
        for a in alerts:
            notify("\U0001F6A8 Nearby incident", body=a, level="warning", category="geo_alert")
        if conv_body:
            notify("\U0001F6A8 Possible major incident nearby", body=conv_body,
                   level="warning", category="geo_alert", dedup_key="geo-convergence")
        try:
            open(STATE, "w").write(newest)
        except Exception:
            pass

    print(f"[geo-alerts] {len(alerts)} proximity alert(s), convergence={conv}, heli={heli}", flush=True)
    mem.close()


if __name__ == "__main__":
    main()
