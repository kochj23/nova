#!/usr/bin/env python3
"""
nova_capacity.py — Capacity calculator for all Nova-accessible hosts.

Aggregates SNMP metrics + local/SSH collection into capacity_snapshots table.
Computes headroom percentages, fires alerts when thresholds breach, and exposes
an HTTP API for dashboards.

Runs as a persistent launchd service.

Written by Jordan Koch.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import asyncpg
    from aiohttp import web
except ImportError as e:
    print(f"FATAL: missing dependency: {e}", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VERSION = "1.0.0"
HTTP_PORT = 37464
BIND_ADDR = "0.0.0.0"
DB_DSN = "postgresql://kochj@pg-primary.digitalnoise.net:5432/nova_ops"
LOG_FILE = Path.home() / ".openclaw/logs/nova_capacity.log"
SNAPSHOT_INTERVAL = 300
RETENTION_DAYS = 90

HOST_CORES = {
    "mac-studio": 32,
    "mac-mini": 14,
    "tv-movies-mini": 12,
    "udm-pro": 4,
    "synology-nas": 4,
    "nova-core": 4,
    "nova-core2": 16,
    "nova-core5": 4,
}

MACOS_HOSTS = {
    "mac-studio": {"ip": "127.0.0.1", "local": True},
    "mac-mini": {"ip": "192.168.1.190", "local": False},
    "tv-movies-mini": {"ip": "192.168.1.7", "local": False},
}

# Devices where high memory use is expected (filesystem cache, not pressure)
MEM_CACHE_HOSTS = {"synology-nas", "udm-pro"}

DISK_MOUNT_FILTER = {
    "mac-studio": ["/", "/System/Volumes/Data", "/Volumes/Data", "/Volumes/MoreData"],
    "mac-mini": ["/", "/System/Volumes/Data"],
    "tv-movies-mini": ["/", "/System/Volumes/Data"],
    "synology-nas": ["/", "/volume1"],
    "nova-core5": ["/"],
    "nova-core": ["/"],
    "nova-core2": ["/"],
}

SSH_HOSTS = {
    "synology-nas": "192.168.1.11",
    "nova-core5": "192.168.1.10",
    "nova-core": "192.168.1.2",
    "nova-core2": "192.168.1.86",
}

_shutdown = False
_pool = None
_start_time = time.time()
_latest_snapshot = {}
_stats = {"snapshots_total": 0, "alerts_fired": 0, "last_run": None}

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[capacity {ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(text):
    try:
        nova_config.post_both(text, slack_channel=nova_config.SLACK_BB)
    except Exception as e:
        log(f"Notification failed: {e}", "WARN")


async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    return _pool


# ── macOS Memory Collection ──────────────────────────────────────────────────

def _parse_vm_stat(output, page_size=16384):
    """Parse vm_stat output into used/free MB."""
    pages = {}
    for line in output.strip().split("\n"):
        if ":" not in line:
            continue
        key, val = line.rsplit(":", 1)
        val = val.strip().rstrip(".")
        try:
            pages[key.strip()] = int(val)
        except ValueError:
            continue

    free = pages.get("Pages free", 0) + pages.get("Pages speculative", 0)
    inactive = pages.get("Pages inactive", 0)
    active = pages.get("Pages active", 0)
    wired = pages.get("Pages wired down", 0)
    compressed = pages.get("Pages occupied by compressor", 0)

    used_pages = active + wired + compressed
    free_pages = free + inactive

    used_mb = (used_pages * page_size) / (1024 * 1024)
    free_mb = (free_pages * page_size) / (1024 * 1024)
    total_mb = ((used_pages + free_pages) * page_size) / (1024 * 1024)

    return total_mb, used_mb, free_mb


def get_local_memory():
    """Get memory stats from local macOS host."""
    try:
        r = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            page_line = r.stdout.split("\n")[0]
            page_size = int(page_line.split("page size of ")[1].split(" ")[0])
            return _parse_vm_stat(r.stdout, page_size)
    except Exception as e:
        log(f"Local vm_stat failed: {e}", "WARN")
    return None, None, None


def get_remote_memory(ip):
    """Get memory stats from remote macOS host via SSH."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", ip, "vm_stat"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            page_line = r.stdout.split("\n")[0]
            page_size = int(page_line.split("page size of ")[1].split(" ")[0])
            return _parse_vm_stat(r.stdout, page_size)
    except Exception as e:
        log(f"SSH vm_stat to {ip} failed: {e}", "WARN")
    return None, None, None


def get_local_disk():
    """Get disk usage for local mounts."""
    disks = []
    try:
        r = subprocess.run(["df", "-g"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            for line in r.stdout.strip().split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 9:
                    mount = " ".join(parts[8:])
                    try:
                        size_gb = int(parts[1])
                        used_gb = int(parts[2])
                        avail_gb = int(parts[3])
                        pct = int(parts[4].rstrip("%"))
                        if size_gb > 0:
                            disks.append({
                                "mount": mount,
                                "size_gb": size_gb,
                                "used_gb": used_gb,
                                "avail_gb": avail_gb,
                                "percent": pct,
                            })
                    except (ValueError, IndexError):
                        continue
    except Exception as e:
        log(f"Local df failed: {e}", "WARN")
    return disks


def get_remote_disk(ip):
    """Get disk usage from remote macOS host via SSH (df -g format)."""
    disks = []
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", ip, "df -g"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            for line in r.stdout.strip().split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 9:
                    mount = " ".join(parts[8:])
                    try:
                        size_gb = int(parts[1])
                        used_gb = int(parts[2])
                        avail_gb = int(parts[3])
                        pct = int(parts[4].rstrip("%"))
                        if size_gb > 0:
                            disks.append({
                                "mount": mount,
                                "size_gb": size_gb,
                                "used_gb": used_gb,
                                "avail_gb": avail_gb,
                                "percent": pct,
                            })
                    except (ValueError, IndexError):
                        continue
    except Exception as e:
        log(f"SSH df to {ip} failed: {e}", "WARN")
    return disks


def get_linux_disk(ip):
    """Get disk usage from remote Linux host via SSH (df -BG format)."""
    disks = []
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", ip, "df -BG"],
            capture_output=True, text=True, timeout=10,
        )
        output = r.stdout.strip() if r.stdout else ""
        if output:
            for line in output.split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 6:
                    mount = parts[5]
                    try:
                        size_gb = int(parts[1].rstrip("G"))
                        used_gb = int(parts[2].rstrip("G"))
                        avail_gb = int(parts[3].rstrip("G"))
                        pct = int(parts[4].rstrip("%"))
                        if size_gb > 0:
                            disks.append({
                                "mount": mount,
                                "size_gb": size_gb,
                                "used_gb": used_gb,
                                "avail_gb": avail_gb,
                                "percent": pct,
                            })
                    except (ValueError, IndexError):
                        continue
    except Exception as e:
        log(f"SSH df -BG to {ip} failed: {e}", "WARN")
    return disks


# ── Snapshot Builder ─────────────────────────────────────────────────────────

async def build_snapshot(device_name, device_ip):
    """Build a capacity snapshot for one host."""
    pool = await get_pool()

    async with pool.acquire() as conn:
        cpu_row = await conn.fetchrow("""
            SELECT
                (SELECT metric_value FROM snmp_metrics WHERE device_name=$1 AND metric_name='cpu_load_1min' ORDER BY timestamp DESC LIMIT 1) as load1,
                (SELECT metric_value FROM snmp_metrics WHERE device_name=$1 AND metric_name='cpu_load_5min' ORDER BY timestamp DESC LIMIT 1) as load5,
                (SELECT metric_value FROM snmp_metrics WHERE device_name=$1 AND metric_name='cpu_load_15min' ORDER BY timestamp DESC LIMIT 1) as load15
        """, device_name)

    cores = HOST_CORES.get(device_name, 4)
    load1 = cpu_row["load1"] if cpu_row and cpu_row["load1"] else 0
    load5 = cpu_row["load5"] if cpu_row and cpu_row["load5"] else 0
    load15 = cpu_row["load15"] if cpu_row and cpu_row["load15"] else 0
    cpu_headroom = max(0, 100.0 * (1.0 - load5 / cores))
    # cpu_headroom SATURATES at 0 the instant load reaches core count, so it can't tell a
    # healthy 1x box from a genuinely-drowning 3x box — both read 0, and the old status gated
    # 'crit' at headroom<10 (load 0.9x cores), paging on well-utilized hardware. The honest
    # signal is load-per-core (run-queue depth normalized by cores), the classic *nix rule:
    # under 1x is fine, 1-2x is 'busy, watch it', past ~2x sustained is real pressure. Status
    # is gated on THIS, not the saturating headroom. (Jordan's Solaris-admin heuristic, 2026-08-11)
    cpu_load_ratio = (load5 / cores) if cores else 0.0

    mem_total, mem_used, mem_free = None, None, None
    swap_headroom = None   # % of swap still free; None = unknown/macOS (don't gate on it)
    if device_name in MACOS_HOSTS:
        host_info = MACOS_HOSTS[device_name]
        if host_info["local"]:
            mem_total, mem_used, mem_free = get_local_memory()
        else:
            mem_total, mem_used, mem_free = get_remote_memory(host_info["ip"])
    else:
        async with pool.acquire() as conn:
            mem_row = await conn.fetchrow("""
                SELECT
                    (SELECT metric_value FROM snmp_metrics WHERE device_name=$1 AND metric_name='mem_total_real' ORDER BY timestamp DESC LIMIT 1) as total,
                    (SELECT metric_value FROM snmp_metrics WHERE device_name=$1 AND metric_name='mem_avail_real' ORDER BY timestamp DESC LIMIT 1) as avail,
                    (SELECT metric_value FROM snmp_metrics WHERE device_name=$1 AND metric_name='mem_buffer' ORDER BY timestamp DESC LIMIT 1) as buffer,
                    (SELECT metric_value FROM snmp_metrics WHERE device_name=$1 AND metric_name='mem_cached' ORDER BY timestamp DESC LIMIT 1) as cached,
                    (SELECT metric_value FROM snmp_metrics WHERE device_name=$1 AND metric_name='mem_total_swap' ORDER BY timestamp DESC LIMIT 1) as swap_total,
                    (SELECT metric_value FROM snmp_metrics WHERE device_name=$1 AND metric_name='mem_avail_swap' ORDER BY timestamp DESC LIMIT 1) as swap_avail
            """, device_name)
        if mem_row and mem_row["total"] and mem_row["total"] > 0:
            mem_total = mem_row["total"] / 1024.0
            # TRUE available memory = MemFree + reclaimable Buffers + Cached. memAvailReal alone
            # is just MemFree, which reads ~1% on a healthy cache-heavy box and cried wolf all
            # night. Buffers/cached collected 2026-08-10; COALESCE to 0 so a host that hasn't
            # reported them yet degrades to the old (conservative) free-only number.
            mem_free = ((mem_row["avail"] or 0) + (mem_row["buffer"] or 0) + (mem_row["cached"] or 0)) / 1024.0
            mem_free = min(mem_free, mem_total)   # never exceed total
            mem_used = mem_total - mem_free
            # Net-SNMP's mem_avail_real is MemFree — it EXCLUDES reclaimable buffers/cache,
            # so a Postgres/cache-heavy box reads ~1-3% "free" while the kernel's real
            # MemAvailable is fine. Swap is the honest pressure signal: a box only actually
            # struggles when it's exhausting swap. Gate the memory alert on that so cache
            # doesn't cry wolf (nova-core5 flapped "critical 1%" all of 2026-07-30 with
            # swap untouched), while a genuinely swap-full box (nova-core did) still alerts.
            if mem_row["swap_total"] and mem_row["swap_total"] > 0:
                swap_headroom = 100.0 * ((mem_row["swap_avail"] or 0) / mem_row["swap_total"])
            else:
                swap_headroom = 100.0   # no swap configured — don't manufacture pressure

    mem_headroom = None
    if mem_total and mem_total > 0:
        mem_headroom = 100.0 * (mem_free / mem_total)
    # For Linux SNMP hosts, only let low free-memory escalate when swap is ALSO under
    # pressure (>60% used); otherwise the low "free" is just cache and the box is healthy.
    _swap_pressured = swap_headroom is not None and swap_headroom <= 40

    disks = []
    if device_name in MACOS_HOSTS:
        host_info = MACOS_HOSTS[device_name]
        if host_info["local"]:
            all_disks = get_local_disk()
        else:
            all_disks = get_remote_disk(host_info["ip"])
        allowed = DISK_MOUNT_FILTER.get(device_name)
        if allowed:
            disks = [d for d in all_disks if d["mount"] in allowed]
        else:
            disks = [d for d in all_disks if not d["mount"].startswith("/System/Volumes/") or d["mount"] == "/System/Volumes/Data"]
    elif device_name in SSH_HOSTS:
        all_disks = get_linux_disk(SSH_HOSTS[device_name])
        allowed = DISK_MOUNT_FILTER.get(device_name)
        if allowed:
            disks = [d for d in all_disks if d["mount"] in allowed]
        else:
            disks = all_disks

    disk_worst = max((d["percent"] for d in disks), default=0)

    status = "ok"
    # mem_matters: a host whose "free" memory is meaningfully low. For Linux SNMP hosts
    # that additionally requires swap pressure (see swap_headroom above), so reclaimable
    # cache never trips the alert. macOS hosts (swap_headroom is None) keep the old check.
    _mem_lowfree = mem_headroom is not None and device_name not in MEM_CACHE_HOSTS
    _linux = swap_headroom is not None
    mem_matters = _mem_lowfree and (_swap_pressured or not _linux)
    # CPU: gate on load-per-core, the *nix run-queue rule — NOT the saturating headroom.
    # >1.5x cores sustained = busy enough to watch; >2.0x = genuine pressure. A box humming
    # along at 0.8-1.2x its core count is well-utilized, not in trouble, and no longer pages.
    CPU_WARN_RATIO, CPU_CRIT_RATIO = 1.5, 2.0
    if cpu_load_ratio > CPU_WARN_RATIO or (mem_matters and mem_headroom < 15) or disk_worst > 85:
        status = "warn"
    if cpu_load_ratio > CPU_CRIT_RATIO or (mem_matters and mem_headroom < 5) or disk_worst > 92:
        status = "crit"

    snapshot = {
        "device_ip": device_ip,
        "device_name": device_name,
        "cpu_load_1m": load1,
        "cpu_load_5m": load5,
        "cpu_load_15m": load15,
        "cpu_cores": cores,
        "cpu_headroom_pct": round(cpu_headroom, 1),
        "mem_total_mb": round(mem_total, 1) if mem_total else None,
        "mem_used_mb": round(mem_used, 1) if mem_used else None,
        "mem_free_mb": round(mem_free, 1) if mem_free else None,
        "mem_headroom_pct": round(mem_headroom, 1) if mem_headroom is not None else None,
        "disks": disks,
        "disk_worst_pct": disk_worst,
        "overall_status": status,
    }

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO capacity_snapshots
            (device_ip, device_name, cpu_load_1m, cpu_load_5m, cpu_load_15m, cpu_cores,
             cpu_headroom_pct, mem_total_mb, mem_used_mb, mem_free_mb, mem_headroom_pct,
             disks, disk_worst_pct, overall_status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        """, device_ip, device_name, load1, load5, load15, cores,
            round(cpu_headroom, 1),
            round(mem_total, 1) if mem_total else None,
            round(mem_used, 1) if mem_used else None,
            round(mem_free, 1) if mem_free else None,
            round(mem_headroom, 1) if mem_headroom is not None else None,
            json.dumps(disks),
            float(disk_worst),
            status)

    return snapshot


# ── Alert Evaluation ─────────────────────────────────────────────────────────

_active_alerts = {}


async def evaluate_alerts(snapshot):
    """Check snapshot against alert_rules and fire notifications."""
    device = snapshot["device_name"]
    pool = await get_pool()

    async with pool.acquire() as conn:
        rules = await conn.fetch("SELECT * FROM alert_rules WHERE enabled = true")

    for rule in rules:
        metric = rule["metric"]
        condition = rule["condition"]
        threshold = rule["threshold"]
        severity = rule["severity"]

        # Skip memory alerts for hosts where high usage is normal (filesystem cache)
        if metric == "mem_headroom_pct" and device in MEM_CACHE_HOSTS:
            continue

        value = None
        if metric == "disk_percent":
            value = snapshot["disk_worst_pct"]
        elif metric == "cpu_load_5min":
            value = snapshot["cpu_load_5m"]
        elif metric == "mem_headroom_pct":
            value = snapshot["mem_headroom_pct"]

        if value is None:
            continue

        triggered = False
        if condition == "gt" and value > threshold:
            triggered = True
        elif condition == "lt" and value < threshold:
            triggered = True

        alert_key = f"{device}:{rule['name']}"

        if triggered:
            if alert_key not in _active_alerts:
                _active_alerts[alert_key] = time.time()
                _stats["alerts_fired"] += 1
                icon = ":fire:" if severity == "critical" else ":warning:"
                notify(
                    f"{icon} *Capacity Alert* [{severity.upper()}] — {device}\n"
                    f"  {metric} = {value:.1f} (threshold: {condition} {threshold})"
                )
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE alert_rules SET last_triggered_at = now() WHERE id = $1",
                        rule["id"])
        else:
            if alert_key in _active_alerts:
                del _active_alerts[alert_key]
                notify(f":white_check_mark: *Capacity Resolved* — {device} {metric} back to normal ({value:.1f})")


# ── Main Loop ────────────────────────────────────────────────────────────────

MONITORED_HOSTS = [
    {"name": "mac-studio", "ip": "127.0.0.1"},
    {"name": "mac-mini", "ip": "192.168.1.190"},
    {"name": "tv-movies-mini", "ip": "192.168.1.7"},
    {"name": "udm-pro", "ip": "192.168.1.1"},
    {"name": "synology-nas", "ip": "192.168.1.11"},
    {"name": "nova-core", "ip": "192.168.1.2"},
    {"name": "nova-core2", "ip": "192.168.1.86"},
    {"name": "nova-core5", "ip": "192.168.1.10"},
]


async def snapshot_loop():
    """Take capacity snapshots every SNAPSHOT_INTERVAL seconds."""
    await asyncio.sleep(10)
    log(f"Snapshot loop started (interval={SNAPSHOT_INTERVAL}s, hosts={len(MONITORED_HOSTS)})")

    while not _shutdown:
        for host in MONITORED_HOSTS:
            try:
                snap = await build_snapshot(host["name"], host["ip"])
                _latest_snapshot[host["name"]] = snap
                await evaluate_alerts(snap)
            except Exception as e:
                log(f"Snapshot failed for {host['name']}: {e}", "ERROR")

        _stats["snapshots_total"] += 1
        _stats["last_run"] = datetime.now(timezone.utc).isoformat()
        await asyncio.sleep(SNAPSHOT_INTERVAL)


async def retention_purge():
    """Purge old snapshots beyond retention."""
    await asyncio.sleep(7200)
    pool = await get_pool()

    while not _shutdown:
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM capacity_snapshots WHERE ts < now() - $1::interval",
                    timedelta(days=RETENTION_DAYS))
                deleted = int(result.split()[-1]) if result else 0
                if deleted > 0:
                    log(f"Purged {deleted} snapshots older than {RETENTION_DAYS} days")
        except Exception as e:
            log(f"Retention purge error: {e}", "ERROR")
        await asyncio.sleep(86400)


# ── HTTP API ─────────────────────────────────────────────────────────────────

async def handle_health(request):
    return web.json_response({
        "ok": True,
        "service": "nova_capacity",
        "version": VERSION,
        "port": HTTP_PORT,
        "uptime_s": int(time.time() - _start_time),
        "hosts_monitored": len(MONITORED_HOSTS),
        "stats": _stats,
        "active_alerts": list(_active_alerts.keys()),
    })


async def handle_capacity(request):
    """GET /capacity — current headroom for all hosts."""
    summaries = []
    for host in MONITORED_HOSTS:
        snap = _latest_snapshot.get(host["name"])
        if snap:
            summaries.append({
                "device": snap["device_name"],
                "ip": snap["device_ip"],
                "status": snap["overall_status"],
                "cpu": {
                    "load_5m": snap["cpu_load_5m"],
                    "cores": snap["cpu_cores"],
                    "headroom_pct": snap["cpu_headroom_pct"],
                },
                "memory": {
                    "total_mb": snap["mem_total_mb"],
                    "used_mb": snap["mem_used_mb"],
                    "free_mb": snap["mem_free_mb"],
                    "headroom_pct": snap["mem_headroom_pct"],
                },
                "disk": snap["disks"],
                "disk_worst_pct": snap["disk_worst_pct"],
            })

    overall = "ok"
    if any(s["status"] == "crit" for s in summaries):
        overall = "crit"
    elif any(s["status"] == "warn" for s in summaries):
        overall = "warn"

    return web.json_response({
        "ok": True,
        "overall_status": overall,
        "hosts": summaries,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


async def handle_history(request):
    """GET /capacity/history?device=<name>&hours=24 — historical snapshots."""
    pool = await get_pool()
    device = request.query.get("device", "")
    hours = min(int(request.query.get("hours", "24")), 720)

    query = """
        SELECT ts, device_name, cpu_headroom_pct, mem_headroom_pct, disk_worst_pct, overall_status
        FROM capacity_snapshots
        WHERE ts > now() - $1::interval
    """
    params = [timedelta(hours=hours)]

    if device:
        query += " AND device_name = $2"
        params.append(device)

    query += " ORDER BY ts DESC LIMIT 500"

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    return web.json_response({
        "ok": True,
        "count": len(rows),
        "snapshots": [
            {
                "ts": r["ts"].isoformat(),
                "device": r["device_name"],
                "cpu_headroom_pct": r["cpu_headroom_pct"],
                "mem_headroom_pct": r["mem_headroom_pct"],
                "disk_worst_pct": r["disk_worst_pct"],
                "status": r["overall_status"],
            }
            for r in rows
        ],
    })


# ── Lifecycle ────────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal received")


async def main():
    global _shutdown
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"Nova Capacity Monitor v{VERSION} starting...")
    log(f"Hosts: {len(MONITORED_HOSTS)}, Interval: {SNAPSHOT_INTERVAL}s")

    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/capacity", handle_capacity)
    app.router.add_get("/capacity/history", handle_history)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, BIND_ADDR, HTTP_PORT)
    await site.start()
    log(f"HTTP API listening on {BIND_ADDR}:{HTTP_PORT}")

    tasks = [
        asyncio.create_task(snapshot_loop()),
        asyncio.create_task(retention_purge()),
    ]

    notify(f":bar_chart: *Capacity Monitor* started (v{VERSION}, {len(MONITORED_HOSTS)} hosts)")

    while not _shutdown:
        await asyncio.sleep(1)

    log("Shutting down...")
    for task in tasks:
        task.cancel()
    await runner.cleanup()
    if _pool:
        await _pool.close()
    log("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
