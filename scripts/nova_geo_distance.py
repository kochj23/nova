#!/usr/bin/env python3
"""nova_geo_distance.py — annotate scanner/fire/aviation text with distance from home.

Home = 508 S Glenwood Pl, Burbank CA 91506. For any street address or intersection found in a
transmission, geocode it (cached in nova_ops.geo_cache) and append the straight-line distance
from home. Used at ingest time (bakes distance into memory), in article generators, and in
conversation. Best-effort: Whisper garbles addresses, so misses are expected and cached so we
never re-query them. Geocoding is bounded to the LA-county viewbox to reject nonsense matches.

CLI:  python3 nova_geo_distance.py "unit ADW suspect 962 Hyperion Avenue, code 3"
"""
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request

import psycopg2

HOME_LAT, HOME_LON = 34.1679024, -118.3148472       # 508 S Glenwood Pl, Burbank CA 91506
DSN = "host=localhost dbname=nova_ops user=kochj"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = "nova-geo/1.0 (home scanner enrichment; kochj)"
# LA-county-ish viewbox (lon_w, lat_s, lon_e, lat_n) — bound geocoding so garble doesn't match Kansas
VIEWBOX = "-118.95,33.70,-117.60,34.85"

_STREET = (r"Street|St|Avenue|Ave|Boulevard|Blvd|Place|Pl|Drive|Dr|Road|Rd|Way|Court|Ct|"
           r"Lane|Ln|Terrace|Ter|Circle|Cir|Parkway|Pkwy|Highway|Hwy|Alley|Walk|Plaza")
NUM_ADDR = re.compile(
    r"\b(\d{2,5})\s+((?:[NSEW]\.?\s+|North\s+|South\s+|East\s+|West\s+)?"
    r"[A-Z][A-Za-z0-9]*(?:\s+[A-Z][A-Za-z0-9]*){0,2})\s+(?:" + _STREET + r")\b", re.I)
INTERSECTION = re.compile(
    r"\b([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)\s+(?:and|&|at)\s+"
    r"([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)\s+(?:" + _STREET + r")\b")


def _db():
    c = psycopg2.connect(DSN); c.autocommit = True
    return c


def ensure_cache(cur):
    cur.execute("CREATE TABLE IF NOT EXISTS geo_cache ("
                "q text PRIMARY KEY, lat double precision, lon double precision, "
                "ok boolean, cached_at timestamptz DEFAULT now())")


ANCHORS_FILE = os.path.expanduser("~/.openclaw/config/nova_anchors.json")


def _load_anchors():
    """Reference points to measure incidents against. Home is always present; add school/work/etc.
    to nova_anchors.json (name + address or lat/lon) and they slot in everywhere automatically."""
    default = [{"name": "home", "lat": HOME_LAT, "lon": HOME_LON}]
    try:
        with open(ANCHORS_FILE) as f:
            a = [x for x in json.load(f) if x.get("lat") is not None and x.get("lon") is not None]
        return a or default
    except Exception:
        return default


ANCHORS = _load_anchors()


def _haversine(lat1, lon1, lat2, lon2):
    R = 3958.8; p = math.radians
    x = (math.sin((p(lat2) - p(lat1)) / 2) ** 2
         + math.cos(p(lat1)) * math.cos(p(lat2)) * math.sin((p(lon2) - p(lon1)) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def _bearing(lat1, lon1, lat2, lon2):
    p = math.radians
    dlon = p(lon2 - lon1)
    y = math.sin(dlon) * math.cos(p(lat2))
    x = math.cos(p(lat1)) * math.sin(p(lat2)) - math.sin(p(lat1)) * math.cos(p(lat2)) * math.cos(dlon)
    brng = (math.degrees(math.atan2(y, x)) + 360) % 360
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][round(brng / 45) % 8]


def haversine_mi(lat, lon):
    return _haversine(HOME_LAT, HOME_LON, lat, lon)


def bearing_from_home(lat, lon):
    """Compass direction (N/NE/E/…) from home to a point — so 'south of me' reads naturally."""
    return _bearing(HOME_LAT, HOME_LON, lat, lon)


def nearest_anchor(lat, lon):
    """Closest saved reference point to (lat,lon): returns (name, miles, compass_dir)."""
    best = None
    for a in ANCHORS:
        d = _haversine(a["lat"], a["lon"], lat, lon)
        if best is None or d < best[1]:
            best = (a["name"], round(d, 1), _bearing(a["lat"], a["lon"], lat, lon))
    return best


def geocode(cur, query):
    """Return (lat, lon) or None. Cached (successes AND failures) so we never re-query a string."""
    q = query.strip()
    if not q:
        return None
    cur.execute("SELECT lat, lon, ok FROM geo_cache WHERE q=%s", (q,))
    row = cur.fetchone()
    if row is not None:
        return (row[0], row[1]) if row[2] else None
    lat = lon = None
    try:
        params = urllib.parse.urlencode({"format": "json", "limit": 1, "countrycodes": "us",
                                         "viewbox": VIEWBOX, "bounded": 1, "q": q + ", California"})
        req = urllib.request.Request(f"{NOMINATIM}?{params}", headers={"User-Agent": UA})
        import json
        data = json.loads(urllib.request.urlopen(req, timeout=12).read())
        if data:
            lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
        time.sleep(1.1)                              # Nominatim: <=1 req/sec
    except Exception:
        pass
    cur.execute("INSERT INTO geo_cache (q,lat,lon,ok) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (q) DO NOTHING", (q, lat, lon, lat is not None))
    return (lat, lon) if lat is not None else None


def find_locations(text):
    """Extract (matched_string, geocode_query) address/intersection candidates from text."""
    out, spans = [], []
    for m in NUM_ADDR.finditer(text or ""):
        out.append((m.group(0), m.group(0)))
        spans.append(m.span())
    for m in INTERSECTION.finditer(text or ""):
        if any(s <= m.start() < e for s, e in spans):   # don't double-count inside a full address
            continue
        out.append((m.group(0), f"{m.group(1)} & {m.group(2)}, Los Angeles"))
        spans.append(m.span())
    # de-dupe preserving order
    seen, uniq = set(), []
    for disp, q in out:
        if disp.lower() in seen:
            continue
        seen.add(disp.lower()); uniq.append((disp, q))
    return uniq


def locate(text, cur=None):
    """Return list of (matched_string, miles_from_home, compass_dir, nearest_anchor) for geocodable
    addresses. nearest_anchor is (name, miles, dir) — equals home unless a closer anchor is configured."""
    own = cur is None
    if own:
        conn = _db(); cur = conn.cursor(); ensure_cache(cur)
    res = []
    try:
        for disp, q in find_locations(text):
            ll = geocode(cur, q)
            if ll:
                res.append((disp, round(haversine_mi(*ll), 1), bearing_from_home(*ll), nearest_anchor(*ll)))
    finally:
        if own:
            conn.close()
    return res


def annotate(text, cur=None):
    """Return text with '(~X mi DIR)' after each address; flags a closer non-home anchor if any."""
    for disp, mi, dr, anc in locate(text, cur):
        tag = f"(~{mi} mi {dr})"
        if anc and anc[0] != "home" and anc[1] <= mi:
            tag = f"(~{mi} mi {dr}; ~{anc[1]} mi {anc[2]} of {anc[0]})"
        text = text.replace(disp, f"{disp} {tag}", 1)
    return text


# ── Locality weighting for NEWS (home is in Burbank; we care more about near than far) ──
# Approx miles-from-home for LA-area places, so a story's dominant place sets how local it is.
PLACE_MILES = {
    "downtown burbank": 1.2, "magnolia park": 1.4, "media district": 1.5, "burbank": 1.5,
    "griffith park": 2.4, "toluca lake": 3.1, "universal city": 3.1, "north hollywood": 3.6,
    "noho": 3.6, "glendale": 4.4, "los feliz": 4.6, "atwater village": 5.0, "studio city": 5.2,
    "tujunga": 5.4, "sun valley": 5.6, "hollywood": 5.9, "la canada": 6.2, "la crescenta": 6.3,
    "silver lake": 6.4, "silverlake": 6.4, "sunland": 6.5, "eagle rock": 6.8, "echo park": 7.5,
    "sherman oaks": 7.7, "van nuys": 8.2, "downtown los angeles": 9.8, "downtown la": 9.8,
    "dtla": 9.8, "pasadena": 11.0, "encino": 11.2, "santa clarita": 21.1,
}
_PLACE_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(k) for k in PLACE_MILES),
                                                key=len, reverse=True)) + r")\b", re.I)


def place_distance(text):
    """Return (dominant_place, miles_from_home) for the LA-area place a story is most about
    (most-mentioned; ties broken toward the nearer place), or None if no known place appears."""
    counts = {}
    for m in _PLACE_RE.finditer(text or ""):
        k = m.group(0).lower()
        counts[k] = counts.get(k, 0) + 1
    if not counts:
        return None
    k = min(counts, key=lambda x: (-counts[x], PLACE_MILES[x]))
    return (k, PLACE_MILES[k])


if __name__ == "__main__":
    sample = " ".join(sys.argv[1:]) or "ADW suspect 962 Hyperion Avenue; TC at Glenoaks Blvd; 403 West Boulevard"
    print("locations:", locate(sample))
    print("annotated:", annotate(sample))
    print("place_distance:", place_distance(sample))
