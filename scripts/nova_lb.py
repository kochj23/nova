#!/usr/bin/env python3
"""
nova_lb.py — Latency-based load balancer for Nova mesh.

F5-style: probe each node's health endpoint, track rolling response times,
route to the fastest healthy responder. No capability matrices, no weighted
algorithms — pure "who answered fastest and isn't broken."

Usage:
    from nova_lb import pick_node, get_pool_status

    # Get the best node for a task
    node = pick_node()  # returns {"name": "mac-mini", "ip": "192.168.1.190", "latency_ms": 12}

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

NODES = [
    {"name": "mac-studio", "ip": "192.168.1.6", "port": 37470, "gpu": True},
    {"name": "mac-mini", "ip": "192.168.1.190", "port": 37470, "gpu": True},
    {"name": "tv-movies-mini", "ip": "192.168.1.7", "port": 37470, "gpu": True},
    {"name": "nuk", "ip": "192.168.1.10", "port": 37470, "gpu": False},
]

# ── Pool State ────────────────────────────────────────────────────────────────

@dataclass
class NodeState:
    name: str
    ip: str
    port: int
    gpu: bool
    status: str = "unknown"          # up, down, draining, ramping
    latencies: deque = field(default_factory=lambda: deque(maxlen=WINDOW_SIZE))
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_probe: float = 0.0
    last_healthy: float = 0.0

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

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ip": self.ip,
            "status": self.status,
            "p50_ms": round(self.p50_ms, 1),
            "avg_ms": round(self.avg_ms, 1),
            "consecutive_failures": self.consecutive_failures,
            "last_healthy": self.last_healthy,
        }


_pool: list[NodeState] = []
_pool_lock = threading.Lock()

def _init_pool():
    global _pool
    _pool = [NodeState(name=n["name"], ip=n["ip"], port=n["port"], gpu=n["gpu"]) for n in NODES]

_init_pool()

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

def pick_node(require_gpu: bool = False, exclude: list[str] = None) -> dict | None:
    """Pick the fastest healthy node. Returns dict with name, ip, latency_ms or None."""
    exclude = exclude or []
    with _pool_lock:
        candidates = [
            n for n in _pool
            if n.is_healthy
            and n.name not in exclude
            and (not require_gpu or n.gpu)
        ]
        if not candidates:
            return None

        # Sort by p50 latency — fastest wins
        best = min(candidates, key=lambda n: n.p50_ms)
        return {"name": best.name, "ip": best.ip, "latency_ms": round(best.p50_ms, 1)}


def get_pool_status() -> list[dict]:
    """Get full pool status for all nodes."""
    with _pool_lock:
        return [n.to_dict() for n in _pool]


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
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)

        for node in _pool:
            cur.execute("""
                INSERT INTO lb_pool_status (node_name, status, p50_ms, avg_ms, consecutive_failures, last_healthy, updated_at)
                VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), now())
                ON CONFLICT (node_name) DO UPDATE SET
                    status = EXCLUDED.status,
                    p50_ms = EXCLUDED.p50_ms,
                    avg_ms = EXCLUDED.avg_ms,
                    consecutive_failures = EXCLUDED.consecutive_failures,
                    last_healthy = EXCLUDED.last_healthy,
                    updated_at = now()
            """, (
                node.name, node.status,
                round(node.p50_ms, 1) if node.p50_ms != float("inf") else None,
                round(node.avg_ms, 1) if node.avg_ms != float("inf") else None,
                node.consecutive_failures,
                node.last_healthy if node.last_healthy > 0 else None,
            ))

        conn.close()
    except Exception as e:
        print(f"[nova-lb] PG report failed: {e}", file=sys.stderr, flush=True)


# ── Daemon Mode ───────────────────────────────────────────────────────────────

_running = True

def _probe_loop():
    """Main probe loop — run probes and report to PG every interval."""
    while _running:
        _probe_all()
        _report_to_pg()
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
    status = get_pool_status()
    for s in status:
        print(f"  {s['name']}: {s['status']} ({s['p50_ms']}ms)", flush=True)

    # Enter probe loop
    _probe_loop()


if __name__ == "__main__":
    main()
