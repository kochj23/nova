#!/usr/bin/env python3
"""nova_geo_enrich.py — bake distance-from-home into scanner/fire transmission memories.

Reads recent source in ('scanner','fire') memories not yet geo-enriched, extracts any street
address/intersection, geocodes it (cached), and rewrites the stored text with '(~X mi)' inline
plus a metadata.geo block (nearest_mi + per-address list). Because it edits the stored text,
distance then rides along everywhere downstream — recall, article context, conversation — with
no per-consumer wiring. Runs on a schedule; `--backfill` widens the window.

Home = 508 S Glenwood Pl, Burbank CA 91506 (see nova_geo_distance).
"""
import json
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
import nova_geo_distance as geo

MEM_DSN = "host=localhost dbname=nova_memories user=kochj"
OPS_DSN = "host=localhost dbname=nova_ops user=kochj"       # geo_cache lives here


def main():
    backfill = "--backfill" in sys.argv
    hours = 720 if backfill else 3
    limit = 5000 if backfill else 600

    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True
    mc = mem.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    up = mem.cursor()
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True
    oc = ops.cursor(); geo.ensure_cache(oc)

    mc.execute(
        "SELECT id, text FROM memories "
        "WHERE source IN ('scanner','fire') AND created_at > now() - interval '%s hours' "
        "AND NOT (coalesce(metadata,'{}'::jsonb) ? 'geo_enriched') "
        "ORDER BY created_at DESC LIMIT %s" % (hours, limit))
    rows = mc.fetchall()

    enriched = located = 0
    for r in rows:
        hits = geo.locate(r["text"], oc)               # oc = geo_cache cursor (nova_ops)
        newtext = r["text"]
        for disp, mi, dr, anc in hits:
            if "(~" not in disp:
                tag = f"(~{mi} mi {dr})"
                if anc and anc[0] != "home" and anc[1] <= mi:
                    tag = f"(~{mi} mi {dr}; ~{anc[1]} mi {anc[2]} of {anc[0]})"
                newtext = newtext.replace(disp, f"{disp} {tag}", 1)
        nearest = min((mi for _, mi, _, _ in hits), default=None)
        near_dir = min(hits, key=lambda h: h[1])[2] if hits else None
        anc_best = None
        for _, _, _, anc in hits:
            if anc and (anc_best is None or anc[1] < anc_best["mi"]):
                anc_best = {"name": anc[0], "mi": anc[1], "dir": anc[2]}
        geo_meta = {"nearest_mi": nearest, "nearest_dir": near_dir, "anchor": anc_best,
                    "locations": [{"addr": d, "mi": m, "dir": dr} for d, m, dr, _ in hits]}
        up.execute(
            "UPDATE memories SET text=%s, "
            "metadata = coalesce(metadata,'{}'::jsonb) "
            "           || jsonb_build_object('geo', %s::jsonb, 'geo_enriched', true) "
            "WHERE id=%s",
            (newtext, json.dumps(geo_meta), r["id"]))
        enriched += 1
        if hits:
            located += 1

    print(f"[geo-enrich] {'BACKFILL ' if backfill else ''}processed {enriched} memories, "
          f"{located} had geocodable addresses (window {hours}h)", flush=True)
    mem.close(); ops.close()


if __name__ == "__main__":
    main()
