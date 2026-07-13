#!/usr/bin/env python3
"""nova_neighborhood_pulse.py — weekly 'neighborhood pulse': the GEOGRAPHY of scanner/fire activity
around home over 7 days. Busiest compass sectors, hotspot cross-streets, the closest incidents of
the week, and the week-over-week trend. Private (Slack). Complements the daily block report with the
bigger spatial picture. Deterministic stats — no LLM needed. Scheduled weekly.
"""
import re
import sys
from collections import Counter
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
from nova_notify import notify

MEM_DSN = "host=localhost dbname=nova_memories user=kochj"
COMPASS = {"N": "north", "NE": "northeast", "E": "east", "SE": "southeast",
           "S": "south", "SW": "southwest", "W": "west", "NW": "northwest"}


def _rows(cur, start_h, end_h):
    cur.execute(
        "SELECT source, text, created_at, (metadata->'geo'->>'nearest_mi')::float mi, "
        "metadata->'geo'->>'nearest_dir' dir, metadata->'geo'->'locations' locs "
        "FROM memories WHERE source IN ('scanner','fire') "
        "AND metadata->'geo'->>'nearest_mi' IS NOT NULL "
        "AND created_at BETWEEN now() - interval '%d hours' AND now() - interval '%d hours'"
        % (start_h, end_h))
    return cur.fetchall()


def main():
    con = psycopg2.connect(MEM_DSN); con.autocommit = True
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    week = _rows(cur, 168, 0)
    prior = _rows(cur, 336, 168)
    con.close()

    if len(week) < 3:
        notify("\U0001F4CD Neighborhood pulse", body="Too few located incidents this week to chart a pattern.",
               category="pulse", dedup_key="pulse")
        print("[pulse] too few"); return

    n = len(week)
    trend = n - len(prior)
    arrow = "↑" if trend > 0 else ("↓" if trend < 0 else "→")
    dirs = Counter(r["dir"] for r in week if r["dir"])
    top_dir = dirs.most_common(1)[0] if dirs else ("?", 0)
    within = {b: sum(1 for r in week if r["mi"] is not None and r["mi"] <= b) for b in (1, 2.5, 5)}
    nearest = sorted((r for r in week if r["mi"] is not None), key=lambda r: r["mi"])[:4]
    # hotspot cross-streets (intersections only — no house numbers)
    spots = Counter()
    for r in week:
        for loc in (r["locs"] or []):
            a = loc.get("addr", "")
            if a and not a[:1].isdigit():
                spots[a] += 1
    hot = spots.most_common(4)

    def line(r):
        k = "\U0001F692" if r["source"] == "fire" else "\U0001F693"
        snip = re.sub(r"^\[.*?\]\s*", "", (r["text"] or ""))[:70]
        return f"  {k} ~{r['mi']} mi {r['dir'] or ''} — {snip}"

    body = [
        f"*{n}* located incidents this week  {arrow} {trend:+d} vs last week",
        f"Busiest direction: *{COMPASS.get(top_dir[0], top_dir[0])}* ({top_dir[1]} calls)",
        f"Proximity: {within[1]} within 1 mi · {within[2.5]} within 2.5 mi · {within[5]} within 5 mi",
    ]
    if hot:
        body.append("Hotspot cross-streets: " + ", ".join(f"{a} ({c})" for a, c in hot))
    body.append("\nClosest this week:")
    body += [line(r) for r in nearest]
    body.append("\n_(Only transmissions with a clean address are placed; garble is excluded.)_")

    notify(f"\U0001F4CD Neighborhood pulse — {n} calls, {arrow}{trend:+d}",
           body="\n".join(body)[:2900], category="pulse", dedup_key="pulse")
    print(f"[pulse] {n} incidents, trend {trend:+d}, busiest {top_dir}", flush=True)


if __name__ == "__main__":
    main()
