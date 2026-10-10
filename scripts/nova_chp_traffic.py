#!/usr/bin/env python3
"""
nova_chp_traffic.py — California Highway Patrol live CAD incident feed → telemetry.

The CHP publishes a statewide "Situation Awareness" CAD XML feed at
https://media.chp.ca.gov/sa_xml/sa.xml. It is updated continuously and contains
every active incident statewide, grouped:

    <State>
      <Center ID="LAHB">              # Communications Center (LAHB = Los Angeles)
        <Dispatch ID="LACC">          # dispatch sub-center
          <Log ID="260624LA1679">     # one incident
            <LogTime>"Jun 24 2026 3:02PM"</LogTime>
            <LogType>"1182-Trfc Collision-No Inj"</LogType>
            <Location>"Sr110 N / E Glenarm St"</Location>
            <LocationDesc>"..."</LocationDesc>
            <Area>"Central LA"</Area>
            <LATLON>"34127470:118147168"</LATLON>   # lat:lon * 1e6, lon implicitly W
            <LogDetails>...</LogDetails>
          </Log>

We keep only the LA / Southern-division incidents relevant to Burbank / Glendale /
greater LA (Center LAHB + a whitelist of LA-region Area names, plus any incident
whose location text mentions Burbank/Glendale/the local freeway corridors). Rows
are upserted into telemetry.chp_incidents, deduped on incident_id, so re-polling
the same incident updates its detail/clear-state instead of duplicating.

This pairs with traffic_cams.py (Caltrans D7 CCTV) — cams show the road, this shows
why it's backed up. Runnable once (poller) on a ~5-minute schedule.

The feed rejects the urllib default UA, so we send a realistic browser User-Agent.
No auth, public feed. stdlib-only XML parsing; PG via psycopg2 (psql fallback).
Written by Jordan Koch.
"""
import re
import sys
import time
import xml.etree.ElementTree as ET
import urllib.request

import psycopg2

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
FEED_URL = "https://media.chp.ca.gov/sa_xml/sa.xml"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# Communications Center that covers the greater-LA basin (Burbank/Glendale/LA).
LA_CENTER = "LAHB"

# Area names within LAHB that are in/around the Burbank-Glendale-LA region we care
# about. LAHB also dispatches Central-Valley areas (Fresno/Bakersfield/etc) which we
# explicitly drop. Keep this LA-basin-focused.
LA_AREAS = {
    "central la", "west la", "south la", "east la", "la", "lafsp",
    "newhall", "altadena", "santa fe springs", "baldwin park",
    "west valley", "antelope valley",
}

# Place-name keywords that pull an incident in even if its Area bucket isn't on the
# LA_AREAS whitelist. These are Burbank/Glendale-specific place names only — NOT bare
# freeway numbers. (LAHB also dispatches Central-Valley areas like Los Banos / Fort
# Tejon whose locations sit on I5/SR152; matching bare "i5" there would wrongly pull
# them in. Generic freeway corridors are accepted only via the LA_AREAS path.)
LOCATION_KEYWORDS = re.compile(
    r"\b(burbank|glendale|glassell|eagle rock|atwater|los feliz|"
    r"verdugo|montrose|la canada|la crescenta|tujunga|sun valley|"
    r"sr134|sr-134|sr170|sr-170)\b",
    re.IGNORECASE,
)

DDL = """
CREATE SCHEMA IF NOT EXISTS telemetry;
CREATE TABLE IF NOT EXISTS telemetry.chp_incidents (
    incident_id   text PRIMARY KEY,
    ts            timestamptz NOT NULL DEFAULT now(),
    first_seen    timestamptz NOT NULL DEFAULT now(),
    log_time      text,
    type          text,
    location      text,
    location_desc text,
    area          text,
    center        text,
    detail        text,
    lat           double precision,
    lon           double precision,
    raw           text
);
CREATE INDEX IF NOT EXISTS idx_chp_incidents_ts   ON telemetry.chp_incidents (ts);
CREATE INDEX IF NOT EXISTS idx_chp_incidents_area ON telemetry.chp_incidents (area);
"""


def log(msg):
    print(f"[chp] {msg}")


def _clean(node, tag):
    """Field values come wrapped in literal double-quotes; strip them."""
    v = node.findtext(tag)
    if v is None:
        return None
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    v = v.strip()
    return v or None


def _parse_latlon(raw):
    """'34127470:118147168' -> (34.12747, -118.147168). Lon is West (negative)."""
    if not raw:
        return (None, None)
    raw = raw.strip().strip('"')
    if ":" not in raw:
        return (None, None)
    a, b = raw.split(":", 1)
    try:
        lat = int(a) / 1_000_000.0
        lon = int(b) / 1_000_000.0
    except ValueError:
        return (None, None)
    if lat == 0 and lon == 0:
        return (None, None)
    # CA is in the western hemisphere; the feed reports magnitude only.
    if lon > 0:
        lon = -lon
    return (lat, lon)


def _latest_detail(log_node):
    """Pull the most-recent incident-detail line, if any, for a short summary."""
    details = log_node.find("LogDetails")
    if details is None:
        return None
    last = None
    for det in details.findall("details"):
        txt = _clean(det, "IncidentDetail")
        if txt:
            last = txt
    return last


def fetch_xml():
    # NB: the CHP edge returns 406 for Accept: application/xml — it wants a
    # browser-style wildcard Accept. Send */* explicitly.
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": UA,
                                                    "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _repair_truncated(xml_bytes):
    """Salvage a truncated feed body.

    Since ~2026-08 the CHP edge caps the response at exactly 152 KiB (observed
    2026-09-13: identical cutoff plain and gzip — origin-side cap), cutting the
    statewide XML mid-token as the feed outgrew it. Every fetch then died with
    ParseError and the task hard-failed for weeks. The LAHB (LA) Center sits
    well before the cap, so cut back to the last complete </Log> and close any
    still-open ancestor elements; ET can then parse everything that arrived.
    Returns repaired bytes, or None if nothing salvageable.
    """
    text = xml_bytes.decode("utf-8", "replace")
    idx = text.rfind("</Log>")
    if idx == -1:
        return None
    head = text[: idx + len("</Log>")]
    for tag in ("Dispatch", "Center", "State"):
        missing = head.count("<" + tag) - head.count("</" + tag + ">")
        head += ("</" + tag + ">") * max(0, missing)
    return head.encode("utf-8")


def parse_incidents(xml_bytes):
    """Return list of LA-area incident dicts."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        repaired = _repair_truncated(xml_bytes)
        if repaired is None:
            raise
        log("feed truncated by CHP edge (152KiB cap) — parsing salvaged prefix")
        root = ET.fromstring(repaired)
    out = []
    for center in root.findall("Center"):
        cid = (center.get("ID") or "").strip()
        if cid != LA_CENTER:
            continue
        for disp in center.findall("Dispatch"):
            for lognode in disp.findall("Log"):
                area = _clean(lognode, "Area") or ""
                location = _clean(lognode, "Location") or ""
                loc_desc = _clean(lognode, "LocationDesc") or ""
                area_l = area.lower()
                in_area = area_l in LA_AREAS
                in_kw = bool(LOCATION_KEYWORDS.search(location)
                             or LOCATION_KEYWORDS.search(loc_desc))
                if not (in_area or in_kw):
                    continue
                inc_id = (lognode.get("ID") or "").strip()
                if not inc_id:
                    continue
                lat, lon = _parse_latlon(lognode.findtext("LATLON"))
                out.append({
                    "incident_id": inc_id,
                    "log_time": _clean(lognode, "LogTime"),
                    "type": _clean(lognode, "LogType"),
                    "location": location or None,
                    "location_desc": loc_desc or None,
                    "area": area or None,
                    "center": cid,
                    "detail": _latest_detail(lognode),
                    "lat": lat,
                    "lon": lon,
                    "raw": ET.tostring(lognode, encoding="unicode")[:8000],
                })
    return out


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(DDL)


def store(conn, incidents):
    inserted = 0
    updated = 0
    with conn.cursor() as cur:
        for inc in incidents:
            cur.execute(
                """
                INSERT INTO telemetry.chp_incidents
                    (incident_id, ts, log_time, type, location, location_desc,
                     area, center, detail, lat, lon, raw)
                VALUES
                    (%(incident_id)s, now(), %(log_time)s, %(type)s, %(location)s,
                     %(location_desc)s, %(area)s, %(center)s, %(detail)s,
                     %(lat)s, %(lon)s, %(raw)s)
                ON CONFLICT (incident_id) DO UPDATE SET
                    ts            = now(),
                    log_time      = EXCLUDED.log_time,
                    type          = EXCLUDED.type,
                    location      = EXCLUDED.location,
                    location_desc = EXCLUDED.location_desc,
                    area          = EXCLUDED.area,
                    center        = EXCLUDED.center,
                    detail        = EXCLUDED.detail,
                    lat           = EXCLUDED.lat,
                    lon           = EXCLUDED.lon,
                    raw           = EXCLUDED.raw
                RETURNING (xmax = 0) AS was_insert
                """,
                inc,
            )
            row = cur.fetchone()
            if row and row[0]:
                inserted += 1
            else:
                updated += 1
    return inserted, updated


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    quiet = "--quiet" in argv

    # The CHP edge occasionally serves a truncated body (ParseError: unclosed
    # token) — a transient that a fresh fetch clears. Retry fetch+parse a few
    # times before giving up so the scheduler doesn't see a hard failure.
    incidents = None
    last_err = None
    for attempt in range(3):
        try:
            xml_bytes = fetch_xml()
            incidents = parse_incidents(xml_bytes)
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:160]}"
            log(f"fetch/parse attempt {attempt + 1}/3 failed: {last_err}")
            time.sleep(2)
    if incidents is None:
        # 2026-10-01: a feed outage is the CHP's problem, not this task's. Exiting 1 here
        # got the task dead-lettered after 22 straight "no element found" (empty body)
        # responses during a morning the feed was down; the next 5-minute run is the retry.
        log(f"giving up after 3 attempts (feed outage, soft-skip): {last_err}")
        return 0

    if not incidents:
        log("no LA-area incidents in feed this cycle")
        return 0

    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    try:
        ensure_table(conn)
        inserted, updated = store(conn, incidents)
    finally:
        conn.close()

    log(f"LA-area incidents: {len(incidents)}  new={inserted} updated={updated}")
    if not quiet:
        for inc in incidents:
            loc = inc["location"] or inc["location_desc"] or "?"
            ll = f" ({inc['lat']:.4f},{inc['lon']:.4f})" if inc["lat"] else ""
            log(f"  {inc['incident_id']}  {(inc['area'] or '?'):<14} "
                f"{(inc['type'] or '?'):<30} {loc}{ll}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
