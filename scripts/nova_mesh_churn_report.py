#!/usr/bin/env python3
"""
nova_mesh_churn_report.py — track and report Meshtastic nodes the radio hears.

Modeled on nova_ble_churn_report.py. The Heltec T114 ("Rancho Adjacent", on
Jordans-Mac-mini) keeps a NodeDB in its own flash of every mesh node it has
ever heard. The bridge exposes it read-only at GET /nodes; we snapshot it into
telemetry.mesh_nodes and report churn daily.

  --collect   snapshot the radio's NodeDB into telemetry.mesh_nodes (hourly)
  --report    daily churn report -> shared_observations + nova_notify (7:15am)

NEW:  node whose SECOND distinct day of appearance is today (same rule as the
      BLE report — a one-off drive-by node isn't churn, it's weather).
GONE: node seen on >=7 of the last 14 days but silent for the last 3.

Written by Jordan Koch (via Claude).
"""

import argparse
import json
import sys
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
from nova_notify import notify

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
BRIDGE_NODES_URL = "http://jordans-mac-mini.local:37478/nodes"
LOG_FILE = Path.home() / ".openclaw/logs/mesh_churn_report.log"

DDL = """
CREATE TABLE IF NOT EXISTS telemetry.mesh_nodes (
    ts            timestamptz NOT NULL DEFAULT now(),
    node_id       text NOT NULL,
    long_name     text,
    short_name    text,
    hw_model      text,
    snr           real,
    hops_away     int,
    last_heard    timestamptz,
    battery_level int,
    latitude      double precision,
    longitude     double precision
);
CREATE INDEX IF NOT EXISTS mesh_nodes_node_ts_idx ON telemetry.mesh_nodes (node_id, ts);
CREATE INDEX IF NOT EXISTS mesh_nodes_ts_idx ON telemetry.mesh_nodes (ts);
"""


def log(msg):
    line = f"[mesh_churn {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def collect():
    """Snapshot the radio's NodeDB via the bridge into telemetry.mesh_nodes.

    The bridge is a best-effort source: the radio can be unplugged, the mini can
    reboot, mDNS can miss. Any of those must log and exit cleanly — an unguarded
    urlopen here killed the hourly job with a traceback.
    """
    for attempt in (1, 2, 3):
        try:
            with urllib.request.urlopen(BRIDGE_NODES_URL, timeout=10) as r:
                data = json.loads(r.read())
            break
        except Exception as e:
            if attempt == 3:
                log(f"collect: bridge unreachable after 3 attempts ({type(e).__name__}: {e}) "
                    f"— nothing recorded this cycle")
                return
            time.sleep(5)
    nodes = data.get("nodes", [])
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    cur.execute(DDL)
    inserted = 0
    for n in nodes:
        last_heard = None
        if n.get("lastHeard"):
            last_heard = datetime.fromtimestamp(n["lastHeard"], tz=timezone.utc)
        # Only snapshot nodes actually heard since the last collection cycle —
        # the NodeDB retains everything forever, and re-inserting stale entries
        # every hour would make every node look permanently present.
        cur.execute(
            "SELECT 1 FROM telemetry.mesh_nodes WHERE node_id=%s AND last_heard=%s LIMIT 1",
            (n["id"], last_heard))
        if cur.fetchone():
            continue
        cur.execute(
            "INSERT INTO telemetry.mesh_nodes (node_id, long_name, short_name, hw_model,"
            " snr, hops_away, last_heard, battery_level, latitude, longitude)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (n["id"], n.get("longName"), n.get("shortName"), n.get("hwModel"),
             n.get("snr"), n.get("hopsAway"), last_heard, n.get("batteryLevel"),
             n.get("latitude"), n.get("longitude")))
        inserted += 1
    conn.commit()
    conn.close()
    log(f"collect: {len(nodes)} nodes in NodeDB, {inserted} new sightings recorded")


def report():
    conn = psycopg2.connect(DSN)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(DDL)

    # NEW: second distinct day of appearance lands today (drive-bys don't count).
    cur.execute("""
        WITH daily AS (
            SELECT node_id, max(long_name) AS long_name, date(last_heard) AS d
            FROM telemetry.mesh_nodes
            WHERE last_heard > now() - interval '14 days'
            GROUP BY node_id, date(last_heard)
        ),
        ranked AS (
            SELECT node_id, long_name, d,
                   row_number() OVER (PARTITION BY node_id ORDER BY d) AS day_rank
            FROM daily
        )
        SELECT node_id, long_name FROM ranked
        WHERE day_rank = 2 AND d = current_date
        ORDER BY long_name NULLS LAST
    """)
    new_nodes = cur.fetchall()

    # GONE: regular (>=7 of last 14 days) but silent 3 days.
    cur.execute("""
        WITH recent AS (
            SELECT node_id, max(long_name) AS long_name,
                   count(DISTINCT date(last_heard)) AS days_seen,
                   max(last_heard) AS last_heard
            FROM telemetry.mesh_nodes
            WHERE last_heard > now() - interval '14 days'
            GROUP BY node_id
        )
        SELECT node_id, long_name, days_seen, last_heard FROM recent
        WHERE days_seen >= 7 AND last_heard < now() - interval '3 days'
        ORDER BY last_heard
    """)
    gone_nodes = cur.fetchall()

    # 24h activity summary.
    cur.execute("""
        SELECT node_id, max(long_name) AS long_name, max(short_name) AS short_name,
               max(last_heard) AS last_heard, max(snr) AS best_snr,
               min(hops_away) AS min_hops
        FROM telemetry.mesh_nodes
        WHERE last_heard > now() - interval '24 hours'
        GROUP BY node_id
        ORDER BY max(last_heard) DESC
    """)
    active = cur.fetchall()

    for d in new_nodes:
        cur.execute("""
            INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
            VALUES ('nova_mesh_churn_report', 'network', 'mesh-new-node', %s, 'info', %s)
        """, (
            f"New Meshtastic node on the local mesh: {d['long_name'] or d['node_id']}",
            psycopg2.extras.Json({"node_id": d["node_id"], "name": d["long_name"]}),
        ))
    for d in gone_nodes:
        cur.execute("""
            INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
            VALUES ('nova_mesh_churn_report', 'network', 'mesh-node-gone', %s, 'info', %s)
        """, (
            f"Meshtastic node that was regularly heard has gone silent: "
            f"{d['long_name'] or d['node_id']}, last heard {d['last_heard'].strftime('%Y-%m-%d %H:%M')}",
            psycopg2.extras.Json({"node_id": d["node_id"], "name": d["long_name"],
                                  "last_heard": d["last_heard"].isoformat()}),
        ))
    conn.commit()

    def _label(d):
        name = d.get("long_name") or "unnamed"
        return f"{name} ({d['node_id']})"

    lines = [
        f"Mesh churn — {len(new_nodes)} new, {len(gone_nodes)} gone; "
        f"{len(active)} node(s) heard in the last 24h (via Rancho Adjacent T114)",
    ]
    if new_nodes:
        lines.append("New:")
        lines += [f"  + {_label(d)}" for d in new_nodes[:15]]
    if gone_nodes:
        lines.append("Gone:")
        lines += [f"  - {_label(d)}, last heard {d['last_heard'].strftime('%m-%d')}"
                  for d in gone_nodes[:15]]
    if active:
        lines.append("Heard in last 24h:")
        for d in active[:15]:
            snr = f", SNR {d['best_snr']:.1f}" if d["best_snr"] is not None else ""
            hops = f", {d['min_hops']} hop(s)" if d["min_hops"] else ", direct"
            lines.append(f"  • {_label(d)}{hops}{snr}")

    log(f"report: new={len(new_nodes)} gone={len(gone_nodes)} active24h={len(active)}")
    try:
        notify(
            f"Mesh Node Churn Report ({date.today().isoformat()})",
            body="\n".join(lines),
            level="info",
            category="network",   # -> #nova-digest tier
            dedup_key="mesh-churn-daily",
            meta={"dedup_window_s": 72000},
        )
    except Exception as e:
        log(f"Notify failed: {e}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true", help="snapshot NodeDB via bridge")
    ap.add_argument("--report", action="store_true", help="daily churn report")
    args = ap.parse_args()
    if args.collect:
        collect()
    elif args.report:
        report()
    else:
        ap.print_help()
