#!/usr/bin/env python3
"""nova_usher_fissure.py — USHER'S FISSURE: watchers that would fall with the thing they watch.

From Poe's "The Fall of the House of Usher": riding up to the house, the narrator notices a
crack so faint he nearly misses it, running from the roof down the front wall in a zigzag
until it disappears into the dark tarn below. The house and the last of the Usher line share
that one flaw. When Roderick and Madeline die together on the final night, the narrator flees,
the crack splits wide in the light of a blood-red moon, and the whole house drops into the
tarn. One fault ran under both of them, and nobody had mapped it. (Rice's Akasha is the same
lesson as a body: every vampire's life ran through her, so harming her harmed them all.)

Nova's version, the minimal first version of the spec: read the Studio scheduler YAML and the
Nova launchd plists, find each job's script, and from the source work out
  * where it runs (this node: both inputs are the Studio's),
  * how it alerts: nova_notify (event row on pg-primary, then the notifier daemon) and/or a
    direct Slack post (needs only the watcher's own host),
  * what it watches: every fleet host named in the source (URLs, host=, *.digitalnoise.net,
    localhost), mapped to a node through node_status.
A watcher's single points are the hosts that sit on EVERY one of its alert paths (its own host
is always one). An Usher pair is a watcher that watches one of its own single points: if that
node dies, the thing goes down and the alarm goes down with it. Blast radius per node = how
many watchers would fall silent if it died.

The graph built here (service -> node, service -> table it writes, table -> service that reads
it) is the shared dependency graph; nova_speedy_circle imports it for cross-organ loops.

CLI:   --run [--dry-run]   --show   --selftest
Table: usher_fissure (one row per Usher pair per run)
service_config: nova_usher_fissure/latest (summary), nova_usher_fissure/notifier_node (override)
Schedule: weekly, Sunday 03:20.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import plistlib
import re
import socket
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_watch_common as W  # noqa: E402
from nova_jade_amulet import PLIST_DIRS, PLIST_PREFIXES  # noqa: E402

SERVICE = "nova_usher_fissure"
SCRIPTS_DIR = Path(__file__).resolve().parent
SCHEDULER_YAML = SCRIPTS_DIR.parent / "config" / "scheduler.yaml"
NOTIFIER_LABEL = "net.digitalnoise.daemon.nova-notifier"
LOCAL_NAMES = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

# How a script can raise an alarm. "notify" rides PG + the notifier daemon; "slack" goes straight out.
ALERT_PATHS = {
    "notify": re.compile(r"\bnova_notify\b|telemetry\.events"),
    "slack": re.compile(r"\bpost_slack\b|\bpost_both\b|chat\.postMessage|hooks\.slack\.com"),
}
HOST_RE = re.compile(r"(?:https?://|['\"])([A-Za-z0-9-]+\.digitalnoise\.net|localhost|"
                     r"\d{1,3}(?:\.\d{1,3}){3})\b")
SCRIPT_RE = re.compile(r"(\S+\.(?:py|sh))\b")
WRITE_RE = re.compile(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_][a-z0-9_.]*)")
READ_RE = re.compile(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_.]*)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS usher_fissure (
  ts timestamptz NOT NULL DEFAULT now(),
  watcher text, script text, host text, alert_paths text[], watched text[], shared text[]);
CREATE INDEX IF NOT EXISTS usher_fissure_ts ON usher_fissure (ts);
"""


def log(m: str) -> None:
    print(f"[usher-fissure {datetime.now():%H:%M:%S}] {m}", flush=True)


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — a failed read degrades to "nothing known"
        log(f"query failed: {e}")
        return []


# ── inventory (config, not runtime) ─────────────────────────────────────────

def scheduler_jobs(path: Path = SCHEDULER_YAML) -> list:
    """[(name, script path)] for every enabled task in the scheduler YAML."""
    import yaml
    try:
        tasks = (yaml.safe_load(path.read_text()) or {}).get("tasks") or {}
    except (OSError, yaml.YAMLError) as e:
        log(f"scheduler yaml unreadable: {e}")
        return []
    return [(f"task:{k}", SCRIPTS_DIR / v["script"]) for k, v in tasks.items()
            if isinstance(v, dict) and v.get("script") and v.get("enabled", True) is not False]


def launchd_jobs(dirs=PLIST_DIRS) -> list:
    """[(name, script path)] for Nova plists whose program is a .py/.sh script."""
    out = []
    for d in dirs:
        for p in sorted(d.glob("*.plist")) if d.is_dir() else []:
            if not p.name.startswith(PLIST_PREFIXES):
                continue
            try:
                pl = plistlib.loads(p.read_bytes())
            except Exception as e:  # noqa: BLE001 — one bad plist never sinks the map
                log(f"unreadable {p.name}: {e}")
                continue
            args = " ".join(pl.get("ProgramArguments") or [pl.get("Program") or ""])
            m = SCRIPT_RE.search(args)
            if m:
                out.append((f"launchd:{pl.get('Label') or p.stem}", Path(m.group(1))))
    return out


def scan(src: str) -> dict:
    """What a script's source says: alert paths, hosts named, tables written and read."""
    return {"paths": sorted(k for k, rx in ALERT_PATHS.items() if rx.search(src)),
            "hosts": sorted(set(HOST_RE.findall(src))),
            "writes": sorted(set(WRITE_RE.findall(src))),
            "reads": sorted(set(READ_RE.findall(src)))}


# ── host resolution ─────────────────────────────────────────────────────────

def resolve(name: str) -> str | None:
    """IP for a name, or None. DNS is local and fast; a miss is not retried (fails open)."""
    try:
        return socket.gethostbyname(name)
    except OSError:
        return None


def node_of(name: str, nodes: dict, local: str) -> str | None:
    """Fleet node a hostname lands on, or None for anything outside the fleet."""
    if name in LOCAL_NAMES:
        return local
    return nodes.get(resolve(name) or "")


def local_node(nodes: dict, probe: str) -> str:
    """This machine's node name: the source address it would use toward `probe` (no packet sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((resolve(probe) or probe, 9))
            return nodes.get(s.getsockname()[0]) or socket.gethostname().split(".")[0]
    except OSError:
        return socket.gethostname().split(".")[0]


# ── the graph and its fissures (pure) ───────────────────────────────────────

def single_points(host: str, hops: list) -> set:
    """Nodes on every alert path. hops = [set of nodes per path]; the watcher's host is on all."""
    return set.intersection(*({host} | set(h) for h in hops)) if hops else {host}


def fissures(watchers: list) -> list:
    """Usher pairs: watchers that watch one of their own single points."""
    out = []
    for w in watchers:
        shared = single_points(w["host"], w["hops"]) & set(w["watched"])
        if shared:
            out.append(dict(w, shared=sorted(shared)))
    return sorted(out, key=lambda w: (w["shared"], w["name"]))


def blast_radius(watchers: list) -> list:
    """[(node, watchers silenced if it dies)], biggest first."""
    c = Counter(n for w in watchers for n in single_points(w["host"], w["hops"]))
    return c.most_common()


def dependency_graph(services: list) -> dict:
    """Directed edges {node: set(nodes)}: svc -> node:<runs on / alert hop / watched>,
    svc -> table:<written>, table:<read> -> svc. Shared with nova_speedy_circle."""
    g: dict = {}
    for s in services:
        me = f"svc:{s['script']}"   # one node per script: a task and a plist running it are the same organ
        out = g.setdefault(me, set())
        out.add(f"node:{s['host']}")
        out.update(f"node:{n}" for h in s["hops"] for n in h)
        out.update(f"node:{n}" for n in s["watched"])
        out.update(f"table:{t}" for t in s["writes"])
        for t in s["reads"]:
            g.setdefault(f"table:{t}", set()).add(me)
    return g


# ── assembly ────────────────────────────────────────────────────────────────

def load_nodes(cur) -> dict:
    return {str(ip): name for ip, name in _q(cur, "SELECT host(node_ip), node_name FROM node_status")}


def services(cur, jobs=None) -> list:
    """Every scheduled/launchd script on this node with its scan, hosts mapped to nodes."""
    nodes = load_nodes(cur)
    pg_host = re.search(r"host=(\S+)", W.DSN).group(1)
    local = local_node(nodes, pg_host)
    try:
        notifier = W.get_config(cur, SERVICE, "notifier_node")
    except Exception as e:  # noqa: BLE001 — no override, fall through to discovery
        log(f"config read failed: {e}")
        notifier = None
    if not notifier:
        notifier = local if any((d / f"{NOTIFIER_LABEL}.plist").exists() for d in PLIST_DIRS) else None
    notify_hops = {n for n in (node_of(pg_host, nodes, local), notifier) if n}
    out = []
    for name, path in jobs if jobs is not None else scheduler_jobs() + launchd_jobs():
        try:
            src = Path(path).read_text(errors="replace")
        except OSError:
            continue
        s = scan(src)
        # ponytail: the PG alias is how a script reaches its DB, not what it watches; a real PG
        # watcher (replication, recovery) is missed here until runtime probes are mapped.
        watched = {node_of(h, nodes, local) for h in s["hosts"] if h != pg_host} - {None}
        hops = [notify_hops if p == "notify" else set() for p in s["paths"]]
        out.append({"name": name, "script": Path(path).name, "host": local, "paths": s["paths"],
                    "hops": hops, "watched": sorted(watched), "writes": s["writes"], "reads": s["reads"]})
    return out


def build_graph(cur=None) -> dict:
    """The shared dependency graph, read-only (opens its own connection when cur is None)."""
    if cur is not None:
        return dependency_graph(services(cur))
    conn = W.connect()
    try:
        return dependency_graph(services(conn.cursor()))
    finally:
        conn.close()


def run(dry: bool = False) -> list:
    conn = W.connect()
    try:
        cur = conn.cursor()
        svcs = services(cur)
        watchers = [s for s in svcs if s["paths"]]
        pairs = fissures(watchers)
        radius = blast_radius(watchers)
        log(f"{'DRY RUN ' if dry else ''}{len(svcs)} services, {len(watchers)} watchers, "
            f"{len(pairs)} Usher pairs; blast radius {radius[:5]}")
        for p in pairs:
            print(f"  {','.join(p['shared']):<14} {p['name']:<48} alerts via {'+'.join(p['paths'])}"
                  f"  watches {','.join(p['watched'])}")
        if not dry:
            ensure_schema(cur)
            ts = datetime.now(timezone.utc)
            for p in pairs:
                cur.execute("INSERT INTO usher_fissure (ts, watcher, script, host, alert_paths, watched, shared) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (ts, p["name"], p["script"], p["host"], p["paths"], p["watched"], p["shared"]))
            W.set_config(cur, SERVICE, "latest", {"ts": ts.isoformat(), "services": len(svcs),
                         "watchers": len(watchers), "pairs": len(pairs), "blast_radius": radius[:10]},
                         by="nova_usher_fissure")
            log(f"wrote {len(pairs)} rows")
        return pairs
    finally:
        conn.close()


def show() -> int:
    conn = W.connect()
    try:
        cur = conn.cursor()
        if not (_q(cur, "SELECT to_regclass('usher_fissure')") or [(None,)])[0][0]:
            print("no map yet")
            return 0
        for row in _q(cur, "SELECT shared, watcher, alert_paths, watched FROM usher_fissure "
                           "WHERE ts = (SELECT max(ts) FROM usher_fissure) ORDER BY shared, watcher"):
            print(f"{','.join(row[0]):<14} {row[1]:<48} {'+'.join(row[2])}  watches {','.join(row[3])}")
        return 0
    finally:
        conn.close()


def selftest() -> int:
    assert single_points("studio", []) == {"studio"}
    assert single_points("studio", [{"core"}, set()]) == {"studio"}           # slack path skips core
    assert single_points("studio", [{"core", "studio"}]) == {"studio", "core"}
    ws = [{"name": "a", "host": "studio", "hops": [{"core"}], "watched": ["core"]},
          {"name": "b", "host": "studio", "hops": [{"core"}, set()], "watched": ["core"]},
          {"name": "c", "host": "studio", "hops": [set()], "watched": ["nas"]}]
    assert [p["name"] for p in fissures(ws)] == ["a"]
    assert dict(blast_radius(ws)) == {"studio": 3, "core": 1}
    s = scan("from nova_notify import notify\nurl='http://localhost:1/x'\n"
             "cur.execute('INSERT INTO foo VALUES (1)'); cur.execute('SELECT x FROM bar')")
    assert s == {"paths": ["notify"], "hosts": ["localhost"], "writes": ["foo"], "reads": ["bar"]}, s
    g = dependency_graph([{"name": "task:a", "script": "a", "host": "studio", "hops": [{"core"}], "watched": [],
                           "writes": ["t"], "reads": ["u"]}])
    assert g["svc:a"] == {"node:studio", "node:core", "table:t"} and g["table:u"] == {"svc:a"}
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="map watchers, list Usher pairs, store the map")
    ap.add_argument("--dry-run", action="store_true", help="with --run: print the map, write nothing")
    ap.add_argument("--show", action="store_true", help="latest stored Usher pairs")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.run:
        run(dry=a.dry_run)
        return 0
    if a.show:
        return show()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
