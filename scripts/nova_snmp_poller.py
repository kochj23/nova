#!/usr/bin/env python3
"""
nova_snmp_poller.py — SNMP metrics collector for Nova's network.

Polls SNMP-enabled devices at regular intervals, stores metrics in PostgreSQL,
fires threshold alerts via Slack, and exposes an HTTP health API.

Complements the syslog server (events) with metrics (state):
  - Syslog = what happened (threats, failures, crashes)
  - SNMP = how things are right now (CPU, bandwidth, disk, temperature)

Services:
  - SNMP poller (asyncio, two intervals: 60s fast, 300s slow)
  - HTTP health/stats API on 0.0.0.0:37463

Written by Jordan Koch.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections import defaultdict, deque
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

# ── Config ────────────────────────────────────────────────────────────────────

VERSION = "1.0.0"
HTTP_PORT = 37463
BIND_ADDR = "0.0.0.0"
DB_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
LOG_FILE = Path.home() / ".openclaw/logs/nova_snmp_poller.log"

FAST_INTERVAL = 60
SLOW_INTERVAL = 300
BATCH_SIZE = 50
BATCH_INTERVAL = 5.0
RETENTION_DAYS = 30

# ── Device Inventory ──────────────────────────────────────────────────────────

DEVICES = [
    {
        "ip": "127.0.0.1",
        "name": "mac-studio",
        "version": "v2c",
        "community_keychain": "nova-snmp-community",
        "port": 161,
        "enabled": True,
    },
    {
        "ip": "192.168.1.1",
        "name": "udm-pro",
        "version": "v2c",
        "community_keychain": "nova-snmp-community",
        "port": 161,
        "enabled": True,
    },
    {
        "ip": "192.168.1.11",
        "name": "synology-nas",
        "version": "v2c",
        "community_keychain": "nova-snmp-community",
        "port": 161,
        "enabled": True,
    },
    {
        "ip": "192.168.1.2",
        "name": "lts01-pi",
        "version": "v2c",
        "community_keychain": "nova-snmp-community",
        "port": 161,
        "enabled": True,
    },
    {
        "ip": "192.168.1.10",
        "name": "nuk",
        "version": "v2c",
        "community_keychain": "nova-snmp-community",
        "port": 161,
        "enabled": True,
    },
    {
        "ip": "192.168.1.190",
        "name": "mac-mini",
        "version": "v2c",
        "community_keychain": "nova-snmp-community",
        "port": 161,
        "enabled": True,
    },
    # ── UniFi Switches ────────────────────────────────────────────────────────
    {"ip": "192.168.1.50",  "name": "sw-patio-16p",      "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.54",  "name": "sw-jordan-8p",      "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.59",  "name": "sw-kitchen-8p",     "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.78",  "name": "sw-rack13-16p",     "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.80",  "name": "sw-livingroom-8p",  "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.102", "name": "sw-garage-desk-8p", "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.122", "name": "sw-rack15-agg-8p",  "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.124", "name": "sw-dining-8p",      "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.155", "name": "sw-jordan-poe-8p",  "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.174", "name": "sw-jordan-16p",     "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.193", "name": "sw-garage-8p-150w", "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    # ── UniFi Access Points ───────────────────────────────────────────────────
    {"ip": "192.168.1.31",  "name": "ap-office-u6e",     "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.106", "name": "ap-kitchen-u6e",    "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.161", "name": "ap-garage-u6e",     "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
]

# Per-device interface indices to monitor (avoids polling hundreds of virtual interfaces)
DEVICE_INTERFACES = {
    "udm-pro": [4, 5],         # WAN1 (Gigabit), WAN2 (SFP+)
    "synology-nas": [7],        # eth4 (LAN NIC)
    "mac-studio": [0],          # primary interface
    "lts01-pi": [0],            # eth0
    "nuk": [0],                 # primary
    "mac-mini": [0],            # primary
    "sw-jordan-16p": [1],       # uplink port
    "sw-rack13-16p": [1],       # uplink port
    "sw-rack15-agg-8p": [1],    # uplink port
    "sw-patio-16p": [1],        # uplink port
    "sw-garage-desk-8p": [1],   # uplink port
    "ap-office-u6e": [1],       # LAN interface
    "ap-kitchen-u6e": [1],      # LAN interface
    "ap-garage-u6e": [1],       # LAN interface
}

# ── OID Definitions ───────────────────────────────────────────────────────────

FAST_OIDS = {
    "cpu_load_1min": {
        "oid": "1.3.6.1.4.1.2021.10.1.3.1",
        "unit": "load",
        "description": "1-minute load average",
    },
    "cpu_load_5min": {
        "oid": "1.3.6.1.4.1.2021.10.1.3.2",
        "unit": "load",
        "description": "5-minute load average",
    },
    "cpu_load_15min": {
        "oid": "1.3.6.1.4.1.2021.10.1.3.3",
        "unit": "load",
        "description": "15-minute load average",
    },
    # Note: UCD-MIB cpu percentage OIDs (.2021.11.9-11) not available on macOS snmpd
    # They work on Linux hosts with snmpd configured. Use load averages instead.
}

SLOW_OIDS = {
    "sys_uptime": {
        "oid": "1.3.6.1.2.1.1.3.0",
        "unit": "ticks",
        "description": "System uptime in hundredths of seconds",
    },
    "mem_total_real": {
        "oid": "1.3.6.1.4.1.2021.4.5.0",
        "unit": "KB",
        "description": "Total real/physical memory",
    },
    "mem_avail_real": {
        "oid": "1.3.6.1.4.1.2021.4.6.0",
        "unit": "KB",
        "description": "Available real/physical memory",
    },
    "mem_total_swap": {
        "oid": "1.3.6.1.4.1.2021.4.3.0",
        "unit": "KB",
        "description": "Total swap space",
    },
    "mem_avail_swap": {
        "oid": "1.3.6.1.4.1.2021.4.4.0",
        "unit": "KB",
        "description": "Available swap space",
    },
    "sys_temp": {
        "oid": "1.3.6.1.4.1.6574.1.2.0",
        "unit": "celsius",
        "description": "Synology system temperature",
    },
}

# Walk these OID trees to get all interfaces/disks
WALK_OIDS = {
    # Interface walks removed — replaced by per-device targeted interface polling below
    "disk_storage_used": {
        "oid": "1.3.6.1.2.1.25.2.3.1.6",
        "unit": "units",
        "poll_group": "slow",
        "description": "Storage used (in allocation units)",
    },
    "disk_storage_size": {
        "oid": "1.3.6.1.2.1.25.2.3.1.5",
        "unit": "units",
        "poll_group": "slow",
        "description": "Storage total size (in allocation units)",
    },
    "disk_storage_descr": {
        "oid": "1.3.6.1.2.1.25.2.3.1.3",
        "unit": "text",
        "poll_group": "slow",
        "description": "Storage description (mount point name)",
    },
}

# ── Thresholds ────────────────────────────────────────────────────────────────

THRESHOLDS = {
    "cpu_load_5min": {"warn": 8.0, "crit": 12.0, "sustained_polls": 5},
    "disk_percent": {"warn": 80.0, "crit": 85.0},
    "if_errors_total": {"crit": 100},
    "unreachable": {"consecutive_failures": 2},
}

# Per-device overrides (WiFi devices need higher failure tolerance)
DEVICE_THRESHOLDS = {
    "mac-mini": {"unreachable": {"consecutive_failures": 5}},
}

# ── State ─────────────────────────────────────────────────────────────────────

_shutdown = False
_pool = None
_metrics_queue = asyncio.Queue() if hasattr(asyncio, 'Queue') else None
_start_time = time.time()
_stats = {
    "polls_total": 0,
    "polls_failed": 0,
    "metrics_stored": 0,
    "alerts_fired": 0,
    "last_fast_poll": None,
    "last_slow_poll": None,
}
_device_failures = defaultdict(int)
_alert_state = {}

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[snmp-poller {ts}] [{level}] {msg}"
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


# ── Credentials ───────────────────────────────────────────────────────────────

_credentials_cache = {}


def get_credential(keychain_service):
    if keychain_service in _credentials_cache:
        return _credentials_cache[keychain_service]
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", "nova", "-s", keychain_service, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            val = r.stdout.strip()
            _credentials_cache[keychain_service] = val
            return val
    except Exception as e:
        log(f"Keychain lookup failed for {keychain_service}: {e}", "WARN")
    return "public"


get_community = get_credential


def build_snmp_cmd(device, cmd, oid_str):
    """Build snmpget/snmpwalk command args for v2c or v3."""
    ip = device["ip"]
    port = device.get("port", 161)
    version = device.get("version", "v2c")

    if version == "v3":
        user = get_credential(device.get("v3_user_keychain", "nova-snmpv3-user"))
        auth_pass = get_credential(device.get("v3_auth_keychain", "nova-snmpv3-auth"))
        priv_pass = get_credential(device.get("v3_priv_keychain", "nova-snmpv3-priv"))
        auth_proto = device.get("v3_auth_proto", "SHA")
        priv_proto = device.get("v3_priv_proto", "AES")
        return [
            cmd, "-v3",
            "-l", "authPriv",
            "-u", user,
            "-a", auth_proto, "-A", auth_pass,
            "-x", priv_proto, "-X", priv_pass,
            "-Oqv", "-t", "3", f"{ip}:{port}", oid_str,
        ]
    else:
        community = get_credential(device.get("community_keychain", "nova-snmp-community"))
        return [
            cmd, "-v2c", "-c", community,
            "-Oqv", "-t", "3", f"{ip}:{port}", oid_str,
        ]


# ── Database ──────────────────────────────────────────────────────────────────

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=3)
    return _pool


async def batch_writer():
    """Consume metrics from queue and batch-insert into PostgreSQL."""
    pool = await get_pool()
    batch = []

    while not _shutdown:
        try:
            try:
                metric = await asyncio.wait_for(_metrics_queue.get(), timeout=BATCH_INTERVAL)
                batch.append(metric)
            except asyncio.TimeoutError:
                pass

            while not _metrics_queue.empty() and len(batch) < BATCH_SIZE:
                batch.append(_metrics_queue.get_nowait())

            if batch:
                async with pool.acquire() as conn:
                    await conn.executemany(
                        """INSERT INTO snmp_metrics
                           (timestamp, device_ip, device_name, metric_name, metric_value, oid, poll_group, unit)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                        [(m["ts"], m["ip"], m["name"], m["metric"], m["value"],
                          m["oid"], m["group"], m["unit"]) for m in batch],
                    )
                _stats["metrics_stored"] += len(batch)
                batch = []

        except Exception as e:
            log(f"Batch writer error: {e}", "ERROR")
            await asyncio.sleep(5)


# ── SNMP Polling ──────────────────────────────────────────────────────────────

def _parse_snmp_value(val):
    """Parse SNMP value string to float, handling various output formats."""
    if not val or val.startswith("No Such"):
        return None
    # Timeticks: (12345) 0:02:03.45
    if "Timeticks:" in val or val.startswith("("):
        m = val.split("(")[-1].split(")")[0]
        try:
            return float(m)
        except ValueError:
            return None
    # Values with unit suffixes: "3339584 kB", "100 Mbps"
    parts = val.split()
    if parts:
        try:
            return float(parts[0])
        except ValueError:
            pass
    # Bare numeric
    try:
        return float(val)
    except ValueError:
        return None


async def snmp_get(device, oid_str):
    """Execute snmpget via subprocess in thread pool."""
    ip = device["ip"]

    def _run():
        try:
            cmd = build_snmp_cmd(device, "/usr/bin/snmpget", oid_str)
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            if r.returncode == 0:
                return _parse_snmp_value(r.stdout.strip())
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass
        return None

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _run)
    except Exception as e:
        log(f"snmpget error {ip} {oid_str}: {e}", "WARN")
        return None


async def snmp_walk(device, oid_str, raw_text=False):
    """Execute snmpwalk via subprocess in thread pool."""
    ip = device["ip"]

    def _run():
        try:
            cmd = build_snmp_cmd(device, "/usr/bin/snmpwalk", oid_str)
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                results = []
                for i, line in enumerate(r.stdout.strip().split("\n")):
                    line = line.strip()
                    if not line or line.startswith("No "):
                        continue
                    if raw_text:
                        results.append((i, line))
                    else:
                        val = _parse_snmp_value(line)
                        if val is not None:
                            results.append((i, val))
                return results
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass
        return []

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _run)
    except Exception:
        return []


async def poll_device(device, oid_group, poll_group):
    """Poll a single device for a group of OIDs + targeted interface metrics."""
    now = datetime.now(timezone.utc)
    ip = device["ip"]
    name = device["name"]
    metrics_collected = 0

    # Poll scalar OIDs (CPU, memory, uptime, temp)
    for metric_name, oid_def in oid_group.items():
        if isinstance(oid_def, str):
            continue
        value = await snmp_get(device, oid_def["oid"])
        if value is not None:
            await _metrics_queue.put({
                "ts": now, "ip": ip, "name": name,
                "metric": metric_name, "value": value,
                "oid": oid_def["oid"], "group": poll_group,
                "unit": oid_def.get("unit", ""),
            })
            metrics_collected += 1

    # Walk disk OIDs (slow poll only)
    if poll_group == "slow":
        for metric_name, oid_def in WALK_OIDS.items():
            is_text = oid_def.get("unit") == "text"
            results = await snmp_walk(device, oid_def["oid"], raw_text=is_text)
            for idx, value in results:
                if is_text:
                    await _metrics_queue.put({
                        "ts": now, "ip": ip, "name": name,
                        "metric": f"{metric_name}.{idx}", "value": 0,
                        "oid": f"{oid_def['oid']}.{idx}", "group": poll_group,
                        "unit": str(value),
                    })
                else:
                    await _metrics_queue.put({
                        "ts": now, "ip": ip, "name": name,
                        "metric": f"{metric_name}.{idx}", "value": value,
                        "oid": f"{oid_def['oid']}.{idx}", "group": poll_group,
                        "unit": oid_def.get("unit", ""),
                    })
                metrics_collected += 1

    # Poll targeted interface metrics (fast poll only)
    if poll_group == "fast":
        iface_indices = DEVICE_INTERFACES.get(name, [0])
        for idx in iface_indices:
            # In octets
            val = await snmp_get(device, f"1.3.6.1.2.1.2.2.1.10.{idx}")
            if val is not None:
                await _metrics_queue.put({
                    "ts": now, "ip": ip, "name": name,
                    "metric": f"if_in_octets.{idx}", "value": val,
                    "oid": f"1.3.6.1.2.1.2.2.1.10.{idx}", "group": "fast",
                    "unit": "bytes",
                })
                metrics_collected += 1
            # Out octets
            val = await snmp_get(device, f"1.3.6.1.2.1.2.2.1.16.{idx}")
            if val is not None:
                await _metrics_queue.put({
                    "ts": now, "ip": ip, "name": name,
                    "metric": f"if_out_octets.{idx}", "value": val,
                    "oid": f"1.3.6.1.2.1.2.2.1.16.{idx}", "group": "fast",
                    "unit": "bytes",
                })
                metrics_collected += 1
            # In errors
            val = await snmp_get(device, f"1.3.6.1.2.1.2.2.1.14.{idx}")
            if val is not None:
                await _metrics_queue.put({
                    "ts": now, "ip": ip, "name": name,
                    "metric": f"if_in_errors.{idx}", "value": val,
                    "oid": f"1.3.6.1.2.1.2.2.1.14.{idx}", "group": "fast",
                    "unit": "count",
                })
                metrics_collected += 1
            # Out errors
            val = await snmp_get(device, f"1.3.6.1.2.1.2.2.1.20.{idx}")
            if val is not None:
                await _metrics_queue.put({
                    "ts": now, "ip": ip, "name": name,
                    "metric": f"if_out_errors.{idx}", "value": val,
                    "oid": f"1.3.6.1.2.1.2.2.1.20.{idx}", "group": "fast",
                    "unit": "count",
                })
                metrics_collected += 1

    return metrics_collected


async def fast_poller():
    """Poll fast metrics (CPU, interfaces) every 60 seconds."""
    await asyncio.sleep(5)
    log(f"Fast poller started (interval={FAST_INTERVAL}s)")

    while not _shutdown:
        for device in DEVICES:
            if not device.get("enabled"):
                continue
            try:
                count = await poll_device(device, FAST_OIDS, "fast")
                if count > 0:
                    _device_failures[device["ip"]] = 0
                else:
                    _device_failures[device["ip"]] += 1
                _stats["polls_total"] += 1
            except Exception as e:
                _device_failures[device["ip"]] += 1
                _stats["polls_failed"] += 1
                log(f"Fast poll failed for {device['name']}: {e}", "WARN")

        _stats["last_fast_poll"] = datetime.now(timezone.utc).isoformat()
        await asyncio.sleep(FAST_INTERVAL)


async def slow_poller():
    """Poll slow metrics (disk, memory, uptime) every 300 seconds."""
    await asyncio.sleep(15)
    log(f"Slow poller started (interval={SLOW_INTERVAL}s)")

    while not _shutdown:
        for device in DEVICES:
            if not device.get("enabled"):
                continue
            try:
                count = await poll_device(device, SLOW_OIDS, "slow")
                _stats["polls_total"] += 1
                if count == 0:
                    _device_failures[device["ip"]] += 1
            except Exception as e:
                _stats["polls_failed"] += 1
                log(f"Slow poll failed for {device['name']}: {e}", "WARN")

        _stats["last_slow_poll"] = datetime.now(timezone.utc).isoformat()
        await asyncio.sleep(SLOW_INTERVAL)


# ── Threshold Checking ────────────────────────────────────────────────────────

async def threshold_checker():
    """Periodically check metrics against thresholds and fire alerts."""
    await asyncio.sleep(30)
    log("Threshold checker started")
    pool = await get_pool()

    while not _shutdown:
        try:
            async with pool.acquire() as conn:
                # Check CPU load (5min avg over last 5 polls)
                rows = await conn.fetch("""
                    SELECT device_ip, device_name, AVG(metric_value) as avg_val
                    FROM snmp_metrics
                    WHERE metric_name = 'cpu_load_5min'
                      AND timestamp > now() - interval '6 minutes'
                    GROUP BY device_ip, device_name
                    HAVING COUNT(*) >= 3
                """)
                for row in rows:
                    key = f"{row['device_ip']}:cpu_load"
                    avg = row["avg_val"]
                    if avg >= THRESHOLDS["cpu_load_5min"]["crit"]:
                        if key not in _alert_state:
                            _alert_state[key] = time.time()
                            _stats["alerts_fired"] += 1
                            notify(
                                f":fire: *SNMP Alert* — {row['device_name']} "
                                f"CPU load critical: {avg:.1f} (threshold: "
                                f"{THRESHOLDS['cpu_load_5min']['crit']})"
                            )
                    elif key in _alert_state:
                        del _alert_state[key]

                # Check device unreachability
                for device in DEVICES:
                    ip = device["ip"]
                    failures = _device_failures.get(ip, 0)
                    key = f"{ip}:unreachable"
                    dev_thresh = DEVICE_THRESHOLDS.get(device["name"], {}).get(
                        "unreachable", THRESHOLDS["unreachable"]
                    )
                    if failures >= dev_thresh["consecutive_failures"]:
                        if key not in _alert_state:
                            _alert_state[key] = time.time()
                            _stats["alerts_fired"] += 1
                            notify(
                                f":warning: *SNMP Alert* — {device['name']} ({ip}) "
                                f"unreachable ({failures} consecutive failures)"
                            )
                    elif key in _alert_state:
                        del _alert_state[key]
                        notify(f":white_check_mark: *SNMP Resolved* — {device['name']} ({ip}) reachable again")

        except Exception as e:
            log(f"Threshold checker error: {e}", "ERROR")

        await asyncio.sleep(60)


# ── Retention Purge ───────────────────────────────────────────────────────────

async def retention_purge():
    """Purge old metrics data per retention policy."""
    await asyncio.sleep(3600)
    pool = await get_pool()

    while not _shutdown:
        try:
            async with pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM snmp_metrics WHERE timestamp < now() - $1::interval",
                    timedelta(days=RETENTION_DAYS),
                )
                deleted = int(result.split()[-1]) if result else 0
                if deleted > 0:
                    log(f"Purged {deleted} metrics older than {RETENTION_DAYS} days")
        except Exception as e:
            log(f"Retention purge error: {e}", "ERROR")

        await asyncio.sleep(3600)


# ── HTTP Health API ───────────────────────────────────────────────────────────

async def handle_health(request):
    """GET /health — return poller status and stats."""
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM snmp_metrics")
            latest = await conn.fetchval(
                "SELECT MAX(timestamp) FROM snmp_metrics"
            )
    except Exception:
        count = 0
        latest = None

    return web.json_response({
        "ok": True,
        "service": "nova_snmp_poller",
        "version": VERSION,
        "port": HTTP_PORT,
        "uptime_s": int(time.time() - _start_time),
        "devices": len([d for d in DEVICES if d.get("enabled")]),
        "stats": _stats,
        "total_metrics": count,
        "latest_metric": latest.isoformat() if latest else None,
        "active_alerts": list(_alert_state.keys()),
        "device_failures": dict(_device_failures),
    })


async def handle_metrics(request):
    """GET /metrics?device=<ip>&name=<metric>&limit=100 — query recent metrics."""
    pool = await get_pool()
    device = request.query.get("device", "")
    name = request.query.get("name", "")
    limit = min(int(request.query.get("limit", "100")), 1000)

    query = "SELECT * FROM snmp_metrics WHERE 1=1"
    params = []
    idx = 1

    if device:
        query += f" AND device_ip = ${idx}::inet"
        params.append(device)
        idx += 1
    if name:
        query += f" AND metric_name LIKE ${idx}"
        params.append(f"{name}%")
        idx += 1

    query += f" ORDER BY timestamp DESC LIMIT ${idx}"
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    return web.json_response({
        "ok": True,
        "count": len(rows),
        "metrics": [
            {
                "timestamp": r["timestamp"].isoformat(),
                "device_ip": str(r["device_ip"]),
                "device_name": r["device_name"],
                "metric_name": r["metric_name"],
                "value": r["metric_value"],
                "unit": r["unit"],
            }
            for r in rows
        ],
    })


async def handle_devices(request):
    """GET /devices — return configured device inventory."""
    return web.json_response({
        "ok": True,
        "devices": [
            {
                "ip": d["ip"],
                "name": d["name"],
                "version": d["version"],
                "enabled": d.get("enabled", True),
                "failures": _device_failures.get(d["ip"], 0),
            }
            for d in DEVICES
        ],
    })


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal received")


async def main():
    global _shutdown, _metrics_queue
    _metrics_queue = asyncio.Queue(maxsize=10000)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"Nova SNMP Poller v{VERSION} starting...")
    log(f"Devices: {len([d for d in DEVICES if d.get('enabled')])} enabled")
    log(f"Fast interval: {FAST_INTERVAL}s, Slow interval: {SLOW_INTERVAL}s")

    # Start HTTP API
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/metrics", handle_metrics)
    app.router.add_get("/devices", handle_devices)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, BIND_ADDR, HTTP_PORT)
    await site.start()
    log(f"HTTP API listening on {BIND_ADDR}:{HTTP_PORT}")

    # Start background tasks
    tasks = [
        asyncio.create_task(batch_writer()),
        asyncio.create_task(fast_poller()),
        asyncio.create_task(slow_poller()),
        asyncio.create_task(threshold_checker()),
        asyncio.create_task(retention_purge()),
    ]

    notify(f":chart_with_upwards_trend: *SNMP Poller* started (v{VERSION}, {len(DEVICES)} devices)")

    # Wait for shutdown
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
