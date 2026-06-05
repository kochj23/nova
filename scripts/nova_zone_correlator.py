#!/usr/bin/env python3
"""
nova_zone_correlator.py — Multi-source event correlation (Frigate multi-camera zone pattern).

Cross-correlates events from multiple data sources within logical zones:
  - security: syslog threats + SNMP anomalies + network sentinel
  - health: device CPU/memory/disk across SNMP + service checks
  - infrastructure: PostgreSQL + Redis + Ollama + Memory Server as unit

Correlation rules detect compound events that single-source monitoring misses:
  - Syslog threat from X AND SNMP CPU spike on X → coordinated attack
  - Multiple services down AND shared dependency (PG/Redis) down → root cause
  - SNMP disk >90% AND syslog write errors → impending failure

Runs every 60s, publishes to Redis stream nova:correlated:events.

Written by Jordan Koch.
"""

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import psycopg2
    import redis
except ImportError as e:
    print(f"FATAL: {e}", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

OPS_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
REDIS_URL = "redis://192.168.1.6:6379"
CORRELATED_STREAM = "nova:correlated:events"
LOG_FILE = Path.home() / ".openclaw/logs/nova_zone_correlator.log"
CORRELATION_WINDOW_S = 300  # 5 minute correlation window


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[correlator {ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _db_query(sql, params=None):
    try:
        conn = psycopg2.connect(OPS_DSN)
        cur = conn.cursor()
        cur.execute(sql, params or ())
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        else:
            rows = []
        conn.close()
        return rows
    except Exception as e:
        log(f"DB error: {e}", "ERROR")
        return []


def _get_redis():
    return redis.from_url(REDIS_URL, decode_responses=True)


# ── Correlation Rules ─────────────────────────────────────────────────────────

def correlate_security_zone() -> list:
    """Cross-correlate syslog threats with SNMP anomalies on same device."""
    # Get recent syslog threats grouped by source IP
    threats = _db_query("""
        SELECT source_ip, threat_type, COUNT(*) as count
        FROM syslog_events
        WHERE threat_type IS NOT NULL
          AND received_at > now() - interval '5 minutes'
        GROUP BY source_ip, threat_type
    """)

    # Get SNMP CPU spikes in same window
    cpu_spikes = _db_query("""
        SELECT device_ip, AVG(metric_value) as avg_cpu
        FROM snmp_metrics
        WHERE metric_name = 'cpu_load_5min'
          AND timestamp > now() - interval '5 minutes'
        GROUP BY device_ip
        HAVING AVG(metric_value) > 8.0
    """)

    correlations = []
    spike_ips = {str(r["device_ip"]) for r in cpu_spikes}

    for threat in threats:
        src_ip = str(threat["source_ip"]) if threat["source_ip"] else ""
        if src_ip in spike_ips:
            correlations.append({
                "zone": "security",
                "type": "threat_with_cpu_spike",
                "severity": "critical",
                "device_ip": src_ip,
                "details": f"{threat['count']}x {threat['threat_type']} from {src_ip} + CPU spike on same device",
                "sources": ["syslog", "snmp"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    return correlations


def correlate_health_zone() -> list:
    """Cross-correlate device health issues across SNMP metrics."""
    # Devices with high disk usage AND high CPU
    correlations = []

    # Check for disk + CPU compound issue
    high_cpu = _db_query("""
        SELECT device_name, device_ip, AVG(metric_value) as avg_load
        FROM snmp_metrics
        WHERE metric_name = 'cpu_load_5min'
          AND timestamp > now() - interval '10 minutes'
        GROUP BY device_name, device_ip
        HAVING AVG(metric_value) > 6.0
    """)

    high_mem = _db_query("""
        SELECT device_name, device_ip
        FROM snmp_metrics
        WHERE metric_name = 'mem_avail_real'
          AND metric_value < 100000
          AND timestamp > now() - interval '10 minutes'
        GROUP BY device_name, device_ip
    """)

    high_mem_devices = {str(r["device_ip"]) for r in high_mem}

    for cpu_row in high_cpu:
        if str(cpu_row["device_ip"]) in high_mem_devices:
            correlations.append({
                "zone": "health",
                "type": "resource_exhaustion",
                "severity": "warning",
                "device": cpu_row["device_name"],
                "device_ip": str(cpu_row["device_ip"]),
                "details": f"{cpu_row['device_name']}: high CPU ({cpu_row['avg_load']:.1f}) + low memory",
                "sources": ["snmp_cpu", "snmp_memory"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    return correlations


def correlate_infrastructure_zone() -> list:
    """Detect infrastructure cascade failures (shared dependency down)."""
    correlations = []

    # Check if PostgreSQL or Redis are down while dependent services report issues
    try:
        import socket
        pg_up = False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            pg_up = s.connect_ex(("127.0.0.1", 5432)) == 0
            s.close()
        except Exception:
            pass

        redis_up = False
        try:
            rc = _get_redis()
            rc.ping()
            redis_up = True
        except Exception:
            pass

        if not pg_up:
            correlations.append({
                "zone": "infrastructure",
                "type": "cascade_root_cause",
                "severity": "critical",
                "device": "localhost",
                "details": "PostgreSQL DOWN — all dependent services (Memory Server, Scheduler, Gateway) will fail",
                "sources": ["port_check"],
                "root_cause": "postgresql",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        if not redis_up:
            correlations.append({
                "zone": "infrastructure",
                "type": "cascade_root_cause",
                "severity": "critical",
                "device": "localhost",
                "details": "Redis DOWN — inference queue, memory cache, agent heartbeats affected",
                "sources": ["port_check"],
                "root_cause": "redis",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
    except Exception:
        pass

    return correlations


# ── Main Correlation Run ──────────────────────────────────────────────────────

def run_correlation() -> list:
    """Run all correlation rules and publish findings."""
    all_correlations = []
    all_correlations.extend(correlate_security_zone())
    all_correlations.extend(correlate_health_zone())
    all_correlations.extend(correlate_infrastructure_zone())

    if all_correlations:
        rc = _get_redis()
        for corr in all_correlations:
            rc.xadd(CORRELATED_STREAM, {"data": json.dumps(corr, default=str)}, maxlen=1000)

        # Alert on critical correlations
        critical = [c for c in all_correlations if c["severity"] == "critical"]
        if critical:
            msg = ":link: *Cross-Source Correlation Alert*\n"
            for c in critical:
                msg += f"  • [{c['zone']}] {c['details']}\n"
            nova_config.post_both(msg, slack_channel=nova_config.SLACK_BB)

        log(f"Found {len(all_correlations)} correlations ({len(critical)} critical)")

    return all_correlations


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    results = run_correlation()
    if results:
        print(json.dumps(results, indent=2, default=str))
    else:
        print("No correlations detected")
