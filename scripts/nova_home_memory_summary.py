#!/usr/bin/env python3
"""
nova_home_memory_summary.py — distill a day of home-sensor telemetry into a short
narrative observation and store it in Nova's vector memory, so she can recall and
reason about the home in conversation ("the rack ran hot Sunday afternoon").

Raw telemetry stays in the Ops DB (telemetry.*); this writes only a daily summary
into nova_memories via the memory service. Run once a day (late evening).
"""
import json
import sys
import urllib.request
from datetime import datetime

import psycopg2

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
VECTOR_URL = "http://memory-server.digitalnoise.net:18790/remember"


def q1(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchone()


def build_summary(cur) -> tuple[str, dict]:
    parts = []
    meta = {}

    # Climate per room (24h)
    cur.execute("""
        SELECT room, round(avg(temp_f)::numeric,1), round(max(temp_f)::numeric,1),
               round(avg(humidity)::numeric,0), round(max(light_lux)::numeric,0)
        FROM telemetry.climate
        WHERE ts > now() - interval '24 hours' AND temp_f IS NOT NULL
        GROUP BY room ORDER BY 3 DESC NULLS LAST""")
    rooms = cur.fetchall()
    if rooms:
        hottest = rooms[0]
        parts.append(f"Warmest spot was {hottest[0]} (avg {hottest[1]}F, peak {hottest[2]}F).")
        meta["rooms_tracked"] = len(rooms)
        for r in rooms:
            if r[3] is not None and r[3] < 30:
                parts.append(f"{r[0]} humidity ran low (~{r[3]}%).")
                break

    # Air quality (VOC) — Eve Room / rack
    aq = q1(cur, """SELECT round(avg(voc)::numeric,0), round(max(voc)::numeric,0)
                    FROM telemetry.air_quality
                    WHERE ts > now() - interval '24 hours' AND voc IS NOT NULL""")
    if aq and aq[0] is not None:
        parts.append(f"Rack air VOC averaged {aq[0]} (peak {aq[1]}) ug/m3.")
        meta["voc_avg"] = float(aq[0])

    # Battery — lowest device
    bat = q1(cur, """SELECT device, min(level) FROM telemetry.battery
                     WHERE ts > now() - interval '24 hours' AND level IS NOT NULL
                     GROUP BY device ORDER BY 2 ASC LIMIT 1""")
    if bat and bat[1] is not None:
        note = "all healthy" if bat[1] >= 30 else "needs attention"
        parts.append(f"Lowest sensor battery: {bat[0]} at {bat[1]}% ({note}).")
        meta["lowest_battery"] = {"device": bat[0], "level": int(bat[1])}

    # Presence — which rooms saw occupancy
    cur.execute("""SELECT DISTINCT room FROM telemetry.presence
                   WHERE ts > now() - interval '24 hours' AND confidence > 0
                     AND room NOT IN ('away','home','nearby','unknown') ORDER BY room""")
    occ = [r[0] for r in cur.fetchall()]
    if occ:
        parts.append(f"Occupancy was seen in: {', '.join(occ)}.")
        meta["occupied_rooms"] = occ

    date = datetime.now().strftime("%A %B %d, %Y")
    text = f"Home environment summary for {date}. " + " ".join(parts)
    return text, meta


def remember(text, meta):
    payload = json.dumps({"text": text, "source": "home_observations", "metadata": meta}).encode()
    req = urllib.request.Request(VECTOR_URL, data=payload,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()


def main():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    text, meta = build_summary(cur)
    conn.close()
    if len(text) < 60:
        print("[home-memory] not enough telemetry to summarize — skipping", flush=True)
        return 0
    print(f"[home-memory] {text}", flush=True)
    try:
        resp = remember(text, meta)
        print(f"[home-memory] stored to vector memory: {resp[:120]}", flush=True)
    except Exception as e:
        print(f"[home-memory] FAILED to store: {e}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
