#!/usr/bin/env python3
"""nova_service_deps.py — service_registry.depends_on + the "Multiple services down" roll-up (plan #59, 2026-10-15).

depends_on is text[] on service_registry. Each element is one of:
  'service@node'   another service_registry row (service_name@node_name)
  'host:<node>'    the machine itself (implicit for every row; listed only for clarity, never required)
  'docker:<node>'  that node's Docker engine (containers on nova-core: plex, searxng, tinychat, grafana,
                   frigate, homebridge, wazuh_*)

Only edges verified on 2026-10-15 are seeded (see VERIFIED). NB the registry's PG names are historic:
'postgresql@mac-studio' is the pgbouncer on .6:5432 that forwards to nova-core, and
'postgresql_replica@nova-core' is in fact the primary (pg_is_in_recovery=false; nova_pg_failover.py).

rollup(down, registry) -> {"roots": [...], "explained": {child: root}} — the hook incident code calls when
several services are down at once: a down service whose (transitive) dependency is also down is a symptom;
when every registered service on a node is down (>=2), the root is 'host:<node>'; when every docker-backed
service on a node is down (>=2) but not the whole node, the root is 'docker:<node>'.

  nova_service_deps.py --seed          add the column (safe_ddl) and write the verified edges (idempotent)
  nova_service_deps.py --rollup a@n b@n   print the roll-up for a hypothetical outage (uses live registry)
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_dsn  # noqa: E402
import nova_pg_schema  # noqa: E402

DDL = "ALTER TABLE service_registry ADD COLUMN IF NOT EXISTS depends_on text[] NOT NULL DEFAULT '{}'::text[]"

PG_PRIMARY = "postgresql_replica@nova-core"      # historic name; this row is the primary
PGBOUNCER_STUDIO = "postgresql@mac-studio"       # historic name; pgbouncer -> nova-core
DOCKER_CORE = "docker:nova-core"

# (service, node) -> deps.  Evidence, 2026-10-15:
#   docker ps on nova-core; grafana datasources (pg-primary:5434 and .6:5432); single-node wazuh compose;
#   nova_memory_server_start.sh waits on PG/Redis/Ollama on the Studio; nova_pg_failover.py topology;
#   haproxy.cfg on nova-core (pgsql_be primary = nova-core:5432).
VERIFIED = {
    ("plex", "nova-core"): [DOCKER_CORE],
    ("searxng", "nova-core"): [DOCKER_CORE],
    ("tinychat", "nova-core"): [DOCKER_CORE],
    ("homebridge", "nova-core"): [DOCKER_CORE],
    ("frigate", "nova-core"): [DOCKER_CORE],
    ("grafana", "nova-core"): [DOCKER_CORE, PG_PRIMARY, PGBOUNCER_STUDIO],
    ("wazuh_indexer", "nova-core"): [DOCKER_CORE],
    ("wazuh_manager", "nova-core"): [DOCKER_CORE, "wazuh_indexer@nova-core"],
    ("wazuh_dashboard", "nova-core"): [DOCKER_CORE, "wazuh_indexer@nova-core"],
    ("postgresql", "mac-studio"): [PG_PRIMARY],
    ("memory_server", "mac-studio"): [PGBOUNCER_STUDIO, "redis@mac-studio", "ollama@mac-studio"],
    ("scheduler-core", "nova-core"): [PG_PRIMARY],
    ("scheduler", "mac-studio"): [PG_PRIMARY],
}


def seed(cur):
    nova_pg_schema.safe_ddl(cur, DDL)
    n = 0
    for (svc, node), deps in VERIFIED.items():
        cur.execute("UPDATE service_registry SET depends_on=%s WHERE service_name=%s AND node_name=%s",
                    (deps, svc, node))
        n += cur.rowcount
    return n


def load_registry(cur):
    cur.execute("SELECT service_name, node_name, depends_on FROM service_registry")
    return {f"{s}@{n}": list(d or []) for s, n, d in cur.fetchall()}


def _ancestors(key, registry, seen=None):
    seen = set() if seen is None else seen
    for d in registry.get(key, []):
        if d not in seen:
            seen.add(d)
            _ancestors(d, registry, seen)
    return seen


def rollup(down, registry):
    """down: iterable of 'service@node'. registry: {'service@node': [deps]}. Pure function."""
    down = set(down)
    explained = {}
    # whole-node and whole-docker-engine outages first: they are the most common multi-service cause
    by_node, docker_by_node = {}, {}
    for key, deps in registry.items():
        node = key.split("@", 1)[1]
        by_node.setdefault(node, set()).add(key)
        if f"docker:{node}" in deps:
            docker_by_node.setdefault(node, set()).add(key)
    virtual_down = set()
    for node, keys in by_node.items():
        if len(keys) >= 2 and keys <= down:
            virtual_down.add(f"host:{node}")
            for k in keys:
                explained[k] = f"host:{node}"
    for node, keys in docker_by_node.items():
        if f"host:{node}" not in virtual_down and len(keys) >= 2 and keys <= down:
            virtual_down.add(f"docker:{node}")
            for k in keys:
                explained.setdefault(k, f"docker:{node}")
    for key in down - set(explained):
        hit = [a for a in _ancestors(key, registry) if a in down]
        if hit:
            # attribute to the deepest down ancestor (one with no down ancestor of its own)
            roots = [a for a in hit if not any(b in down for b in _ancestors(a, registry))]
            explained[key] = sorted(roots or hit)[0]
    roots = sorted((down - set(explained)) | virtual_down)
    return {"roots": roots, "explained": explained}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--rollup", nargs="*", metavar="SERVICE@NODE")
    a = ap.parse_args(argv)
    conn = nova_dsn.pg_connect()
    with conn, conn.cursor() as cur:
        if a.seed:
            print(f"depends_on written for {seed(cur)} rows")
        if a.rollup:
            print(json.dumps(rollup(a.rollup, load_registry(cur)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
