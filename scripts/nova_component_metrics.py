#!/opt/homebrew/bin/python3
"""
nova_component_metrics.py — Nova-MIB external collector ("Nova watches Nova").

SNMP-style per-component vital stats for every Nova software component, polled
EXTERNALLY (non-invasive — no code injected into the live daemons). Each cycle
this probes every Nova component and records standard vitals to
telemetry.nova_components, the "SNMP device table for Nova herself".

Vitals per component:
  up               1/0   — HTTP health_url or TCP connect to host:port
  rss_mb           real  — process resident memory (psutil, matched by script/port)
  cpu_pct          real  — process CPU% (psutil)
  uptime_s         bigint— from /health uptime_s field if exposed, else proc create_time
  last_write_age_s real  — freshness of the component's newest output row (silent-failure detector)
  healthy          1/0   — up AND (no fresh-data SLA OR data is fresh)
  note             text  — short status string

Usage:
  nova_component_metrics.py --once     run one cycle and exit (prints summary)
  nova_component_metrics.py            run one cycle (default; suitable for scheduler)

Address by the scheduler every 1m. Safe to run unattended.

Written by Jordan Koch / Nova.
"""

import nova_dsn as _nova_dsn  # noqa: E402
import sys
import os
import socket
import time
import json
import argparse
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

try:
    import psutil  # noqa
    HAVE_PSUTIL = True
except Exception:
    HAVE_PSUTIL = False

try:
    import requests  # noqa
    HAVE_REQUESTS = True
except Exception:
    HAVE_REQUESTS = False

import psycopg2

# ── Config ──────────────────────────────────────────────────────────────────

DB_DSN = _nova_dsn.pg_url("nova_ops")
LOCAL_IPS = {"127.0.0.1", "localhost", "192.168.1.6", "::1"}
HOSTNAME = socket.gethostname()
PROBE_TIMEOUT = 4.0

LOG_PATH = str(Path.home()) + "/.openclaw/logs/component_metrics.log"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("component_metrics")

# ── Component inventory ──────────────────────────────────────────────────────
# Each component:
#   name        : logical component id (matches the dashboard rows)
#   host, port  : where to probe for "up"
#   health_path : optional HTTP health path; if None, fall back to TCP connect
#   uptime_field: JSON key in health body that exposes uptime seconds (optional)
#   proc        : substring to match against process cmdline for RSS/CPU (local only)
#   write_sql   : SQL returning seconds-since-newest-row for silent-failure detect (optional)
#   stale_sla_s : if last_write_age_s exceeds this, component is "stale"/unhealthy
#
# Component list is seeded from service_registry + the launchd net.digitalnoise.*
# / com.nova.* services. write_sql / proc / stale_sla are layered on by hand for
# the components that have a known output table or a known local process.

COMPONENTS = [
    # ── Core brain (priority services, all expose HTTP health) ──
    {"name": "gateway", "host": "192.168.1.2", "port": 18792, "health_path": "/health",
     "uptime_field": "uptime_s", "proc": "nova_gateway",
     # inference_latency only gets rows on actual inference; track age informationally
     # (no SLA -> a long idle gap is not a "silent failure", just low traffic)
     "write_sql": "SELECT EXTRACT(EPOCH FROM (now()-max(timestamp))) FROM inference_latency"},
    {"name": "memory_server", "host": "127.0.0.1", "port": 18790, "health_path": "/health",
     "proc": "memory_server"},
    {"name": "scheduler", "host": "127.0.0.1", "port": 37460, "health_path": "/status",
     "uptime_field": "uptime_s", "proc": "nova_scheduler.py",
     "write_sql": "SELECT EXTRACT(EPOCH FROM (now()-to_timestamp(max(started_at)/1000.0))) FROM scheduler_runs",
     "stale_sla_s": 600},
    {"name": "big_brother", "host": "127.0.0.1", "port": 37461, "health_path": "/bb/status",
     "uptime_field": "uptime_s", "proc": "nova_big_brother.py"},

    # ── Inference backends ──
    {"name": "ollama", "host": "127.0.0.1", "port": 11434, "health_path": "/api/version",
     "proc": "Ollama.app/Contents/Resources/ollama serve"},
    {"name": "llama_server", "host": "127.0.0.1", "port": 11435, "health_path": "/v1/models",
     "proc": "llama-server"},
    {"name": "mlx_server", "host": "127.0.0.1", "port": 5050, "health_path": "/v1/models",
     "proc": "mlx"},

    # ── Pollers / monitors with known output tables (silent-failure detection) ──
    {"name": "snmp_poller", "host": "192.168.1.2", "port": 37463, "health_path": "/health",   # 2026-10-03: runs on nova-core (systemd), not .6
     "write_sql": "SELECT EXTRACT(EPOCH FROM (now()-max(timestamp))) FROM snmp_metrics",
     "stale_sla_s": 600},
    {"name": "weather_receiver", "host": "127.0.0.1", "port": 8087, "health_path": None,
     "proc": "nova_weather_receiver.py",
     "write_sql": "SELECT EXTRACT(EPOCH FROM (now()-max(ts))) FROM telemetry.weather",
     "stale_sla_s": 900},
    {"name": "unifi_poller", "proc": "nova_unifi_poller.py",
     "write_sql": "SELECT EXTRACT(EPOCH FROM (now()-max(ts))) FROM telemetry.unifi_metrics",
     "stale_sla_s": 900},
    {"name": "energy_poller", "proc": "nova_energy_poller.py",
     "write_sql": "SELECT EXTRACT(EPOCH FROM (now()-max(ts))) FROM telemetry.energy",
     "stale_sla_s": 900},
    {"name": "climate_poller", "proc": "nova_climate_poller.py",
     "write_sql": "SELECT EXTRACT(EPOCH FROM (now()-max(ts))) FROM telemetry.climate",
     "stale_sla_s": 1800},
    {"name": "av_poller", "proc": "nova_av_poller.py",
     "write_sql": "SELECT EXTRACT(EPOCH FROM (now()-max(ts))) FROM telemetry.av_state",
     "stale_sla_s": 1800},
    {"name": "ble_monitor", "proc": "nova_ble_monitor.py",
     "write_sql": "SELECT EXTRACT(EPOCH FROM (now()-max(ts))) FROM telemetry.bluetooth",
     "stale_sla_s": 900},
    {"name": "ha_poller", "proc": "nova_ha_poller.py",
     "write_sql": "SELECT EXTRACT(EPOCH FROM (now()-max(ts))) FROM telemetry.ha_sensors",
     "stale_sla_s": 900},
    {"name": "presence_engine", "host": "127.0.0.1", "port": 37465, "health_path": "/occupancy",
     "proc": "nova_presence_engine.py",
     "write_sql": "SELECT EXTRACT(EPOCH FROM (now()-max(ts))) FROM telemetry.presence",
     "stale_sla_s": 900},
    {"name": "mesh_agent", "proc": "nova_mesh_agent.py",
     "write_sql": "SELECT EXTRACT(EPOCH FROM (now()-max(last_heartbeat))) FROM node_status",
     "stale_sla_s": 600},

    # ── Endpoint / network monitors (HTTP health) ──
    {"name": "endpoint_monitor", "host": "127.0.0.1", "port": 37469, "health_path": "/health",
     "proc": "nova_endpoint_monitor.py"},
    {"name": "syslog", "host": "127.0.0.1", "port": 37462, "health_path": "/health",
     "proc": "nova_syslog"},

    # ── Web / control surfaces ──
    {"name": "novahomekit", "host": "127.0.0.1", "port": 37433, "health_path": "/api/status",
     "proc": "homekit"},
    {"name": "capacity", "host": "127.0.0.1", "port": 37468, "health_path": None,
     "proc": "nova_capacity.py"},

    # ── Data stores ──
    {"name": "postgresql", "host": "127.0.0.1", "port": 5432, "health_path": None,
     "proc": "postgres"},
    {"name": "redis", "host": "127.0.0.1", "port": 6379, "health_path": None,
     "proc": "redis-server"},
    {"name": "pgbouncer", "proc": "pgbouncer"},
]

# ── Probes ───────────────────────────────────────────────────────────────────


def http_probe(host, port, path, uptime_field):
    """Return (up, uptime_s_or_None) via HTTP health endpoint."""
    url = f"http://{host}:{port}{path}"
    try:
        if HAVE_REQUESTS:
            r = requests.get(url, timeout=PROBE_TIMEOUT)
            ok = r.status_code < 500
            body = r.text
        else:
            import urllib.request
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
                ok = resp.status < 500
                body = resp.read().decode("utf-8", "replace")
        uptime = None
        if ok and uptime_field:
            try:
                uptime = int(float(json.loads(body).get(uptime_field)))
            except Exception:
                uptime = None
        return (1 if ok else 0), uptime
    except Exception:
        return 0, None


def tcp_probe(host, port):
    """Return 1 if a TCP connect succeeds, else 0."""
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT):
            return 1
    except Exception:
        return 0


def snapshot_processes():
    """Snapshot the local process list ONCE per cycle as a list of (proc, cmd, name).

    Perf: find_process() used to run a fresh psutil.process_iter() for every
    component (~26 full process-table scans per cycle). Building the list once and
    matching substrings against it removes that redundant work while keeping the
    exact same match semantics. Returns None on any failure so find_process() can
    reproduce the old "psutil errored -> no match" behavior (never falls back to ps
    when HAVE_PSUTIL is True).

    Tradeoff: the process table is captured once at cycle start rather than at each
    per-component probe, so a process that appears/exits mid-cycle is seen
    consistently across all components. Negligible for a 1-minute external poller.
    """
    if not HAVE_PSUTIL:
        return None
    try:
        procs = []
        for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            try:
                cmd = " ".join(p.info.get("cmdline") or [])
                nm = p.info.get("name") or ""
                procs.append((p, cmd, nm))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return procs
    except Exception:
        return None


def find_process(match, snapshot=None):
    """Return (rss_mb, cpu_pct, create_time) for the matching local process, else (None,None,None).

    `snapshot` is the per-cycle process list from snapshot_processes(); passing None
    is equivalent to an empty scan under psutil (no match).
    """
    if not match:
        return None, None, None
    if HAVE_PSUTIL:
        try:
            best = None
            for p, cmd, nm in (snapshot or []):
                if match in cmd or match in nm:
                    # prefer the longest cmdline match (the real daemon, not a wrapper)
                    if best is None or len(cmd) > best[1]:
                        best = (p, len(cmd))
            if best is None:
                return None, None, None
            p = best[0]
            try:
                p.cpu_percent(None)
                time.sleep(0.25)
                cpu = p.cpu_percent(None)
                rss = p.memory_info().rss / (1024 * 1024)
                ct = p.create_time()
                return round(rss, 1), round(cpu, 1), ct
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return None, None, None
        except Exception:
            return None, None, None
    # ── ps fallback ──
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid,rss,pcpu,etimes,command"],
            capture_output=True, text=True, timeout=8,
        ).stdout
        for line in out.splitlines()[1:]:
            if match in line:
                parts = line.split(None, 4)
                if len(parts) >= 5 and "ps -axo" not in parts[4]:
                    rss = int(parts[1]) / 1024.0
                    cpu = float(parts[2])
                    etimes = int(parts[3])
                    return round(rss, 1), round(cpu, 1), time.time() - etimes
    except Exception:
        pass
    return None, None, None


def write_age(conn, sql):
    """Return seconds since the newest output row, or None."""
    if not sql:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            row = cur.fetchone()
            conn.commit()
            if row and row[0] is not None:
                return round(float(row[0]), 1)
            return None  # table empty / no rows -> treat as unknown
    except Exception as e:
        conn.rollback()
        log.warning("write_age failed for SQL %r: %s", sql[:60], e)
        return None


# ── Collection ───────────────────────────────────────────────────────────────


def collect(conn):
    now = datetime.now(timezone.utc)
    rows = []
    # Snapshot the process table ONCE per cycle (instead of one process_iter per
    # component) — see snapshot_processes() for match-semantics/tradeoff notes.
    proc_snapshot = snapshot_processes()
    for c in COMPONENTS:
        name = c["name"]
        host = c.get("host")
        port = c.get("port")
        health_path = c.get("health_path")
        notes = []
        up, uptime = 0, None

        if host and port is not None:
            if health_path is not None:
                up, uptime = http_probe(host, port, health_path, c.get("uptime_field"))
                if up == 0:  # health endpoint failed; still try a raw TCP connect
                    tcp = tcp_probe(host, port)
                    if tcp:
                        up = 1
                        notes.append("tcp-only (health failed)")
            else:
                up = tcp_probe(host, port)
        # process-only components (no host/port): up is inferred from process presence below

        # RSS / CPU / uptime from local process
        rss = cpu = None
        is_local = (host in LOCAL_IPS) or (host is None)
        if is_local:
            rss, cpu, ct = find_process(c.get("proc"), proc_snapshot)
            if uptime is None and ct:
                uptime = int(time.time() - ct)
            if (host is None or port is None):
                # process-only component: up == process exists
                up = 1 if rss is not None else 0
            elif up == 0 and rss is not None:
                notes.append("proc alive, port down")

        lwa = write_age(conn, c.get("write_sql"))
        sla = c.get("stale_sla_s")

        # healthy: up AND (no data SLA OR data is fresh within SLA)
        healthy = 0
        if up == 1:
            if sla is None:
                healthy = 1
            elif lwa is not None and lwa <= sla:
                healthy = 1
            elif lwa is None:
                # poller has an output-table SLA but the table is EMPTY -> it is
                # running yet has never produced data: a silent failure (#510 case).
                healthy = 0
                notes.append("STALE: up but output table empty (no data ever written)")
            else:
                healthy = 0
                notes.append(f"STALE: last write {int(lwa)}s > {sla}s SLA")

        rows.append((
            now, name, HOSTNAME, up,
            rss, cpu, uptime, lwa, healthy,
            "; ".join(notes) if notes else None,
        ))

    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO telemetry.nova_components
               (ts, component, instance, up, rss_mb, cpu_pct, uptime_s,
                last_write_age_s, healthy, note)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            rows,
        )
        conn.commit()
    return rows


def main():
    ap = argparse.ArgumentParser(description="Nova-MIB component metrics collector")
    ap.add_argument("--once", action="store_true", help="run one cycle and print a summary")
    args = ap.parse_args()

    conn = psycopg2.connect(DB_DSN)
    try:
        rows = collect(conn)
    finally:
        conn.close()

    up_n = sum(1 for r in rows if r[3] == 1)
    stale = [r for r in rows if r[8] == 0 and r[3] == 1]
    log.info("cycle: %d components, %d up, %d silently-stale", len(rows), up_n, len(stale))

    if args.once:
        print(f"Nova-MIB cycle @ {rows[0][0].isoformat()}  "
              f"({len(rows)} components, {up_n} up, {len(stale)} silently-stale)\n")
        hdr = f"{'component':<18}{'up':>3}{'rss_mb':>9}{'cpu%':>7}{'uptime_s':>10}{'wr_age_s':>10}{'ok':>4}  note"
        print(hdr)
        print("-" * len(hdr))
        for r in rows:
            _, name, _, up, rss, cpu, upt, lwa, ok, note = r
            print(f"{name:<18}{up:>3}"
                  f"{(f'{rss:.1f}' if rss is not None else '-'):>9}"
                  f"{(f'{cpu:.1f}' if cpu is not None else '-'):>7}"
                  f"{(str(upt) if upt is not None else '-'):>10}"
                  f"{(f'{lwa:.0f}' if lwa is not None else '-'):>10}"
                  f"{ok:>4}  {note or ''}")
        if stale:
            print("\n*** SILENTLY-STALE (up but not writing data within SLA) ***")
            for r in stale:
                print(f"  - {r[1]}: {r[9]}")


if __name__ == "__main__":
    main()
