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
import re
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

VERSION = "1.1.0"
HTTP_PORT = 37463
BIND_ADDR = "0.0.0.0"
DB_DSN = "postgresql://kochj@pg-primary.digitalnoise.net:5432/nova_ops"
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
        "name": "nova-core",
        "version": "v2c",
        "community_keychain": "nova-snmp-community",
        "port": 161,
        "enabled": True,
    },
    {
        "ip": "192.168.1.10",
        "name": "nova-core5",
        "version": "v2c",
        "community_keychain": "nova-snmp-community",
        "port": 161,
        "enabled": True,
    },
    {
        "ip": "192.168.1.86",
        "name": "nova-core2",
        "version": "v2c",
        "community_keychain": "nova-snmp-community",
        "port": 161,
        "enabled": True,
    },
    {
        "ip": "192.168.1.251",  # static Ethernet — .190 is its WiFi (private MAC, drops inbound while idle)
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
    {"ip": "192.168.1.80",  "name": "sw-livingroom-8p",  "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.102", "name": "sw-garage-desk-8p", "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.124", "name": "sw-dining-8p",      "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    # 2026-07-20: sw-rack13-16p (.78) and sw-rack15-agg-8p (.122) physically
    # removed, replaced by this one 48-port aggregation switch (confirmed via
    # live UniFi controller: 192.168.1.24, USW Pro 48 PoE, adopted+online).
    {"ip": "192.168.1.24",  "name": "sw-rack-agg-48p",   "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.155", "name": "sw-jordan-poe-8p",  "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.174", "name": "sw-jordan-16p",     "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.193", "name": "sw-garage-8p-150w", "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    # ── UniFi Access Points ───────────────────────────────────────────────────
    {"ip": "192.168.1.31",  "name": "ap-office-u6e",     "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.106", "name": "ap-kitchen-u6e",    "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    {"ip": "192.168.1.161", "name": "ap-garage-u6e",     "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
    # ── NAS ───────────────────────────────────────────────────────────────────
    # UNAS Pro 8 — SNMP enabled 2026-06-20. Exposes system + IF-MIB (per-interface
    # traffic/errors) only; no hrStorage/CPU via SNMP (those come from the UniFi Drive API).
    {"ip": "192.168.1.69",  "name": "unas-pro",          "version": "v2c", "community_keychain": "nova-snmp-community", "port": 161, "enabled": True},
]

# Per-device interface indices to monitor (avoids polling hundreds of virtual interfaces)
DEVICE_INTERFACES = {
    "udm-pro": [4, 5],         # WAN1 (Gigabit), WAN2 (SFP+)
    "synology-nas": [7],        # eth4 (LAN NIC)
    "mac-studio": [0],          # primary interface
    "nova-core": [0],            # eth0
    "nova-core2": [0],           # eth0
    "nova-core5": [0],                 # primary
    "mac-mini": [0],            # primary
    "sw-jordan-16p": [1],       # uplink port
    "sw-rack13-16p": [1],       # uplink port
    "sw-rack15-agg-8p": [1],    # uplink port
    "sw-patio-16p": [1],        # uplink port
    "sw-garage-desk-8p": [1],   # uplink port
    "sw-jordan-8p": [1],        # uplink port
    "sw-kitchen-8p": [1],       # uplink port
    "sw-livingroom-8p": [1],    # uplink port
    "sw-dining-8p": [1],        # uplink port
    "sw-jordan-poe-8p": [1],    # uplink port
    "sw-garage-8p-150w": [1],   # uplink port
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
    # UCD-MIB ssCpu percentages (.2021.11.9-11). Present on Linux/UniFi/Synology snmpd
    # (NOT macOS — gracefully returns None there). Gives real CPU% utilization.
    "cpu_user_pct": {
        "oid": "1.3.6.1.4.1.2021.11.9.0",
        "unit": "percent",
        "description": "CPU user time %",
    },
    "cpu_system_pct": {
        "oid": "1.3.6.1.4.1.2021.11.10.0",
        "unit": "percent",
        "description": "CPU system time %",
    },
    "cpu_idle_pct": {
        "oid": "1.3.6.1.4.1.2021.11.11.0",
        "unit": "percent",
        "description": "CPU idle time % (cpu_used_pct = 100 - this)",
    },
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
    # Buffers + Cached are RECLAIMABLE. memAvailReal (above) is only MemFree, so a healthy
    # cache-heavy Linux box reads ~1% "free" and the mem_headroom alert cries wolf all night.
    # True MemAvailable ≈ memAvailReal + memBuffer + memCached — collect these so the headroom
    # metric reflects memory the kernel can actually hand out. (added 2026-08-10)
    "mem_buffer": {
        "oid": "1.3.6.1.4.1.2021.4.14.0",
        "unit": "KB",
        "description": "Memory used for buffers (reclaimable)",
    },
    "mem_cached": {
        "oid": "1.3.6.1.4.1.2021.4.15.0",
        "unit": "KB",
        "description": "Memory used for cache (reclaimable)",
    },
    "sys_temp": {
        "oid": "1.3.6.1.4.1.6574.1.2.0",
        "unit": "celsius",
        "description": "Synology system temperature",
    },
    # ── Synology vendor scalars (SYNOLOGY-SYSTEM-MIB .6574.1) ──────────────────
    "syno_system_status": {
        "oid": "1.3.6.1.4.1.6574.1.1.0",
        "unit": "status",
        "description": "Synology system status (1=Normal, 2=Failed)",
    },
    "syno_power_status": {
        "oid": "1.3.6.1.4.1.6574.1.3.0",
        "unit": "status",
        "description": "Synology power status (1=Normal, 2=Failed)",
    },
    "syno_fan_system_status": {
        "oid": "1.3.6.1.4.1.6574.1.4.1.0",
        "unit": "status",
        "description": "Synology system fan status (1=Normal, 2=Failed)",
    },
    "syno_fan_cpu_status": {
        "oid": "1.3.6.1.4.1.6574.1.4.2.0",
        "unit": "status",
        "description": "Synology CPU fan status (1=Normal, 2=Failed)",
    },
    "syno_upgrade_available": {
        "oid": "1.3.6.1.4.1.6574.1.5.4.0",
        "unit": "status",
        "description": "Synology DSM upgrade availability (1=available, 2=unavailable)",
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

# ── Per-interface (ifTable/ifXTable) collection ─────────────────────────────────
#
# The big observability win: per-PORT throughput + errors on every switch/AP/router.
# We walk ifXTable 64-bit counters (no 32-bit wrap), compute bps from sample deltas,
# and capture errors/discards/operstatus/speed. ifName is stored as a label metric.
#
# Devices in IFACE_FULL_WALK get every physical/active interface (switches = all ports).
# Everything else keeps lightweight scalar host metrics only (avoids hammering Macs
# with 25 virtual interfaces). Loopback/virtual ifaces are filtered by name/operstatus.

IFACE_FULL_WALK = {
    # UniFi switches — per-port traffic is the headline metric
    "sw-patio-16p", "sw-jordan-8p", "sw-kitchen-8p",
    "sw-livingroom-8p", "sw-garage-desk-8p", "sw-rack-agg-48p", "sw-dining-8p",
    "sw-jordan-poe-8p", "sw-jordan-16p", "sw-garage-8p-150w",
    # UniFi APs — wired uplink + radio interfaces
    "ap-office-u6e", "ap-kitchen-u6e", "ap-garage-u6e",
    # Router — WAN + LAN segments
    "udm-pro",
    # NAS — LAN NICs
    "synology-nas",
}

# ifXTable / ifTable columns. (mib_oid, metric_prefix, unit, is_counter)
# is_counter=True columns get an additional computed *_bps rate metric.
IFACE_COLUMNS = [
    ("1.3.6.1.2.1.31.1.1.1.6",  "if_hc_in_octets",  "bytes",  True),   # ifHCInOctets (64-bit)
    ("1.3.6.1.2.1.31.1.1.1.10", "if_hc_out_octets", "bytes",  True),   # ifHCOutOctets (64-bit)
    ("1.3.6.1.2.1.2.2.1.14",    "if_in_errors",     "count",  False),  # ifInErrors
    ("1.3.6.1.2.1.2.2.1.20",    "if_out_errors",    "count",  False),  # ifOutErrors
    ("1.3.6.1.2.1.2.2.1.13",    "if_in_discards",   "count",  False),  # ifInDiscards
    ("1.3.6.1.2.1.2.2.1.19",    "if_out_discards",  "count",  False),  # ifOutDiscards
    ("1.3.6.1.2.1.2.2.1.8",     "if_oper_status",   "status", False),  # ifOperStatus (1=up,2=down)
    ("1.3.6.1.2.1.31.1.1.1.15", "if_speed_mbps",    "mbps",   False),  # ifHighSpeed (Mbps)
]

IFNAME_OID = "1.3.6.1.2.1.31.1.1.1.1"   # ifName
IFOPER_OID = "1.3.6.1.2.1.2.2.1.8"       # ifOperStatus (used to filter active ifaces)

# Skip these interface name patterns (loopback / tunnels / virtual) on full walks
IFACE_SKIP_PREFIXES = ("lo", "dummy", "gre", "erspan", "ip_vti", "ip6", "sit",
                       "ifb", "honeypot", "tap", "tun", "br", "switch0.", "veth")

# ── Synology vendor walk tables (SYNOLOGY-DISK-MIB / SYNOLOGY-RAID-MIB) ──────────
# Per-disk temp/status, per-volume/RAID status + size, per-disk IO. Synology only.
SYNO_DISK_COLUMNS = [
    ("1.3.6.1.4.1.6574.2.1.1.2",  "syno_disk_name",   "text",    False),  # diskID label
    ("1.3.6.1.4.1.6574.2.1.1.5",  "syno_disk_status", "status",  True),   # 1=Normal..5=Crashed
    ("1.3.6.1.4.1.6574.2.1.1.6",  "syno_disk_temp",   "celsius", True),   # disk temperature
]
SYNO_RAID_COLUMNS = [
    ("1.3.6.1.4.1.6574.3.1.1.2", "syno_raid_name",       "text",   False),  # volume/pool name
    ("1.3.6.1.4.1.6574.3.1.1.3", "syno_raid_status",     "status", True),   # 1=Normal..others=degraded
    ("1.3.6.1.4.1.6574.3.1.1.4", "syno_raid_free_bytes", "bytes",  True),   # raidFreeSize
    ("1.3.6.1.4.1.6574.3.1.1.5", "syno_raid_total_bytes","bytes",  True),   # raidTotalSize
]

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
# Previous interface counter samples for bps delta computation.
# Keyed by (device_name, ifindex, counter_name) -> (timestamp_epoch, counter_value)
_iface_prev = {}

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
    # Bare -Oqv timeticks format (no "Timeticks:"/parens at all): "D:HH:MM:SS.ss"
    # e.g. "2:18:27:25.00". Convert back to raw ticks (hundredths of a second)
    # so it's the same unit as the "Timeticks: (raw)" branch above.
    m = re.match(r'^(\d+):(\d{1,2}):(\d{1,2}):(\d{1,2}(?:\.\d+)?)$', val)
    if m:
        d, h, mi, s = m.groups()
        total_seconds = int(d) * 86400 + int(h) * 3600 + int(mi) * 60 + float(s)
        return total_seconds * 100
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


async def snmp_walk_index(device, oid_str):
    """Walk an OID subtree, returning {trailing_index: raw_value_string}.

    Uses -Oqn (numeric OID + bare value) so we can recover the table row index
    (the OID suffix after oid_str) for joining columns of the same table.
    """
    ip = device["ip"]
    base = "." + oid_str if not oid_str.startswith(".") else oid_str

    def _run():
        try:
            community = get_credential(device.get("community_keychain", "nova-snmp-community"))
            version = device.get("version", "v2c")
            port = device.get("port", 161)
            if version == "v3":
                cmd = build_snmp_cmd(device, "/usr/bin/snmpbulkwalk", oid_str)
            else:
                cmd = ["/usr/bin/snmpbulkwalk", "-v2c", "-c", community,
                       "-Oqn", "-t", "3", f"{ip}:{port}", oid_str]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if r.returncode != 0:
                return {}
            out = {}
            for line in r.stdout.strip().split("\n"):
                line = line.strip()
                if not line or line.startswith("No "):
                    continue
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
                oid, val = parts
                if not oid.startswith(base + "."):
                    continue
                idx = oid[len(base) + 1:]
                out[idx] = val.strip().strip('"')
            return out
        except Exception:
            return {}

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _run)
    except Exception:
        return {}


async def collect_interfaces(device, now):
    """Walk ifTable/ifXTable for all active interfaces; emit traffic/errors/bps.

    Computes bps from the delta against the previous sample (per device+iface+counter)
    so Grafana can chart throughput directly without needing rate() on a SQL source.
    Also emits the raw 64-bit counter so rate() in Grafana stays possible.
    """
    name = device["name"]
    ip = device["ip"]
    collected = 0

    # ifName labels + operstatus to decide which interfaces are worth storing
    ifnames = await snmp_walk_index(device, IFNAME_OID)
    if not ifnames:
        return 0
    opers = await snmp_walk_index(device, IFOPER_OID)

    def _wanted(idx):
        nm = ifnames.get(idx, "")
        low = nm.lower()
        if any(low.startswith(p) for p in IFACE_SKIP_PREFIXES):
            return False
        # keep up interfaces, plus switch ports (numeric "0/N") even if currently down
        oper = opers.get(idx, "")
        is_up = "1" in oper.split("(")[0] if oper else False
        is_switchport = "/" in nm
        return is_up or is_switchport

    wanted = [i for i in ifnames if _wanted(i)]
    if not wanted:
        return 0

    # Emit ifName as a label metric (value=index) so dashboards can join name->index
    for idx in wanted:
        await _metrics_queue.put({
            "ts": now, "ip": ip, "name": name,
            "metric": f"if_name.{idx}", "value": float(idx) if idx.isdigit() else 0.0,
            "oid": f"{IFNAME_OID}.{idx}", "group": "fast",
            "unit": ifnames[idx],   # unit field carries the human-readable port name
        })
        collected += 1

    # ifOperStatus comes back as a textual enum under -Oqn (e.g. "up"/"down")
    _oper_map = {"up": 1.0, "down": 2.0, "testing": 3.0, "unknown": 4.0,
                 "dormant": 5.0, "notpresent": 6.0, "lowerlayerdown": 7.0}

    now_epoch = time.time()
    for col_oid, prefix, unit, is_counter in IFACE_COLUMNS:
        col = await snmp_walk_index(device, col_oid)
        for idx in wanted:
            raw = col.get(idx)
            if raw is None:
                continue
            if prefix == "if_oper_status":
                val = _oper_map.get(raw.split("(")[0].strip().lower(),
                                    _parse_snmp_value(raw))
            else:
                val = _parse_snmp_value(raw)
            if val is None:
                continue
            await _metrics_queue.put({
                "ts": now, "ip": ip, "name": name,
                "metric": f"{prefix}.{idx}", "value": val,
                "oid": f"{col_oid}.{idx}", "group": "fast",
                "unit": unit,
            })
            collected += 1

            # Compute bits-per-second from counter delta
            if is_counter:
                key = (name, idx, prefix)
                prev = _iface_prev.get(key)
                _iface_prev[key] = (now_epoch, val)
                if prev:
                    dt = now_epoch - prev[0]
                    dv = val - prev[1]
                    if dt > 0 and dv >= 0:   # dv<0 => counter reset/reboot, skip
                        bps = (dv * 8.0) / dt
                        bps_metric = prefix.replace("hc_", "").replace("_octets", "_bps")
                        await _metrics_queue.put({
                            "ts": now, "ip": ip, "name": name,
                            "metric": f"{bps_metric}.{idx}", "value": bps,
                            "oid": f"{col_oid}.{idx}", "group": "fast",
                            "unit": "bps",
                        })
                        collected += 1
    return collected


async def collect_synology(device, now):
    """Walk Synology disk + RAID/volume vendor tables. Emits temp/status/size."""
    name = device["name"]
    ip = device["ip"]
    collected = 0

    async def _emit_table(columns):
        nonlocal collected
        # First column is the name/label table; fetch it to label rows
        name_oid = columns[0][0]
        labels = await snmp_walk_index(device, name_oid)
        for col_oid, prefix, unit, _store in columns:
            if not _store:
                continue
            col = await snmp_walk_index(device, col_oid)
            for idx, raw in col.items():
                val = _parse_snmp_value(raw)
                if val is None:
                    continue
                label = labels.get(idx, idx)
                await _metrics_queue.put({
                    "ts": now, "ip": ip, "name": name,
                    "metric": f"{prefix}.{idx}", "value": val,
                    "oid": f"{col_oid}.{idx}", "group": "slow",
                    "unit": f"{unit}|{label}",   # unit carries "unit|RowLabel" for dashboards
                })
                collected += 1

    await _emit_table(SYNO_DISK_COLUMNS)
    await _emit_table(SYNO_RAID_COLUMNS)
    return collected


async def poll_device(device, oid_group, poll_group):
    """Poll a single device for a group of OIDs + targeted interface metrics."""
    now = datetime.now(timezone.utc)
    ip = device["ip"]
    name = device["name"]
    metrics_collected = 0

    # Poll scalar OIDs (CPU, memory, uptime, temp)
    scalar_vals = {}
    for metric_name, oid_def in oid_group.items():
        if isinstance(oid_def, str):
            continue
        value = await snmp_get(device, oid_def["oid"])
        if value is not None:
            scalar_vals[metric_name] = value
            await _metrics_queue.put({
                "ts": now, "ip": ip, "name": name,
                "metric": metric_name, "value": value,
                "oid": oid_def["oid"], "group": poll_group,
                "unit": oid_def.get("unit", ""),
            })
            metrics_collected += 1

    # Derive total CPU utilization % from UCD ssCpuIdle (cleaner single metric for dashboards)
    if "cpu_idle_pct" in scalar_vals:
        used = max(0.0, min(100.0, 100.0 - scalar_vals["cpu_idle_pct"]))
        await _metrics_queue.put({
            "ts": now, "ip": ip, "name": name,
            "metric": "cpu_used_pct", "value": used,
            "oid": "1.3.6.1.4.1.2021.11.11.0", "group": poll_group,
            "unit": "percent",
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

    # Synology vendor tables (slow poll only)
    if poll_group == "slow" and name == "synology-nas":
        try:
            metrics_collected += await collect_synology(device, now)
        except Exception as e:
            log(f"Synology collect failed for {name}: {e}", "WARN")

    # hrProcessorLoad — per-CPU utilization % (HOST-RESOURCES-MIB). Slow poll.
    # Available on Linux/Synology/macOS/UniFi-AP snmpd. We also emit the average.
    if poll_group == "slow":
        try:
            cpus = await snmp_walk_index(device, "1.3.6.1.2.1.25.3.3.1.2")
            loads = []
            for idx, raw in cpus.items():
                val = _parse_snmp_value(raw)
                if val is None:
                    continue
                loads.append(val)
                await _metrics_queue.put({
                    "ts": now, "ip": ip, "name": name,
                    "metric": f"hr_cpu_load.{idx}", "value": val,
                    "oid": f"1.3.6.1.2.1.25.3.3.1.2.{idx}", "group": "slow",
                    "unit": "percent",
                })
                metrics_collected += 1
            if loads:
                await _metrics_queue.put({
                    "ts": now, "ip": ip, "name": name,
                    "metric": "hr_cpu_load_avg", "value": sum(loads) / len(loads),
                    "oid": "1.3.6.1.2.1.25.3.3.1.2", "group": "slow",
                    "unit": "percent",
                })
                metrics_collected += 1
        except Exception as e:
            log(f"hrProcessorLoad walk failed for {name}: {e}", "WARN")

    # Full per-interface walk for switches/APs/router/NAS (fast poll only)
    if poll_group == "fast" and name in IFACE_FULL_WALK:
        try:
            metrics_collected += await collect_interfaces(device, now)
        except Exception as e:
            log(f"Interface walk failed for {name}: {e}", "WARN")

    # Legacy targeted single-interface metrics (fast poll only) — kept for hosts
    # that aren't in IFACE_FULL_WALK (Macs/Linux endpoints) so existing
    # if_in_octets.N / if_out_octets.N series stay continuous.
    if poll_group == "fast" and name not in IFACE_FULL_WALK:
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
            # Approximate count from catalog stats, not a live COUNT(*) — this
            # table is 60M+ rows, an exact count needs a full scan every call.
            # Found 2026-07-21: this endpoint's COUNT(*)+MAX(timestamp) alone
            # accounted for 31% of ALL database time fleet-wide, called on
            # every health check. reltuples is updated by autovacuum/ANALYZE;
            # close enough for a status display, not used for anything exact.
            count = await conn.fetchval(
                "SELECT reltuples::bigint FROM pg_class WHERE oid = 'snmp_metrics'::regclass"
            )
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
