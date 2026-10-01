#!/usr/bin/env python3
"""nova_identity_graph.py — one graph over every identity space in the house.

The house observes people through four incompatible lenses, each with its own private
notion of "who": cameras know FACES, BLE knows FINGERPRINTS, UniFi knows CLIENT MACS,
vehicle-vision knows CARS. Nothing joins them, so a person walking through the front door
generates four unrelated rows and Nova can only ever say "something happened".

This builds the join. Nodes are observations; edges are CO-OCCURRENCE in time. Two things
that repeatedly appear together are probably attached to the same human, and after enough
weeks the clusters simply are people — no labelling required.

Scoring uses the phi coefficient over the full contingency table, for the reason the earlier
correlator learned the hard way: with plain overlap, two things that are both always present
score a perfect match and mean nothing. Discrimination comes from ABSENCE — from the times
one appeared and the other did not.

Edges are evidence, not conclusions. Nothing here writes device_owner or presence; it writes
a weighted graph that other tools (and humans) read.
"""
import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import psycopg2

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"


def _connect(dsn, tries=3):
    """House rule: external calls retry. The PG primary sits across the LAN; a transient
    'Operation timed out' (629 of these in September) must not fail the whole run."""
    import time as _t
    last = None
    for i in range(tries):
        try:
            return psycopg2.connect(dsn, connect_timeout=10)
        except psycopg2.OperationalError as e:
            last = e
            if i < tries - 1:
                _t.sleep(5 * (i + 1))
    raise last
SLOT_MINUTES = 5
MIN_SLOTS = 6          # a node seen fewer times than this proves nothing
MIN_PHI = 0.30
ALWAYS_ON = 0.95       # nodes present in >95% of slots carry no information


def log(m):
    print(f"[identity-graph] {m}", flush=True)


def collect(cur, days):
    """Every identity space, reduced to {(kind, key): set_of_time_slots}."""
    slot = f"floor(extract(epoch from %s)/{SLOT_MINUTES*60})::bigint"
    nodes = defaultdict(set)

    def add(kind, rows):
        for key, s in rows:
            if key:
                nodes[(kind, str(key))].add(s)

    # BLE — fingerprint preferred, it survives MAC rotation
    cur.execute(f"""SELECT coalesce(fingerprint, device_mac), {slot % 'ts'}
                    FROM telemetry.bluetooth
                    WHERE ts > now() - interval '{days} days'
                      AND device_mac <> '--:--:--:--:--:--' GROUP BY 1,2""")
    add("ble", cur.fetchall())

    # WiFi clients seen by UniFi
    cur.execute(f"""SELECT metadata->>'mac', {slot % 'ts'}
                    FROM telemetry.unifi_metrics
                    WHERE ts > now() - interval '{days} days'
                      AND metric='unifi_client_signal_dbm' GROUP BY 1,2""")
    add("wifi", cur.fetchall())

    # Named people from any presence method (placeholders excluded — they are not identities)
    cur.execute(f"""SELECT person, {slot % 'ts'} FROM telemetry.presence
                    WHERE ts > now() - interval '{days} days'
                      AND person NOT IN ('unknown','occupant','motion','camera_detected',
                                         'av_inferred','light_inferred','media_inferred','vehicle')
                    GROUP BY 1,2""")
    add("person", cur.fetchall())

    # Faces, when the recogniser has recorded any
    try:
        cur.execute(f"""SELECT person_name, {slot % 'last_seen'} FROM face_presence
                        WHERE last_seen > now() - interval '{days} days' GROUP BY 1,2""")
        add("face", cur.fetchall())
    except Exception:
        cur.connection.rollback()

    # Rooms where a camera saw a person / a vehicle was detected — location context
    cur.execute(f"""SELECT method || ':' || room, {slot % 'ts'} FROM telemetry.presence
                    WHERE ts > now() - interval '{days} days'
                      AND method IN ('camera_vision','vehicle_vision','mmwave')
                      AND room IS NOT NULL GROUP BY 1,2""")
    add("zone", cur.fetchall())
    return nodes


def main(days, dry_run):
    conn = _connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS telemetry.identity_graph_edge (
            a_kind text NOT NULL, a_key text NOT NULL,
            b_kind text NOT NULL, b_key text NOT NULL,
            phi real NOT NULL, together int, a_only int, b_only int,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (a_kind, a_key, b_kind, b_key))""")

    nodes = collect(cur, days)
    nodes = {k: v for k, v in nodes.items() if len(v) >= MIN_SLOTS}
    universe = set().union(*nodes.values()) if nodes else set()
    total = len(universe)
    log(f"nodes: {len(nodes)} across {len({k[0] for k in nodes})} identity spaces, "
        f"{total} time slots")
    # Drop the always-on: a node present in nearly every slot matches everything perfectly.
    dropped = [k for k, v in nodes.items() if total and len(v) / total > ALWAYS_ON]
    for k in dropped:
        del nodes[k]
    if dropped:
        log(f"dropped {len(dropped)} always-present nodes (no absence to learn from)")

    by_kind = defaultdict(list)
    for k in nodes:
        by_kind[k[0]].append(k)
    log("  " + ", ".join(f"{kind}={len(v)}" for kind, v in sorted(by_kind.items())))

    edges = []
    kinds = sorted(by_kind)
    for i, ka in enumerate(kinds):
        for kb in kinds[i:]:
            for a in by_kind[ka]:
                for b in by_kind[kb]:
                    if a >= b:
                        continue          # cross-kind and unordered within kind
                    sa, sb = nodes[a], nodes[b]
                    n11 = len(sa & sb)
                    if n11 < MIN_SLOTS:
                        continue
                    n10, n01 = len(sa - sb), len(sb - sa)
                    n00 = total - n11 - n10 - n01
                    den = math.sqrt((n11+n10)*(n11+n01)*(n00+n10)*(n00+n01))
                    if not den:
                        continue
                    phi = (n11*n00 - n10*n01) / den
                    if phi >= MIN_PHI:
                        edges.append((a[0], a[1], b[0], b[1], round(phi, 4), n11, n10, n01))

    edges.sort(key=lambda e: -e[4])
    # CROSS-KIND edges are the whole point — they join two identity spaces (a BLE fingerprint
    # to a WiFi client, a face to a person). Same-kind edges are mostly household fixtures
    # that are simply always on together, and they outnumber the useful ones ~100:1, so they
    # are stored but never allowed to dominate the report.
    cross = [e for e in edges if e[0] != e[2]]
    same = [e for e in edges if e[0] == e[2]]
    log(f"edges above phi {MIN_PHI}: {len(edges)}  ({len(cross)} cross-space, {len(same)} same-space)")
    log("CROSS-SPACE (these are the identity joins):")
    for e in cross[:20]:
        log(f"  {e[4]:.2f}  {e[0]}:{e[1][:24]:26} <-> {e[2]}:{e[3][:24]:26} together={e[5]}")
    if not cross:
        log("  (none yet — needs named-person or face history overlapping device history)")

    if edges and not dry_run:
        cur.executemany("""INSERT INTO telemetry.identity_graph_edge
            (a_kind,a_key,b_kind,b_key,phi,together,a_only,b_only)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (a_kind,a_key,b_kind,b_key) DO UPDATE SET
              phi=EXCLUDED.phi, together=EXCLUDED.together, a_only=EXCLUDED.a_only,
              b_only=EXCLUDED.b_only, updated_at=now()""", edges)
        log(f"stored {len(edges)} edges")
    conn.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    sys.exit(main(a.days, a.dry_run))
