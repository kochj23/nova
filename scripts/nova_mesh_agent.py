#!/usr/bin/env python3
"""
nova_mesh_agent.py — Nova Mesh node agent.

Lightweight daemon that runs on every node in the Nova fleet.
- Heartbeats to PG every 15s (node health: CPU, RAM, disk)
- Checks local services and updates service_registry status
- Exposes HTTP API on :37470 (/health, /services, /metrics)
- Pings ring-peer to detect node failures

Runs as launchd (macOS) or systemd (Linux).

Written by Jordan Koch.
"""

import json
import os
import platform
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATHS = [
    Path("/opt/nova-config/mesh-agent.yaml"),
    Path.home() / ".openclaw/config/mesh-agent.yaml",
]

DEFAULT_CONFIG = {
    "node_name": socket.gethostname().split(".")[0],
    "pg_dsn": "dbname=nova_ops user=kochj host=pg-primary.digitalnoise.net",
    "heartbeat_interval": 15,
    "port": 37470,
    "peer": None,
    "services": [],
    # The single node responsible for reconciling service_registry.status for
    # EVERY registered service (the health authority). Only this node probes the
    # whole registry and writes status; all other mesh-agents only heartbeat
    # node_status. This eliminates the multi-writer fight over status.
    "registry_authority": "mac-studio",
}

# A row's status is marked 'down' only if it has failed continuously for at
# least this long (probes run every heartbeat cycle). A single transient miss
# does not flip an otherwise-healthy service to down.
REGISTRY_STALE_SECS = 60

PORT = 37470
RUNNING = True

# ── Globals ───────────────────────────────────────────────────────────────────

config = dict(DEFAULT_CONFIG)
node_metrics = {"cpu_load_1m": 0, "memory_percent": 0, "disk_percent": 0}
service_statuses = {}


def load_config():
    global config
    for p in CONFIG_PATHS:
        if p.exists():
            try:
                import yaml
                config = {**DEFAULT_CONFIG, **yaml.safe_load(p.read_text())}
                return
            except ImportError:
                raw = p.read_text()
                for line in raw.splitlines():
                    line = line.strip()
                    if ":" in line and not line.startswith("#") and not line.startswith("-"):
                        k, v = line.split(":", 1)
                        k, v = k.strip(), v.strip().strip('"').strip("'")
                        if k in DEFAULT_CONFIG:
                            if isinstance(DEFAULT_CONFIG[k], int):
                                config[k] = int(v)
                            else:
                                config[k] = v
                return


# ── System Metrics ────────────────────────────────────────────────────────────

def collect_metrics():
    global node_metrics
    try:
        load_1m = os.getloadavg()[0]
    except (OSError, AttributeError):
        load_1m = 0.0

    if platform.system() == "Darwin":
        try:
            r = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
            lines = r.stdout.splitlines()
            stats = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    v = v.strip().rstrip(".")
                    try:
                        stats[k.strip()] = int(v)
                    except ValueError:
                        pass
            page_size = 16384
            pages_free = stats.get("Pages free", 0)
            pages_active = stats.get("Pages active", 0)
            pages_inactive = stats.get("Pages inactive", 0)
            pages_wired = stats.get("Pages wired down", 0)
            pages_spec = stats.get("Pages speculative", 0)
            total = pages_free + pages_active + pages_inactive + pages_wired + pages_spec
            used = pages_active + pages_wired
            mem_pct = (used / total * 100) if total > 0 else 0
        except Exception:
            mem_pct = 0.0
    else:
        try:
            with open("/proc/meminfo") as f:
                info = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        info[parts[0].rstrip(":")] = int(parts[1])
                total = info.get("MemTotal", 1)
                avail = info.get("MemAvailable", total)
                mem_pct = ((total - avail) / total) * 100
        except Exception:
            mem_pct = 0.0

    try:
        if platform.system() == "Darwin":
            r = subprocess.run(["df", "-k", "/"], capture_output=True, text=True, timeout=5)
            lines = r.stdout.strip().splitlines()
            if len(lines) > 1:
                parts = lines[1].split()
                capacity = parts[4].rstrip("%") if len(parts) > 4 else "0"
                disk_pct = float(capacity)
            else:
                disk_pct = 0.0
        else:
            st = os.statvfs("/")
            disk_pct = ((st.f_blocks - st.f_bfree) / st.f_blocks) * 100 if st.f_blocks else 0
    except Exception:
        disk_pct = 0.0

    node_metrics = {
        "cpu_load_1m": round(load_1m, 2),
        "memory_percent": round(mem_pct, 1),
        "disk_percent": round(disk_pct, 1),
    }


# ── Service Health Checks ────────────────────────────────────────────────────

def check_service(svc: dict) -> dict:
    name = svc["name"]
    port = svc["port"]
    health_url = svc.get("health_url")
    host = svc.get("host", "127.0.0.1")

    start = time.time()
    try:
        if health_url:
            url = f"http://{host}:{port}{health_url}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = "up" if resp.status < 400 else "degraded"
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect((host, port))
            s.close()
            status = "up"
    except Exception as e:
        status = "down"

    latency_ms = int((time.time() - start) * 1000)
    return {"name": name, "status": status, "latency_ms": latency_ms}


def check_all_services():
    global service_statuses
    services = config.get("services", [])
    results = {}
    for svc in services:
        if isinstance(svc, dict):
            r = check_service(svc)
            results[r["name"]] = r
    service_statuses = results


# ── Peer Health Check ─────────────────────────────────────────────────────────

def check_peer():
    peer = config.get("peer")
    if not peer:
        return None
    try:
        url = f"http://{peer}:{PORT}/health"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"peer": peer, "status": "unreachable", "error": str(e)}


# ── Registry Health Authority ────────────────────────────────────────────────
#
# ONE node (config["registry_authority"], default mac-studio) is the sole
# authority for service_registry.status. Each cycle it probes EVERY registered
# service and sets status='up'+last_heartbeat=NOW() on success or status='down'
# on sustained failure. Every probe is wrapped in its own try/except so one bad
# service can never stall the others.

# Local host aliases — when probing a service hosted on the authority node
# itself, a LAN-IP connect can fail even though the service is bound to
# 127.0.0.1 only (e.g. big_brother :37461, novahomekit :37433). For local
# services we fall back to loopback so we report reality, not a binding quirk.
_LOCAL_HOST_ALIASES = {"127.0.0.1", "localhost", "::1"}


def _local_ips() -> set:
    ips = {"127.0.0.1", "localhost", "::1"}
    try:
        hn = socket.gethostname()
        ips.add(hn)
        for info in socket.getaddrinfo(hn, None):
            ips.add(info[4][0])
    except Exception:
        pass
    # Known LAN IP of the authority node.
    ips.add("192.168.1.6")
    return ips


def _tcp_ok(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def _http_ok(url: str, timeout: float = 5.0) -> bool:
    # Self-signed/internal TLS is the norm on the fleet (wazuh, etc.). We are
    # probing liveness, not validating certs — a successful TLS handshake to a
    # self-signed endpoint still means the service is up.
    ctx = None
    if url.startswith("https://"):
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status < 400
    except urllib.error.HTTPError as e:
        # The endpoint answered — service is alive even if the path 4xx/5xx.
        return e.code < 500
    except Exception:
        return False


def probe_registry_service(host: str, port: int, health_url, local_ips: set):
    """Probe one registered service. Returns (is_up, latency_ms).

    - health_url absolute (http://...): GET it directly.
    - health_url path (/foo): GET http://host:port/foo.
    - no health_url: plain TCP connect to host:port.
    For services on the local node, fall back to loopback if the registered
    host (often a LAN IP) is unreachable but the loopback bind is up.
    """
    start = time.time()
    hosts_to_try = [host]
    if host in local_ips and "127.0.0.1" not in hosts_to_try:
        hosts_to_try.append("127.0.0.1")

    up = False
    hu = (health_url or "").strip()
    for h in hosts_to_try:
        if hu.startswith("http://") or hu.startswith("https://"):
            # Absolute URL: rewrite the host so the loopback fallback works too.
            try:
                from urllib.parse import urlsplit, urlunsplit
                parts = urlsplit(hu)
                netloc = h if not parts.port else f"{h}:{parts.port}"
                url = urlunsplit((parts.scheme, netloc, parts.path or "/",
                                  parts.query, parts.fragment))
            except Exception:
                url = hu
            up = _http_ok(url)
        elif hu:
            up = _http_ok(f"http://{h}:{port}{hu}")
        else:
            up = _tcp_ok(h, port)
        if up:
            break

    latency_ms = int((time.time() - start) * 1000)
    return up, latency_ms


def reconcile_registry_health():
    """Authoritative reconciliation of service_registry.status.

    Runs ONLY on the authority node. Probes every registered service and writes
    the correct status. Resilient: each service probe is isolated so a single
    failure can't abort the whole pass.
    """
    try:
        import psycopg2
    except ImportError:
        return

    node_name = config["node_name"]
    if node_name != config.get("registry_authority", "mac-studio"):
        return  # Not the authority — do not touch service_registry.status.

    pg_dsn = config["pg_dsn"]
    local_ips = _local_ips()

    try:
        conn = psycopg2.connect(pg_dsn, connect_timeout=5)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            SELECT service_name, node_name, host(host)::text, port, health_url,
                   EXTRACT(EPOCH FROM (NOW() - last_heartbeat)) AS age_secs
            FROM service_registry
        """)
        rows = cur.fetchall()
    except Exception as e:
        print(f"[mesh-agent] registry reconcile: load failed: {e}",
              file=sys.stderr, flush=True)
        return

    up_count = 0
    down_count = 0
    for service_name, svc_node, host, port, health_url, age_secs in rows:
        try:
            is_up, latency_ms = probe_registry_service(
                host, port, health_url, local_ips)
        except Exception as e:
            print(f"[mesh-agent] probe error {service_name}@{svc_node}: {e}",
                  file=sys.stderr, flush=True)
            continue

        try:
            if is_up:
                cur.execute("""
                    UPDATE service_registry
                       SET status = 'up', last_heartbeat = NOW()
                     WHERE service_name = %s AND node_name = %s
                """, (service_name, svc_node))
                up_count += 1
            else:
                # Only flip to down once the failure has persisted, so a single
                # transient miss doesn't churn a healthy service.
                sustained = age_secs is None or age_secs >= REGISTRY_STALE_SECS
                new_status = "down" if sustained else "degraded"
                cur.execute("""
                    UPDATE service_registry
                       SET status = %s
                     WHERE service_name = %s AND node_name = %s
                """, (new_status, service_name, svc_node))
                if new_status == "down":
                    down_count += 1
            cur.execute("""
                INSERT INTO health_checks
                    (service_name, node_name, checked_by, status, latency_ms, checked_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
            """, (service_name, svc_node, node_name,
                  "up" if is_up else "down", latency_ms))
        except Exception as e:
            print(f"[mesh-agent] registry write {service_name}: {e}",
                  file=sys.stderr, flush=True)

    try:
        cur.close()
        conn.close()
    except Exception:
        pass
    print(f"[mesh-agent] registry reconciled: {up_count} up, {down_count} down "
          f"({len(rows)} total)", flush=True)


# ── PG Heartbeat ─────────────────────────────────────────────────────────────

def heartbeat_to_pg():
    try:
        import psycopg2
    except ImportError:
        return

    node_name = config["node_name"]
    pg_dsn = config["pg_dsn"]

    try:
        conn = psycopg2.connect(pg_dsn, connect_timeout=5)
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("""
            UPDATE node_status SET
                status = 'up',
                last_heartbeat = NOW(),
                load_avg_1m = %s,
                memory_percent = %s,
                disk_percent = %s,
                updated_at = NOW()
            WHERE node_name = %s
        """, (
            node_metrics["cpu_load_1m"],
            node_metrics["memory_percent"],
            node_metrics["disk_percent"],
            node_name,
        ))

        # NOTE: service_registry.status is NOT written here anymore. A single
        # authority node reconciles it for ALL services via
        # reconcile_registry_health(). This agent only owns node_status
        # heartbeats for its own node, which prevents the previous multi-writer
        # fight that left stale/contradictory service status.

        # Record peer check if peer is down
        peer_result = check_peer()
        if peer_result and peer_result.get("status") == "unreachable":
            peer_node = config.get("peer_node_name", config.get("peer", "unknown"))
            cur.execute("""
                UPDATE node_status SET status = 'suspect', updated_at = NOW()
                WHERE node_name = %s AND last_heartbeat < NOW() - INTERVAL '60 seconds'
            """, (peer_node,))

        cur.close()
        conn.close()
    except Exception as e:
        print(f"[mesh-agent] PG heartbeat failed: {e}", file=sys.stderr, flush=True)


# ── HTTP API ──────────────────────────────────────────────────────────────────

class MeshHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            self._json_response(200, {
                "status": "ok",
                "node": config["node_name"],
                "uptime": int(time.time() - _start_time),
            })
        elif self.path == "/services":
            self._json_response(200, {
                "node": config["node_name"],
                "services": service_statuses,
            })
        elif self.path == "/metrics":
            self._json_response(200, {
                "node": config["node_name"],
                "metrics": node_metrics,
                "services": service_statuses,
            })
        else:
            self._json_response(404, {"error": "not found"})

    def _json_response(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ── Main Loop ─────────────────────────────────────────────────────────────────

_start_time = time.time()


def heartbeat_loop():
    while RUNNING:
        try:
            collect_metrics()
            check_all_services()
            heartbeat_to_pg()
            # Single authority reconciles status for EVERY registered service.
            # No-op on non-authority nodes.
            reconcile_registry_health()
        except Exception as e:
            print(f"[mesh-agent] heartbeat error: {e}", file=sys.stderr, flush=True)
        time.sleep(config["heartbeat_interval"])


def main():
    global RUNNING

    def _shutdown(sig, frame):
        global RUNNING
        RUNNING = False
        print("[mesh-agent] shutting down", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGHUP, lambda s, f: load_config())

    load_config()
    print(f"[mesh-agent] Starting on {config['node_name']} (port {config['port']})", flush=True)
    print(f"[mesh-agent] Monitoring {len(config.get('services', []))} services", flush=True)
    if config.get("peer"):
        print(f"[mesh-agent] Peer: {config['peer']}", flush=True)

    # Start heartbeat thread
    t = threading.Thread(target=heartbeat_loop, daemon=True)
    t.start()

    # Start HTTP server
    server = HTTPServer(("0.0.0.0", config["port"]), MeshHandler)
    print(f"[mesh-agent] HTTP API listening on 0.0.0.0:{config['port']}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        RUNNING = False
        server.server_close()


if __name__ == "__main__":
    main()
