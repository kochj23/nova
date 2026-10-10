#!/usr/bin/env python3
"""nova_weekly_flights.py — Weekly overhead-aircraft report for /local, in Nova's voice.

Queries telemetry.overhead_flights for the past 7 days, enriches the most-seen callsigns
with from/to routes via the free adsbdb.com callsign->route API, and has Nova write a
catalog + summary of everything that flew over Burbank this week.

Run:  python3 nova_weekly_flights.py            # generate + publish now
Scheduled weekly (Sunday 19:00) via scheduler.yaml task 'weekly_flights'.
"""
import sys
import json
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".openclaw" / "scripts"))

import psycopg2
import psycopg2.extras
import nova_voice
from nova_local_burbank import call_llm, publish, generate_image

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
ADSBDB = "https://api.adsbdb.com/v0/callsign/"
ROUTE_LOOKUPS = 75   # cap external API calls (free tier courtesy)


def _q(cur, sql):
    cur.execute(sql)
    return cur.fetchall()


def fetch_flight_data():
    con = psycopg2.connect(DSN)
    con.autocommit = True
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    W = "ts > now() - interval '7 days'"
    d = {}
    d["totals"] = _q(cur, f"""SELECT count(1) sightings, count(distinct callsign) callsigns,
        count(distinct hex) aircraft, count(*) FILTER (WHERE is_helicopter) heli_sightings,
        round(min(dist_nm)::numeric,1) closest_nm, max(alt_ft) highest_ft, round(max(gs_kt)::numeric) fastest_kt
        FROM telemetry.overhead_flights WHERE {W}""")[0]
    d["operators"] = _q(cur, f"""SELECT operator, count(distinct callsign) flights, count(1) hits
        FROM telemetry.overhead_flights WHERE {W} AND operator IS NOT NULL AND operator<>''
        GROUP BY operator ORDER BY flights DESC LIMIT 15""")
    d["types"] = _q(cur, f"""SELECT coalesce(nullif(type_name,''), aircraft_type, 'unknown') t,
        count(distinct callsign) c FROM telemetry.overhead_flights WHERE {W}
        GROUP BY 1 ORDER BY c DESC LIMIT 12""")
    d["busiest_hours"] = _q(cur, f"""SELECT to_char(ts,'Dy HH24:00') slot, count(1) c
        FROM telemetry.overhead_flights WHERE {W} GROUP BY 1 ORDER BY c DESC LIMIT 6""")
    d["closest"] = _q(cur, f"""SELECT callsign, operator, round(dist_nm::numeric,1) nm, alt_ft,
        coalesce(nullif(type_name,''),aircraft_type) t FROM telemetry.overhead_flights
        WHERE {W} AND callsign IS NOT NULL AND dist_nm IS NOT NULL ORDER BY dist_nm ASC LIMIT 8""")
    d["lowest"] = _q(cur, f"""SELECT DISTINCT callsign, operator, alt_ft,
        coalesce(nullif(type_name,''),aircraft_type) t FROM telemetry.overhead_flights
        WHERE {W} AND callsign IS NOT NULL AND alt_ft IS NOT NULL AND alt_ft>0 AND NOT is_helicopter
        ORDER BY alt_ft ASC LIMIT 8""")
    # Commercial callsigns (airline ICAO code + number) with resolvable routes. Prioritize ones
    # seen LOW (< 12000 ft = BUR arrival/departure pattern) — the most-SEEN callsigns skew to
    # high-altitude transiting flights that aren't actually BUR traffic.
    d["top_callsigns"] = _q(cur, f"""SELECT callsign, operator, count(1) hits, min(alt_ft) min_alt
        FROM telemetry.overhead_flights WHERE {W} AND callsign ~ '^[A-Z]{{3}}[0-9]'
        GROUP BY callsign, operator
        ORDER BY (min(alt_ft) < 12000) DESC, hits DESC LIMIT {ROUTE_LOOKUPS}""")
    # Frequent N-number orbiters (LAPD / news / private choppers) — Burbank's real regulars.
    d["orbiters"] = _q(cur, f"""SELECT operator, count(distinct callsign) tails, count(1) hits
        FROM telemetry.overhead_flights WHERE {W} AND callsign ~ '^N[0-9]'
        AND operator IS NOT NULL AND operator<>'' GROUP BY operator ORDER BY hits DESC LIMIT 10""")
    con.close()
    return d


HOME_AIRPORTS = {"BUR", "KBUR"}   # Hollywood Burbank — the field under the house


def enrich_routes(top_callsigns):
    """Look up from->to for the busiest callsigns via adsbdb.com (free).

    Returns {callsign: {origin, origin_city, dest, dest_city, text}} so callers can
    identify which flights are actually landing at / departing from Burbank (BUR)."""
    routes = {}
    for row in top_callsigns:
        cs = (row["callsign"] or "").strip()
        if not cs:
            continue
        try:
            req = urllib.request.Request(ADSBDB + cs, headers={"User-Agent": "nova-flights/1.0"})
            r = json.loads(urllib.request.urlopen(req, timeout=8).read())
            fr = (r.get("response") or {}).get("flightroute") or {}
            o, dst = fr.get("origin") or {}, fr.get("destination") or {}
            if o and dst:
                oi, di = o.get("iata_code", "?"), dst.get("iata_code", "?")
                routes[cs] = {
                    "origin": oi, "origin_city": o.get("municipality", "?"),
                    "dest": di, "dest_city": dst.get("municipality", "?"),
                    "text": f"{oi} ({o.get('municipality','?')}) -> {di} ({dst.get('municipality','?')})",
                }
        except Exception:
            pass
    return routes


def build_summary(d, routes):
    t = d["totals"]
    L = [f"OVERHEAD BURBANK — PAST 7 DAYS (raw data for you to write from):",
         f"Totals: {t['sightings']} sightings, {t['callsigns']} distinct flights, "
         f"{t['aircraft']} distinct aircraft, {t['heli_sightings']} helicopter sightings. "
         f"Closest pass {t['closest_nm']} nm, highest {t['highest_ft']} ft, fastest {t['fastest_kt']} kt.",
         "\nTop operators (by flights): " + "; ".join(
             f"{r['operator']} ({r['flights']})" for r in d["operators"]),
         "\nFrequent N-number orbiters (choppers circling Burbank — LAPD, news, private): " + "; ".join(
             f"{r['operator']} ({r['hits']} sightings, {r['tails']} tails)" for r in d.get("orbiters", [])),
         "\nTop aircraft types: " + "; ".join(f"{r['t']} ({r['c']})" for r in d["types"]),
         "\nBusiest hours: " + "; ".join(f"{r['slot']} ({r['c']})" for r in d["busiest_hours"]),
         "\nClosest passes: " + "; ".join(
             f"{r['callsign']}/{r['operator'] or '?'} {r['t'] or ''} at {r['nm']}nm {r['alt_ft']}ft" for r in d["closest"]),
         "\nLowest fixed-wing: " + "; ".join(
             f"{r['callsign']}/{r['operator'] or '?'} {r['t'] or ''} {r['alt_ft']}ft" for r in d["lowest"])]
    if routes:
        from collections import Counter
        bur_arr = [r for r in routes.values() if r["dest"] in HOME_AIRPORTS]
        bur_dep = [r for r in routes.values() if r["origin"] in HOME_AIRPORTS]
        transiting = [r for r in routes.values()
                      if r["origin"] not in HOME_AIRPORTS and r["dest"] not in HOME_AIRPORTS]
        dep_rank = Counter(f"{r['dest_city']} ({r['dest']})" for r in bur_dep).most_common(15)
        arr_rank = Counter(f"{r['origin_city']} ({r['origin']})" for r in bur_arr).most_common(15)
        route_rank = Counter(r["text"] for r in routes.values()).most_common(15)
        L.append("\nBUR (Hollywood Burbank Airport) traffic — the LOW aircraft over the house are landing at or "
                 "departing BUR, NOT random overflights (the house is under the BUR approach/departure corridor).")
        L.append("  TOP DESTINATIONS departing BUR — RANKED by number of distinct flights (present these ranked): "
                 + (", ".join(f"{d} ({n})" for d, n in dep_rank) or "(none resolved this sample)"))
        L.append("  TOP ORIGINS arriving into BUR — RANKED: "
                 + (", ".join(f"{o} ({n})" for o, n in arr_rank) or "(none resolved this sample)"))
        L.append("\n  TOP ROUTES overall this week — RANKED by frequency: "
                 + "; ".join(f"{rt} ({n})" for rt, n in route_rank))
        if transiting:
            L.append("\nHIGH-altitude transiting overflights (NOT BUR traffic — just passing over): "
                     + "; ".join(r["text"] for r in transiting[:10]))
    else:
        L.append("\n(No from->to routes resolved this week — note honestly that most low fixed-wing is still "
                 "BUR arrival/departure traffic even without resolved routes.)")
    return "\n".join(L)


SYSTEM = nova_voice.NOVA_VOICE + """

FORMAT FOR THIS ARTICLE — a WEEKLY overhead-aircraft report for the /local journal:
- Open with the week's headline number and your take on the sky over Burbank.
- Catalog it: who's flying over (operators), what they're flying (types), and the from->to
  routes you can resolve. Weave the routes into prose, don't just dump a table.
- Call out the notable ones: the closest passes, the lowest fixed-wing, the helicopters,
  the busiest hours, anything weird.
- CRITICAL FRAMING: the house sits directly under the Hollywood Burbank Airport (BUR)
  approach/departure corridor. The LOW-altitude fixed-wing aircraft are almost all LANDING at
  or DEPARTING FROM BUR — write them that way (arriving from X, departing to Y), NOT as random
  overflights. Only the HIGH-altitude traffic is genuinely just passing over. Helicopters are
  the exception — those are LAPD/news/private choppers orbiting, not airport traffic.
- If a route couldn't be resolved, say so honestly rather than inventing one.
- RANK IT: Jordan specifically wants the destinations and routes ranked. Present the top BUR
  destinations (where departures head, most-frequent first with counts) and top origins (where
  arrivals come from) as an explicit ranked list, and call out the top overall routes in order.
  A short ranked list is allowed here — that's the point of the piece — even though the rest is prose.
- Prose everywhere else. Your full sarcastic voice. ~800-1100 words.
"""


def main():
    print("[weekly_flights] fetching 7d flight data...", flush=True)
    d = fetch_flight_data()
    print(f"[weekly_flights] enriching routes for {len(d['top_callsigns'])} callsigns...", flush=True)
    routes = enrich_routes(d["top_callsigns"])
    print(f"[weekly_flights] resolved {len(routes)} routes", flush=True)
    summary = build_summary(d, routes)
    # Verified aviation reference: translate ATC/pattern/transponder terms from the reference
    # vector rather than guessing (KBUR is General Aviation — the terms matter).
    system = SYSTEM
    try:
        from nova_code_reference import code_reference_block
        system += code_reference_block(summary, ["aviation"])
    except Exception as e:
        print(f"[weekly_flights] code-reference skipped: {e}", flush=True)
    body = call_llm(system, summary, max_tokens=6000)
    if not body or len(body) < 200:
        print("[weekly_flights] LLM returned too little; aborting", flush=True)
        return 1
    img_path = None
    try:
        img_path = generate_image(body)
        print(f"[weekly_flights] image: {img_path}", flush=True)
    except Exception as e:
        print(f"[weekly_flights] image gen failed ({e}); hourly repair will backfill", flush=True)
    title = f"What Flew Over Burbank This Week — {time.strftime('%B %-d, %Y')}"
    publish(title, body, img_path)
    print("[weekly_flights] published", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
