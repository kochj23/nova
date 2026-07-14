#!/usr/bin/env python3
"""
nova_resolve.py — Service resolution for Nova Mesh.

Replaces hardcoded IPs with dynamic PG-backed resolution.
Falls back to static map when PG is unreachable — zero regression.

Usage:
    from nova_resolve import resolve, resolve_url

    host, port = resolve("memory_server")
    url = resolve_url("memory_server", "/health")

Written by Jordan Koch.
"""

import time
import random
import threading

_PG_DSN = "dbname=nova_ops user=kochj host=127.0.0.1"
_CACHE_TTL = 30
_cache = {}
_cache_lock = threading.Lock()
_cache_ts = 0.0

# Node-health cache: maps node_ip AND node_name -> headroom score.
# Refreshed alongside _cache (same ~30s TTL). Higher score = more able to
# take work. Down / stale nodes are excluded (not present in the map).
_node_headrooms = {}
# Fallback headroom for an instance whose host has no node_status row.
# Small but non-zero so it stays eligible for selection.
_DEFAULT_HEADROOM = 0.1
# A node is considered stale (and excluded) if its last heartbeat is older
# than this many seconds. Observed heartbeat interval is ~30-60s.
_HEARTBEAT_STALE_SECS = 120

_STATIC_MAP = {
    "postgresql":       ("192.168.1.6", 5432),
    "pgbouncer":        ("192.168.1.6", 6432),
    "redis":            ("192.168.1.6", 6379),
    "ollama":           ("192.168.1.6", 11434),
    "llama_server":     ("192.168.1.6", 11435),
    "memory_server":    ("192.168.1.6", 18790),
    "gateway":          ("192.168.1.2", 18792),
    "scheduler":        ("192.168.1.6", 37460),
    "big_brother":      ("192.168.1.6", 37461),
    "novacontrol":      ("192.168.1.6", 37400),
    "novacontrol_web":  ("192.168.1.6", 37450),
    "syslog":           ("192.168.1.2", 37462),
    "snmp_poller":      ("192.168.1.6", 37463),
    "endpoint_monitor": ("192.168.1.6", 37469),
    "presence_engine":  ("192.168.1.6", 37465),
    "hue":              ("192.168.1.6", 37476),
    "mlx_server":       ("192.168.1.6", 5050),
    "searxng":          ("192.168.1.2", 8080),
    "tinychat":         ("192.168.1.2", 8000),
    "grafana":          ("192.168.1.2", 3000),
    "homebridge":       ("192.168.1.2", 8581),
    "wazuh":            ("192.168.1.2", 9200),
    "plex":             ("192.168.1.2", 32400),
    "signal_cli":       ("192.168.1.6", 8080),
    "openrouter":       ("openrouter.ai", 443),
}


def _compute_headroom(cpu_cores, ram_gb, load_avg_1m, memory_percent):
    """Headroom score: free CPU capacity scaled by free memory.

        headroom = cpu_cores
                   * max(0, 1 - load_avg_1m / cpu_cores)   # free CPU fraction
                   * max(0.05, 1 - memory_percent / 100)    # free RAM fraction

    Higher = more able to take work. Returns 0.0 if cpu_cores is unusable.
    """
    try:
        cores = float(cpu_cores) if cpu_cores else 0.0
        if cores <= 0:
            return 0.0
        load = float(load_avg_1m) if load_avg_1m is not None else 0.0
        mem = float(memory_percent) if memory_percent is not None else 0.0
        cpu_free = max(0.0, 1.0 - (load / cores))
        mem_free = max(0.05, 1.0 - (mem / 100.0))
        return cores * cpu_free * mem_free
    except (TypeError, ValueError):
        return 0.0


def _refresh_cache():
    """Load all service endpoints + node-health scores from PG into cache."""
    global _cache, _cache_ts, _multi_cache, _node_headrooms
    try:
        import psycopg2
        conn = psycopg2.connect(_PG_DSN, connect_timeout=3)
        cur = conn.cursor()
        cur.execute("""
            SELECT service_name, host(host)::text, port
            FROM service_registry
            WHERE status IN ('up', 'unknown')
            ORDER BY priority ASC
        """)
        new_cache = {}
        new_multi = {}
        for row in cur.fetchall():
            svc, host, port = row
            if svc not in new_cache:
                new_cache[svc] = (host, port)
            new_multi.setdefault(svc, []).append((host, port))

        # Build node-health map keyed by both node_ip and node_name.
        # Down or stale-heartbeat nodes are excluded entirely (score 0 ->
        # simply not added to the map).
        cur.execute("""
            SELECT node_name, host(node_ip)::text, cpu_cores, ram_gb,
                   load_avg_1m, memory_percent, status,
                   EXTRACT(EPOCH FROM (now() - last_heartbeat)) AS age_secs
            FROM node_status
        """)
        new_headrooms = {}
        for row in cur.fetchall():
            (node_name, node_ip, cpu_cores, ram_gb,
             load_avg_1m, memory_percent, status, age_secs) = row
            if (status or "").lower() != "up":
                continue
            if age_secs is None or age_secs > _HEARTBEAT_STALE_SECS:
                continue
            score = _compute_headroom(cpu_cores, ram_gb,
                                      load_avg_1m, memory_percent)
            if score <= 0:
                continue
            if node_ip:
                new_headrooms[node_ip] = score
            if node_name:
                new_headrooms[node_name] = score

        cur.close()
        conn.close()
        with _cache_lock:
            _cache = new_cache
            _multi_cache = new_multi
            _node_headrooms = new_headrooms
            _cache_ts = time.time()
    except Exception:
        pass


def _ensure_fresh():
    """Refresh caches if past TTL."""
    global _cache_ts
    if time.time() - _cache_ts > _CACHE_TTL:
        _refresh_cache()


def _headroom_for_host(host) -> float:
    """Headroom for an instance host (IP string). Falls back to a small
    constant when no node_status row maps to it (so it stays eligible)."""
    if host in _node_headrooms:
        return _node_headrooms[host]
    return _DEFAULT_HEADROOM


def node_headrooms() -> dict:
    """Return current per-node headroom scores (for the mesh-map page).

    Keys are both node_ip and node_name (same score under each), so the
    map page can look up by whichever it has. Down/stale nodes are absent.
    """
    _ensure_fresh()
    with _cache_lock:
        return dict(_node_headrooms)


def _select_least_load(instances):
    """Power-of-two-choices selection across instances [(host, port), ...].

    Pick 2 candidates at random, weighted by headroom, then return the one
    with the higher current headroom. Single instance -> return it. If no
    headroom data exists for ANY instance, signal round-robin fallback by
    returning None.
    """
    if not instances:
        return None
    if len(instances) == 1:
        return instances[0]

    weights = [_headroom_for_host(h) for (h, _p) in instances]
    # If we have zero real node_status data for every instance, bail to RR.
    if not any(h in _node_headrooms for (h, _p) in instances):
        return None

    total = sum(weights)
    if total <= 0:
        return None

    def _weighted_pick():
        r = random.uniform(0, total)
        upto = 0.0
        for inst, w in zip(instances, weights):
            upto += w
            if r <= upto:
                return inst
        return instances[-1]

    a = _weighted_pick()
    b = _weighted_pick()
    # Pick the candidate with the higher current headroom.
    ha = _headroom_for_host(a[0])
    hb = _headroom_for_host(b[0])
    return a if ha >= hb else b


_multi_cache = {}
_rr_counters = {}

def resolve(service_name: str, load_balance: bool = False) -> tuple:
    """Resolve service to (host, port). Cached 30s. Falls back to static.

    With load_balance=True, performs capacity-weighted least-load selection
    (power-of-two-choices) across healthy instances using live node_status
    headroom scores. Falls back to round-robin if no node_status data is
    available for the service's instances.
    """
    _ensure_fresh()

    if load_balance:
        with _cache_lock:
            instances = _multi_cache.get(service_name, [])
            if len(instances) == 1:
                return instances[0]
            if len(instances) > 1:
                chosen = _select_least_load(instances)
                if chosen is not None:
                    return chosen
                # No node_status data at all -> round-robin fallback.
                idx = _rr_counters.get(service_name, 0)
                _rr_counters[service_name] = (idx + 1) % len(instances)
                return instances[idx]

    with _cache_lock:
        if service_name in _cache:
            return _cache[service_name]

    return _STATIC_MAP.get(service_name, ("127.0.0.1", 0))


def resolve_url(service_name: str, path: str = "") -> str:
    """Resolve to full URL: http://host:port/path"""
    host, port = resolve(service_name)
    if port == 443:
        return f"https://{host}{path}"
    return f"http://{host}:{port}{path}"


def resolve_all(service_name: str) -> list:
    """Return all registered instances of a service, ordered by priority."""
    _ensure_fresh()

    try:
        import psycopg2
        conn = psycopg2.connect(_PG_DSN, connect_timeout=3)
        cur = conn.cursor()
        cur.execute("""
            SELECT host(host)::text, port, node_name, status
            FROM service_registry
            WHERE service_name = %s
            ORDER BY priority ASC
        """, (service_name,))
        results = [{"host": r[0], "port": r[1], "node": r[2], "status": r[3]} for r in cur.fetchall()]
        cur.close()
        conn.close()
        return results
    except Exception:
        entry = _STATIC_MAP.get(service_name)
        if entry:
            return [{"host": entry[0], "port": entry[1], "node": "unknown", "status": "static"}]
        return []


def list_services() -> dict:
    """Return all registered services with their endpoints."""
    _ensure_fresh()
    with _cache_lock:
        return dict(_cache) if _cache else dict(_STATIC_MAP)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        svc = sys.argv[1]
        host, port = resolve(svc)
        print(f"{svc} -> {host}:{port}")
    else:
        print("Nova Mesh Service Resolution")
        print("=" * 50)
        services = list_services()
        for name, (host, port) in sorted(services.items()):
            print(f"  {name:20s} -> {host}:{port}")

        print()
        print("Node headroom scores (capacity-weighted least-load)")
        print("=" * 50)
        hr = node_headrooms()
        # Show IP-keyed entries only for readability (node_name dupes score).
        seen = set()
        for key, score in sorted(hr.items(), key=lambda kv: -kv[1]):
            if score in seen and not key.replace(".", "").isdigit():
                continue
            print(f"  {key:18s} headroom={score:.3f}")

        print()
        print("Load-balanced selection smoke test")
        print("=" * 50)
        for svc in ("ollama", "searxng"):
            counts = {}
            for _ in range(20):
                hostport = resolve(svc, load_balance=True)
                counts[hostport] = counts.get(hostport, 0) + 1
            print(f"  {svc}: {counts}")
