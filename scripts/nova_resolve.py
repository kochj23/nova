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
import threading

_PG_DSN = "dbname=nova_ops user=kochj host=127.0.0.1"
_CACHE_TTL = 30
_cache = {}
_cache_lock = threading.Lock()
_cache_ts = 0.0

_STATIC_MAP = {
    "postgresql":       ("192.168.1.6", 5432),
    "pgbouncer":        ("192.168.1.6", 6432),
    "redis":            ("192.168.1.6", 6379),
    "ollama":           ("192.168.1.6", 11434),
    "llama_server":     ("192.168.1.6", 11435),
    "memory_server":    ("192.168.1.6", 18790),
    "gateway":          ("192.168.1.6", 18792),
    "scheduler":        ("192.168.1.6", 37460),
    "big_brother":      ("192.168.1.6", 37461),
    "novacontrol":      ("192.168.1.6", 37400),
    "novacontrol_web":  ("192.168.1.6", 37450),
    "syslog":           ("192.168.1.6", 37462),
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


def _refresh_cache():
    """Load all service endpoints from PG into the cache."""
    global _cache, _cache_ts, _multi_cache
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
        cur.close()
        conn.close()
        with _cache_lock:
            _cache = new_cache
            _multi_cache = new_multi
            _cache_ts = time.time()
    except Exception:
        pass


_multi_cache = {}
_rr_counters = {}

def resolve(service_name: str, load_balance: bool = False) -> tuple:
    """Resolve service to (host, port). Cached 30s. Falls back to static.

    With load_balance=True, round-robins across all healthy instances.
    """
    global _cache_ts
    now = time.time()
    if now - _cache_ts > _CACHE_TTL:
        _refresh_cache()

    if load_balance:
        with _cache_lock:
            instances = _multi_cache.get(service_name, [])
            if len(instances) > 1:
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
    global _cache_ts
    now = time.time()
    if now - _cache_ts > _CACHE_TTL:
        _refresh_cache()

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
    global _cache_ts
    now = time.time()
    if now - _cache_ts > _CACHE_TTL:
        _refresh_cache()
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
