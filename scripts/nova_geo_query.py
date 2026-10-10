#!/usr/bin/env python3
"""nova_geo_query.py — proximity queries over the structured `places` table.

The structured counterpart to vector recall: answers "what's the nearest <category> to my
house?" by real distance, which semantic search cannot. Home coordinates live in
nova_ops.service_config (service='geo', key='home'); pass --from "lat,lon" to query from
anywhere else.

  nova_geo_query.py nearest ghost_town              # nearest ghost towns to home
  nova_geo_query.py nearest ca_tourist_attraction --limit 10
  nova_geo_query.py nearest ghost_town --from "36.6,-121.9"
  nova_geo_query.py categories                       # what can I ask about?
  nova_geo_query.py nearest ghost_town --json        # machine-readable (for Nova)
"""
from __future__ import annotations
import argparse
import json
import sys

import nova_dsn as _nova_dsn  # noqa: E402
MEM_DSN = _nova_dsn.pg_dsn("nova_memories")
import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
EARTH_MI = 3959

# Great-circle distance in SQL — least(1,...) guards acos() against float overshoot at d=0.
_DIST = ("{r}*acos(least(1, cos(radians(%s))*cos(radians(lat))*cos(radians(lon)-radians(%s))"
         "+ sin(radians(%s))*sin(radians(lat))))").format(r=EARTH_MI)


def home_coords():
    """(lat, lon, label) from service_config, or None if home isn't set."""
    import psycopg2
    with psycopg2.connect(OPS_DSN) as c, c.cursor() as cur:
        cur.execute("SELECT value FROM service_config WHERE service='geo' AND key='home'")
        r = cur.fetchone()
    if not r:
        return None
    v = r[0] if isinstance(r[0], dict) else json.loads(r[0])
    return float(v["lat"]), float(v["lon"]), v.get("label", "home")


def categories():
    import psycopg2
    with psycopg2.connect(MEM_DSN) as c, c.cursor() as cur:
        cur.execute("SELECT category, count(*) FROM places WHERE lat IS NOT NULL GROUP BY category ORDER BY 2 DESC")
        return cur.fetchall()


def nearest(category, lat, lon, limit=5):
    import psycopg2
    sql = (f"SELECT name, subcategory, url, round({_DIST}::numeric,0) AS miles "
           "FROM places WHERE category=%s AND lat IS NOT NULL ORDER BY miles LIMIT %s")
    with psycopg2.connect(MEM_DSN) as c, c.cursor() as cur:
        cur.execute(sql, (lat, lon, lat, category, limit))
        return [{"name": n, "detail": sub, "url": u, "miles": float(mi)} for n, sub, u, mi in cur.fetchall()]


def _origin(args):
    if args.__dict__.get("from"):
        la, lo = (float(x) for x in args.__dict__["from"].split(","))
        return la, lo, "the given point"
    h = home_coords()
    if not h:
        print("No home coordinates set (service_config geo/home) and no --from given.", file=sys.stderr)
        sys.exit(2)
    return h


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("nearest")
    n.add_argument("category")
    n.add_argument("--from", dest="from")
    n.add_argument("--limit", type=int, default=5)
    n.add_argument("--json", action="store_true")
    sub.add_parser("categories")
    args = ap.parse_args()

    if args.cmd == "categories":
        rows = categories()
        if getattr(args, "json", False):
            print(json.dumps([{"category": c, "count": n} for c, n in rows]))
        else:
            print("Queryable place categories:")
            for c, cnt in rows:
                print(f"  {c:24} {cnt:>6} located")
        return

    lat, lon, label = _origin(args)
    results = nearest(args.category, lat, lon, args.limit)
    if args.json:
        print(json.dumps({"category": args.category, "from": label, "results": results}))
        return
    if not results:
        print(f"No '{args.category}' places found. Try: nova_geo_query.py categories")
        return
    print(f"Nearest {args.category.replace('_', ' ')} to {label}:")
    for r in results:
        d = f" ({r['detail']})" if r["detail"] else ""
        print(f"  {r['miles']:>6.1f} mi  {r['name']}{d}")


if __name__ == "__main__":
    main()
