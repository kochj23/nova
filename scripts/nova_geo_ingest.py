#!/usr/bin/env python3
"""nova_geo_ingest.py — build a STRUCTURED, proximity-queryable 'places' table from Wikipedia
coordinates. Unlike vector ingestion (which can only do semantic recall), this stores lat/lon
so Nova can answer 'what's the nearest ghost town / tourist attraction to my house?'.

Modes:
  ghost_towns                      — US ghost towns via the list-of-ghost-towns hierarchy
  category <Category:X> <label>    — a Wikipedia category (+ its subcategories), e.g.
                                     Category:Tourist_attractions_in_California
"""
import sys, os, time, json, urllib.request, urllib.parse, urllib.error
sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
import nova_ingest as ni
import psycopg2

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_memories")
API = "https://en.wikipedia.org/w/api.php"
# Wikipedia's API policy wants a descriptive UA; a generic one gets rate-limited hard.
UA = {"User-Agent": "NovaGeoIngest/1.0 (personal homelab research; https://nova.digitalnoise.net)"}


def ensure_table(conn):
    with conn.cursor() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS places (
            id serial PRIMARY KEY, name text, category text, subcategory text,
            lat double precision, lon double precision, url text,
            created_at timestamptz DEFAULT now(), UNIQUE(name, category))""")
    conn.commit()


def api_get(params, _tries=4):
    url = API + "?" + urllib.parse.urlencode({**params, "format": "json"})
    for attempt in range(_tries):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < _tries - 1:
                time.sleep(5 * (attempt + 1))  # back off on rate-limit
                continue
            raise


def fetch_coords(titles):
    """{title: (lat,lon)} for titles that have a primary coordinate. Batches of 50."""
    out = {}
    for i in range(0, len(titles), 50):
        try:
            d = api_get({"action": "query", "prop": "coordinates", "coprimary": "primary",
                         "titles": "|".join(titles[i:i + 50])})
        except Exception as e:
            print(f"  coords batch {i} failed: {e}", flush=True); continue
        for p in d.get("query", {}).get("pages", {}).values():
            c = (p.get("coordinates") or [{}])[0]
            if c.get("lat") is not None:
                out[p["title"]] = (c["lat"], c["lon"])
        time.sleep(0.6)
        if i % 1000 == 0:
            print(f"  ...coords {i}/{len(titles)} ({len(out)} located)", flush=True)
    return out


def category_members(cat, depth=1):
    """All article titles in a category, recursing `depth` levels into subcategories."""
    pages, subcats, cont = set(), [], {}
    while True:
        d = api_get({"action": "query", "list": "categorymembers", "cmtitle": cat,
                     "cmlimit": "500", "cmtype": "page|subcat", **cont})
        for m in d.get("query", {}).get("categorymembers", []):
            (subcats.append(m["title"]) if m["ns"] == 14 else
             pages.add(m["title"]) if m["ns"] == 0 else None)
        if "continue" in d:
            cont = d["continue"]; time.sleep(0.2)
        else:
            break
    if depth > 0:
        for sc in subcats:
            pages |= category_members(sc, depth - 1); time.sleep(0.15)
    return pages


def insert_places(conn, rows):
    with conn.cursor() as c:
        for r in rows:
            c.execute("""INSERT INTO places(name,category,subcategory,lat,lon,url)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(name,category) DO NOTHING""", r)
    conn.commit()


def ghost_town_titles():
    _, _, us, _ = ni.wiki_fetch("https://en.wikipedia.org/wiki/Lists_of_ghost_towns_in_the_United_States")
    result = {}
    for sl in [l for l in us if "List_of_ghost_towns_in" in l]:
        state = urllib.parse.unquote(sl.split("List_of_ghost_towns_in_")[-1]).replace("_", " ")
        _, _, links, e = ni.wiki_fetch(sl); time.sleep(0.4)
        if e:
            continue
        for l in links:
            t = urllib.parse.unquote(l.split("/wiki/")[-1]).replace("_", " ")
            if ", " in t and "County" not in t and not any(t.lower().startswith(b) for b in
                    ("list", "category", "template", "portal", "help", "file", "wikipedia")):
                result[t] = state
    return result


def _url(t):
    return "https://en.wikipedia.org/wiki/" + t.replace(" ", "_")


_BAD_NS = ("category:", "template:", "portal:", "help:", "file:", "wikipedia:", "talk:",
           "special:", "module:", "user:", "draft:")


def _usable_place(title):
    tl = title.lower()
    if any(tl.startswith(b) for b in _BAD_NS):
        return False
    if "united states" in tl:
        return False
    return len(title) > 2 and title.count(" ") <= 6


def _is_sublist(title):
    tl = title.lower()
    return tl.startswith("list of") or "listings in" in tl


def list_titles(url, recurse=1, _seen=None):
    """Article titles from a 'List of X' page, recursing one level into sub-lists (e.g. NRHP
    'listings in <County>'). Flat lists (casinos, peaks) just return their article links."""
    _seen = _seen if _seen is not None else set()
    if url in _seen:
        return set()
    _seen.add(url)
    _, _, links, e = ni.wiki_fetch(url)
    time.sleep(0.4)
    if e:
        return set()
    out = set()
    for l in links:
        t = urllib.parse.unquote(l.split("/wiki/")[-1]).replace("_", " ")
        if _is_sublist(t):
            if recurse > 0:
                out |= list_titles(l, recurse - 1, _seen)
        elif _usable_place(t):
            out.add(t)
    return out


# California bounding box — for CA-scoped lists, keep only places actually inside CA. This
# throws out the navigation/reference/other-state link noise that raw list pages are full of.
CA_BBOX = (32.30, 42.05, -124.48, -114.13)  # (lat_min, lat_max, lon_min, lon_max)


def _in_bbox(lat, lon, bbox):
    return bbox is None or (bbox[0] <= lat <= bbox[1] and bbox[2] <= lon <= bbox[3])


def run_list(conn, url, label, recurse=0, bbox=CA_BBOX):
    titles = list_titles(url, recurse)
    print(f"'{label}' candidate place titles: {len(titles)}", flush=True)
    coords = fetch_coords(list(titles))
    rows = [(t, label, None, la, lo, _url(t)) for t, (la, lo) in coords.items()
            if _in_bbox(la, lo, bbox)]
    insert_places(conn, rows)
    print(f"DONE {label}: {len(coords)} located, {len(rows)} in-CA inserted", flush=True)


def main():
    conn = psycopg2.connect(DSN)
    ensure_table(conn)
    mode = sys.argv[1]
    if mode == "ghost_towns":
        titles = ghost_town_titles()
        print(f"ghost-town candidate titles: {len(titles)}", flush=True)
        coords = fetch_coords(list(titles))
        rows = [(t, "ghost_town", titles[t], la, lo, _url(t)) for t, (la, lo) in coords.items()]
        insert_places(conn, rows)
        print(f"DONE ghost_town: {len(coords)} located, {len(rows)} inserted", flush=True)
    elif mode == "list":
        rec = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        run_list(conn, sys.argv[2], sys.argv[3], recurse=rec)
    elif mode == "category":
        cat, label = sys.argv[2], sys.argv[3]
        pages = category_members(cat, depth=1)
        print(f"'{cat}' member pages: {len(pages)}", flush=True)
        coords = fetch_coords(list(pages))
        rows = [(t, label, None, la, lo, _url(t)) for t, (la, lo) in coords.items()
                if _in_bbox(la, lo, CA_BBOX)]
        insert_places(conn, rows)
        print(f"DONE {label}: {len(coords)} located, {len(rows)} in-CA inserted", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
