#!/usr/bin/env python3
"""nova_local_situation.py — "is something happening near us right now?"

Nova already watches the sky (ADS-B), the roads (CHP incidents), the airwaves (scanner
transcripts) and the property (cameras). Each feed answers a small question on its own and
none of them answers the one a human actually asks when a helicopter wakes them at 2am.

Fused, they do. A helicopter ORBITING low overhead is unremarkable in Burbank — until it
coincides with a CHP incident a mile away and fire dispatch naming your grid, at which point
it is one event and you would like to know. This computes a situation score from concurrent,
independent signals and only speaks when several agree.

The design rule is the same one the infrastructure work landed on: a single signal is a
self-report, several independent ones agreeing is a witnessed fact. One low helicopter is
noise. A low helicopter plus a nearby incident plus scanner traffic is a situation.
"""
import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import psycopg2

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
HOME_LAT, HOME_LON = 34.169, -118.325
NEAR_MI = 2.0
LOW_FT = 2500


def log(m):
    print(f"[local-situation] {m}", flush=True)


def miles(lat1, lon1, lat2, lon2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * r * math.asin(math.sqrt(a))


def main(minutes, alert):
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    signals, score = [], 0

    # ── 1. Aircraft loitering low and close ──────────────────────────────────
    # A helicopter that ORBITS is different from one transiting: same aircraft, many samples,
    # sustained low altitude nearby. Transits are constant here and mean nothing.
    cur.execute(f"""
        SELECT hex, max(callsign), count(*) AS hits, min(alt_ft), min(dist_nm), bool_or(is_helicopter)
        FROM telemetry.overhead_flights
        WHERE ts > now() - interval '{minutes} minutes'
          AND alt_ft < {LOW_FT} AND dist_nm < {NEAR_MI}
        GROUP BY hex HAVING count(*) >= 5
        ORDER BY 3 DESC""")
    for hexid, call, hits, alt, dist, heli in cur.fetchall():
        kind = "HELICOPTER" if heli else "aircraft"
        signals.append(f"{kind} {call or hexid} loitering: {hits} samples, "
                       f"as low as {alt}ft, {dist:.1f}nm out")
        score += 2 if heli else 1

    # ── 2. CHP incidents nearby ──────────────────────────────────────────────
    # CALIBRATED AGAINST THE BASE RATE. A 2-hour window over greater LA returned twenty-odd
    # "traffic hazard" incidents 3-5 miles away — that is the constant background of a city,
    # not an event, and alerting on it would train you to ignore this. Only SERIOUS types
    # genuinely close count, deduped: CHP publishes the same incident under several areas.
    SERIOUS = ('fire', 'collision', 'injury', 'pursuit', 'shots', 'fatal', 'pedestrian',
               'overturn', 'hazmat', 'assist w/', 'ambulance')
    cur.execute(f"""
        SELECT DISTINCT ON (type, location) type, location, area, lat, lon
        FROM telemetry.chp_incidents
        WHERE ts > now() - interval '{minutes} minutes' AND lat IS NOT NULL""")
    for typ, loc, area, lat, lon in cur.fetchall():
        d = miles(HOME_LAT, HOME_LON, float(lat), float(lon))
        blob = f"{typ} {loc}".lower()
        if d <= 1.5 and any(k in blob for k in SERIOUS):
            signals.append(f"CHP: {typ} at {loc} — {d:.1f} mi away")
            score += 2

    # ── 3. Scanner traffic mentioning our streets ────────────────────────────
    # Only counted when it names somewhere local; generic dispatch chatter is constant.
    try:
        cur.execute(f"""
            SELECT count(*) FROM memories
            WHERE created_at > now() - interval '{minutes} minutes'
              AND source ILIKE '%scanner%'""")
        n = cur.fetchone()[0]
        if n >= 3:
            signals.append(f"scanner: {n} dispatch transmissions in the window")
            score += 1
    except Exception:
        conn.rollback()

    # ── 4. Exterior motion at the property ───────────────────────────────────
    cur.execute(f"""
        SELECT count(*), count(DISTINCT room) FROM telemetry.presence
        WHERE ts > now() - interval '{minutes} minutes'
          AND method IN ('camera_vision','vehicle_vision')
          AND room IN ('front_yard','driveway','alley','back_yard','carport','entry')""")
    n, zones = cur.fetchone()
    # Raw counts are meaningless — 811 detections in 2h is a normal afternoon here. Compare
    # against this window's own 7-day baseline and only count a genuine SPIKE.
    cur.execute(f"""
        SELECT count(*)::float / 7 FROM telemetry.presence
        WHERE ts > now() - interval '7 days'
          AND ts::time BETWEEN (now() - interval '{minutes} minutes')::time AND now()::time
          AND method IN ('camera_vision','vehicle_vision')
          AND room IN ('front_yard','driveway','alley','back_yard','carport','entry')""")
    baseline = cur.fetchone()[0] or 0
    if n >= 5 and baseline and n > baseline * 2.5:
        signals.append(f"exterior motion SPIKE: {n} detections across {zones} zone(s) "
                       f"vs {baseline:.0f} typical for this time of day")
        score += 2

    # ── verdict ──────────────────────────────────────────────────────────────
    log(f"situation score {score} from {len(signals)} signal(s) over {minutes} min")
    for s in signals:
        log(f"  - {s}")
    # One signal is noise. Two independent ones agreeing is the thing worth saying out loud.
    if score >= 4 and len(signals) >= 2:
        msg = ("Something is happening nearby: " + "; ".join(signals) +
               ". Multiple independent feeds agree, which is why this is being raised at all.")
        log("SITUATION: " + msg)
        if alert:
            try:
                from nova_notify import notify
                notify(msg, level="warning", category="local")
                log("alerted")
            except Exception as e:
                log(f"notify failed: {e}")
    elif signals:
        log("below threshold — individually unremarkable, staying quiet")
    conn.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--minutes", type=int, default=20)  # short window: this asks 'right now'
    ap.add_argument("--alert", action="store_true")
    a = ap.parse_args()
    sys.exit(main(a.minutes, a.alert))
