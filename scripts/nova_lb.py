#!/usr/bin/env python3
"""
nova_lb.py — Latency-based load balancer for Nova mesh.

F5-style: probe each node's health endpoint, track rolling response times,
route to the fastest healthy responder. No capability matrices, no weighted
algorithms — pure "who answered fastest and isn't broken."

Usage:
    from nova_lb import pick_node, get_pool_status

    # Get the best node for a task
    node = pick_node()  # returns {"name": "mac-mini", "ip": "192.168.1.77", "latency_ms": 12}

    # Or filter by capability
    node = pick_node(require_gpu=True)

    # Full pool status
    status = get_pool_status()  # list of all nodes with health/latency info

Also runs standalone as a daemon that writes pool state to PG every 10s.
"""

import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field

# ── Config ────────────────────────────────────────────────────────────────────

PROBE_INTERVAL = 10  # seconds between health probes
PROBE_TIMEOUT = 3    # seconds before marking a probe as failed
WINDOW_SIZE = 6      # rolling window (last N probes = last 60s at 10s interval)
MARK_DOWN_AFTER = 2  # consecutive failures before marking node down
RAMP_BACK_PROBES = 3 # successful probes before node is fully "up" again

# "protocols": which inference APIs this node actually serves, and on what port —
# ground-truthed by port scan, not assumed. A node can be healthy on its 37470
# sidecar and still be useless for a protocol it doesn't run.
NODES = [
    # ollama CHAT pool — nodes that actually serve chat (2026-09-18 rebuild): .6 ollama is
    # wedged on /api/chat (answers /api/tags but hangs on chat), nova-core5/.10 is
    # embeddings-only, and .190 is a dead soundbar IP — all removed from the ollama pool.
    {"name": "nova-core2", "ip": "192.168.1.86", "port": 37470, "gpu": False,
     "protocols": {"ollama": 11434}},
    {"name": "nova-core7", "ip": "192.168.1.125", "port": 37470, "gpu": True,
     "protocols": {"ollama": 11434}},
    {"name": "nova-core10", "ip": "192.168.1.77", "port": 37470, "gpu": True,
     "protocols": {"ollama": 11434, "mlx": 5050}},
    {"name": "tv-movies-mini", "ip": "192.168.1.7", "port": 37470, "gpu": True,
     "protocols": {"ollama": 11434, "mlx": 5050}},
    {"name": "mac-studio", "ip": "192.168.1.6", "port": 37470, "gpu": True,
     "protocols": {"mlx": 5050, "llamacpp": 11435}},   # ollama removed 2026-09-18: chat-wedged; re-add when fixed
]

# ── Pool State ────────────────────────────────────────────────────────────────

@dataclass
class NodeState:
    name: str
    ip: str
    port: int
    gpu: bool
    protocols: dict = field(default_factory=dict)   # {"ollama": 11434, "mlx": 5050, ...}
    status: str = "unknown"          # up, down, draining, ramping
    latencies: deque = field(default_factory=lambda: deque(maxlen=WINDOW_SIZE))
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_probe: float = 0.0
    last_healthy: float = 0.0
    # F5-style connection tracking
    active_connections: int = 0
    max_connections: int = 8         # per-node concurrency cap; 0 = unlimited
    total_connections: int = 0       # lifetime counter, for observability

    @property
    def p50_ms(self) -> float:
        if not self.latencies:
            return float("inf")
        sorted_l = sorted(self.latencies)
        return sorted_l[len(sorted_l) // 2]

    @property
    def avg_ms(self) -> float:
        if not self.latencies:
            return float("inf")
        return sum(self.latencies) / len(self.latencies)

    @property
    def is_healthy(self) -> bool:
        return self.status in ("up", "ramping")

    @property
    def accepts_new_work(self) -> bool:
        """Healthy, not draining, and under its connection cap."""
        if not self.is_healthy:
            return False
        if self.max_connections and self.active_connections >= self.max_connections:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ip": self.ip,
            "status": self.status,
            "p50_ms": round(self.p50_ms, 1),
            "avg_ms": round(self.avg_ms, 1),
            "consecutive_failures": self.consecutive_failures,
            "last_healthy": self.last_healthy,
            "active_connections": self.active_connections,
            "max_connections": self.max_connections,
            "total_connections": self.total_connections,
        }


_pool: list[NodeState] = []
_pool_lock = threading.Lock()
_sticky_map: dict[str, str] = {}   # session_id -> node name, for persistence
_sticky_lock = threading.Lock()

def _init_pool():
    global _pool
    _pool = [NodeState(name=n["name"], ip=n["ip"], port=n["port"], gpu=n["gpu"],
                        protocols=n.get("protocols", {}),
                        max_connections=n.get("max_connections", 8)) for n in NODES]

_init_pool()


# ── Pool Draining (F5-style: stop new work, let in-flight finish) ──────────────

def drain_node(name: str) -> bool:
    """Mark a node draining: pick_node() stops sending it new work, but
    in-flight connections (tracked via mark_start/mark_done) are left alone."""
    with _pool_lock:
        for n in _pool:
            if n.name == name:
                n.status = "draining"
                return True
    return False


def undrain_node(name: str) -> bool:
    """Return a drained node to the normal probe-driven state machine.
    It re-enters as 'ramping' so it has to prove itself healthy again
    before taking full traffic, same as any other recovering node."""
    with _pool_lock:
        for n in _pool:
            if n.name == name and n.status == "draining":
                n.status = "ramping"
                n.consecutive_successes = 0
                return True
    return False


# ── Connection Tracking (F5-style: least-connections needs real counts) ────────

def mark_start(name: str) -> None:
    """Call when a request is dispatched to `name`. Pair with mark_done()."""
    with _pool_lock:
        for n in _pool:
            if n.name == name:
                n.active_connections += 1
                n.total_connections += 1
                return


def mark_done(name: str) -> None:
    """Call when a request to `name` completes (success OR failure)."""
    with _pool_lock:
        for n in _pool:
            if n.name == name:
                n.active_connections = max(0, n.active_connections - 1)
                return

# ── Probing ───────────────────────────────────────────────────────────────────

def _probe_node(node: NodeState) -> tuple[bool, float]:
    """Probe a node's health endpoint. Returns (healthy, latency_ms)."""
    url = f"http://{node.ip}:{node.port}/health"
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=PROBE_TIMEOUT)
        data = json.loads(resp.read())
        elapsed_ms = (time.perf_counter() - start) * 1000
        if data.get("status") == "ok":
            return True, elapsed_ms
        return False, elapsed_ms
    except Exception:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return False, elapsed_ms


def _probe_all():
    """Probe all nodes and update pool state."""
    now = time.time()
    with _pool_lock:
        for node in _pool:
            healthy, latency_ms = _probe_node(node)
            node.last_probe = now

            if healthy:
                node.latencies.append(latency_ms)
                node.consecutive_failures = 0
                node.consecutive_successes += 1
                node.last_healthy = now

                if node.status == "down" or node.status == "unknown":
                    node.status = "ramping"
                elif node.status == "ramping" and node.consecutive_successes >= RAMP_BACK_PROBES:
                    node.status = "up"
                elif node.status == "up":
                    pass  # stay up
            else:
                node.consecutive_successes = 0
                node.consecutive_failures += 1

                if node.consecutive_failures >= MARK_DOWN_AFTER:
                    node.status = "down"


# ── Public API ────────────────────────────────────────────────────────────────

def pick_node(require_gpu: bool = False, exclude: list[str] = None,
               strategy: str = "latency", session_id: str = None,
               respect_capacity: bool = True, protocol: str = None) -> dict | None:
    """Pick a node for new work. F5-style strategies:

    protocol: if set (e.g. "ollama", "mlx"), only nodes that actually serve that
    protocol are eligible, and the returned dict's "port" is that protocol's real
    port on the chosen node — not every node in the pool runs every backend.

    strategy="latency"     (default) — fastest p50 wins. Best for stateless,
                            short-lived requests where you want the snappiest node.
    strategy="least_conn"  — fewest active_connections wins, latency as tiebreak.
                            Best when requests vary wildly in duration (e.g. long
                            generations) — latency alone can't see "busy but fast".

    session_id: if set, sticky — the same session_id always lands on the same
    node as long as it keeps accepting work (persistence, F5-style). Falls
    through to normal selection (and re-stickies) if that node stops qualifying.

    respect_capacity: if True (default), nodes at their max_connections or in
    "draining" state are excluded from NEW picks — existing sticky/in-flight
    work on a draining node is untouched, it just gets no new assignments.
    """
    exclude = exclude or []

    def _result(n, sticky):
        d = {"name": n.name, "ip": n.ip, "latency_ms": round(n.p50_ms, 1),
             "active_connections": n.active_connections, "sticky": sticky}
        if protocol:
            d["port"] = n.protocols[protocol]
        return d

    with _pool_lock:
        pred = (lambda n: n.accepts_new_work) if respect_capacity else (lambda n: n.is_healthy)
        candidates = [
            n for n in _pool
            if pred(n)
            and n.name not in exclude
            and (not require_gpu or n.gpu)
            and (not protocol or protocol in n.protocols)
        ]

        # Sticky sessions: reuse the same node if it still qualifies.
        if session_id:
            with _sticky_lock:
                sticky_name = _sticky_map.get(session_id)
            if sticky_name:
                sticky_node = next((n for n in candidates if n.name == sticky_name), None)
                if sticky_node:
                    return _result(sticky_node, True)
                # sticky node no longer qualifies — fall through and re-stick below

        if not candidates:
            return None

        if strategy == "least_conn":
            best = min(candidates, key=lambda n: (n.active_connections, n.p50_ms))
        else:
            best = min(candidates, key=lambda n: n.p50_ms)

        if session_id:
            with _sticky_lock:
                _sticky_map[session_id] = best.name

        return _result(best, False)


def get_pool_status() -> list[dict]:
    """Get full pool status for all nodes."""
    with _pool_lock:
        return [n.to_dict() for n in _pool]


# ── Cross-process picking (for consumers like the gateway, running as their own
# process — they don't share this module's in-memory _pool with the standalone
# nova_lb.py daemon, so they read the daemon's probe results out of PG instead,
# where it's written every PROBE_INTERVAL seconds regardless of who's asking) ──

_NODE_BY_NAME = {n["name"]: n for n in NODES}


def pick_node_shared(require_gpu: bool = False, exclude: list[str] = None,
                      strategy: str = "latency", protocol: str = None,
                      max_staleness_s: float = 30.0, dsn: str = "dbname=nova_ops user=kochj host=localhost"
                      ) -> dict | None:
    """Same contract as pick_node(), but sourced from lb_pool_status in PG instead
    of this process's own probe loop. Use this from any process that isn't the
    nova_lb.py daemon itself (e.g. the gateway) — avoids running a second,
    divergent probe loop against the same nodes.

    Falls back to None (caller should have its own fallback chain) if PG is
    unreachable or the daemon's data is stale past max_staleness_s — a load
    balancer that silently trusts minutes-old health data is worse than none.
    """
    import psycopg2
    try:
        conn = psycopg2.connect(dsn, connect_timeout=3)
        cur = conn.cursor()
        cur.execute("""
            SELECT node_name, status, p50_ms, active_connections, updated_at,
                   extract(epoch from (now() - updated_at)) as age_s
            FROM lb_pool_status
        """)
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return None

    exclude = exclude or []
    candidates = []
    for node_name, status, p50_ms, active_conn, updated_at, age_s in rows:
        cfg = _NODE_BY_NAME.get(node_name)
        if not cfg:
            continue  # stale row for a node no longer in NODES
        if age_s is None or age_s > max_staleness_s:
            continue  # daemon hasn't reported recently — don't trust it
        if status not in ("up", "ramping"):
            continue
        if node_name in exclude:
            continue
        if require_gpu and not cfg.get("gpu"):
            continue
        if protocol and protocol not in cfg.get("protocols", {}):
            continue
        candidates.append({
            "name": node_name, "ip": cfg["ip"],
            "p50_ms": p50_ms if p50_ms is not None else float("inf"),
            "active_connections": active_conn or 0,
            "port": cfg["protocols"][protocol] if protocol else cfg.get("port"),
        })

    if not candidates:
        return None

    if strategy == "least_conn":
        best = min(candidates, key=lambda c: (c["active_connections"], c["p50_ms"]))
    else:
        best = min(candidates, key=lambda c: c["p50_ms"])

    return {"name": best["name"], "ip": best["ip"], "port": best["port"],
            "latency_ms": round(best["p50_ms"], 1) if best["p50_ms"] != float("inf") else None,
            "active_connections": best["active_connections"], "sticky": False}


# ── PG Reporting ──────────────────────────────────────────────────────────────

def _report_to_pg():
    """Write pool state to PG for dashboard consumption."""
    try:
        import psycopg2
        conn = psycopg2.connect("dbname=nova_ops user=kochj host=localhost", connect_timeout=5)
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS lb_pool_status (
                node_name TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                p50_ms REAL,
                avg_ms REAL,
                consecutive_failures INT DEFAULT 0,
                last_healthy TIMESTAMPTZ,
                active_connections INT DEFAULT 0,
                max_connections INT DEFAULT 0,
                total_connections INT DEFAULT 0,
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute("ALTER TABLE lb_pool_status ADD COLUMN IF NOT EXISTS active_connections INT DEFAULT 0")
        cur.execute("ALTER TABLE lb_pool_status ADD COLUMN IF NOT EXISTS max_connections INT DEFAULT 0")
        cur.execute("ALTER TABLE lb_pool_status ADD COLUMN IF NOT EXISTS total_connections INT DEFAULT 0")

        for node in _pool:
            cur.execute("""
                INSERT INTO lb_pool_status (node_name, status, p50_ms, avg_ms, consecutive_failures, last_healthy,
                                             active_connections, max_connections, total_connections, updated_at)
                VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), %s, %s, %s, now())
                ON CONFLICT (node_name) DO UPDATE SET
                    status = EXCLUDED.status,
                    p50_ms = EXCLUDED.p50_ms,
                    avg_ms = EXCLUDED.avg_ms,
                    consecutive_failures = EXCLUDED.consecutive_failures,
                    active_connections = EXCLUDED.active_connections,
                    max_connections = EXCLUDED.max_connections,
                    total_connections = EXCLUDED.total_connections,
                    last_healthy = EXCLUDED.last_healthy,
                    updated_at = now()
            """, (
                node.name, node.status,
                round(node.p50_ms, 1) if node.p50_ms != float("inf") else None,
                round(node.avg_ms, 1) if node.avg_ms != float("inf") else None,
                node.consecutive_failures,
                node.last_healthy if node.last_healthy > 0 else None,
                node.active_connections, node.max_connections, node.total_connections,
            ))

        conn.close()
    except Exception as e:
        print(f"[nova-lb] PG report failed: {e}", file=sys.stderr, flush=True)


# ── DNS Reporting (F5-style: ollama/cluster follow the fastest healthy node) ───

BIND_PRIMARY = "192.168.1.138"
DNS_ZONE = "digitalnoise.net"
DNS_TTL = 15
_last_dns_ip = None

def _tsig_secret():
    import subprocess as _sp
    return _sp.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-bind-tsig-key", "-w"],
        capture_output=True, text=True, check=True
    ).stdout.strip()


def _report_to_dns():
    """Push ollama.digitalnoise.net + cluster.digitalnoise.net to whichever node is
    currently fastest+healthy — but only when the pick actually changes, not every
    probe cycle. This is the F5-style DNS failover layer for cluster names."""
    global _last_dns_ip
    best = pick_node(require_gpu=True)
    if not best:
        return  # nothing healthy — leave the last-known-good record in place
    ip = best["ip"]
    if ip == _last_dns_ip:
        return
    try:
        import subprocess as _sp
        secret = _tsig_secret()
        script = (
            f"server {BIND_PRIMARY}\n"
            f"zone {DNS_ZONE}.\n"
            f"update delete ollama.{DNS_ZONE}. A\n"
            f"update add ollama.{DNS_ZONE}. {DNS_TTL} A {ip}\n"
            f"update delete cluster.{DNS_ZONE}. A\n"
            f"update add cluster.{DNS_ZONE}. {DNS_TTL} A {ip}\n"
            f"send\n"
        )
        r = _sp.run(["nsupdate", "-y", f"hmac-sha256:nova-dns-key:{secret}"],
                     input=script, capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            print(f"[nova-lb] DNS: ollama/cluster -> {best['name']} ({ip})", flush=True)
            _last_dns_ip = ip
        else:
            print(f"[nova-lb] DNS update failed: {r.stderr.strip()[:200]}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[nova-lb] DNS update failed: {e}", file=sys.stderr, flush=True)


# ── Daemon Mode ───────────────────────────────────────────────────────────────

_running = True

def _probe_loop():
    """Main probe loop — run probes and report to PG every interval."""
    while _running:
        _probe_all()
        _report_to_pg()
        _report_to_dns()
        time.sleep(PROBE_INTERVAL)


def main():
    global _running
    print(f"[nova-lb] Starting latency-based load balancer ({len(NODES)} nodes, {PROBE_INTERVAL}s interval)", flush=True)

    def _shutdown(sig, frame):
        global _running
        _running = False
        print("[nova-lb] shutting down", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Initial probe
    _probe_all()
    _report_to_dns()
    status = get_pool_status()
    for s in status:
        print(f"  {s['name']}: {s['status']} ({s['p50_ms']}ms)", flush=True)

    # Enter probe loop
    _probe_loop()


if __name__ == "__main__":
    main()
