#!/usr/bin/env python3
"""
nova_snmp_daily_digest.py — Daily SNMP metrics summary ingested into Nova's vector memory.

Summarizes yesterday's SNMP metrics: device health, CPU/disk trends,
bandwidth usage, any threshold alerts. Ingests as a memory under
source="infrastructure" for Nova's infrastructure awareness.

Runs daily at 5:15am via scheduler (staggered after syslog digest at 5:00am).

Written by Jordan Koch.
"""

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import psycopg2
except ImportError:
    print("FATAL: psycopg2 not installed", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

OPS_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"
MEMORY_URL = "http://192.168.1.6:18790/remember"
LOG_FILE = Path.home() / ".openclaw/logs/snmp_daily_digest.log"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[snmp_digest {ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def remember(text, metadata):
    payload = json.dumps({
        "text": nova_config.truncate_at_boundary(text),
        "source": "infrastructure",
        "tier": "long_term",
        "metadata": metadata,
    }).encode()
    req = urllib.request.Request(
        MEMORY_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception as e:
        log(f"  Ingest error: {e}")
        return False


def run():
    log("Starting SNMP daily digest...")
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    conn = psycopg2.connect(OPS_DSN)
    cur = conn.cursor()

    # Total metrics collected
    cur.execute(
        "SELECT COUNT(*) FROM snmp_metrics WHERE timestamp >= %s AND timestamp < %s",
        (start, end))
    total_metrics = cur.fetchone()[0]

    if total_metrics == 0:
        log("No SNMP metrics yesterday — skipping digest")
        conn.close()
        return

    # Devices reporting
    cur.execute(
        "SELECT DISTINCT device_name, device_ip FROM snmp_metrics "
        "WHERE timestamp >= %s AND timestamp < %s",
        (start, end))
    devices = cur.fetchall()

    # CPU load averages per device (peak and avg)
    cur.execute("""
        SELECT device_name,
               ROUND(AVG(metric_value)::numeric, 2) as avg_load,
               ROUND(MAX(metric_value)::numeric, 2) as peak_load
        FROM snmp_metrics
        WHERE metric_name = 'cpu_load_5min'
          AND timestamp >= %s AND timestamp < %s
        GROUP BY device_name
    """, (start, end))
    cpu_stats = cur.fetchall()

    # Memory usage per device
    cur.execute("""
        SELECT device_name,
               MAX(CASE WHEN metric_name = 'mem_total_real' THEN metric_value END) as total_kb,
               MIN(CASE WHEN metric_name = 'mem_avail_real' THEN metric_value END) as min_avail_kb
        FROM snmp_metrics
        WHERE metric_name IN ('mem_total_real', 'mem_avail_real')
          AND timestamp >= %s AND timestamp < %s
        GROUP BY device_name
    """, (start, end))
    mem_stats = cur.fetchall()

    # Interface traffic (total bytes transferred)
    cur.execute("""
        SELECT device_name,
               SUM(CASE WHEN metric_name LIKE 'if_in_octets%%' THEN metric_value ELSE 0 END) as total_in,
               SUM(CASE WHEN metric_name LIKE 'if_out_octets%%' THEN metric_value ELSE 0 END) as total_out
        FROM snmp_metrics
        WHERE metric_name LIKE 'if_%%_octets%%'
          AND timestamp >= %s AND timestamp < %s
        GROUP BY device_name
    """, (start, end))
    traffic_stats = cur.fetchall()

    # Alerts fired
    cur.execute(
        "SELECT device_name, alert_type, alert_value, threshold, triggered_at "
        "FROM snmp_alert_state WHERE triggered_at >= %s AND triggered_at < %s "
        "ORDER BY triggered_at",
        (start, end))
    alerts = cur.fetchall()

    conn.close()

    # Build digest text
    date_str = yesterday.strftime("%Y-%m-%d")
    lines = [
        f"SNMP metrics digest {date_str}:",
        f"  Total data points: {total_metrics:,}",
        f"  Devices reporting: {len(devices)} ({', '.join(d[0] for d in devices)})",
        "",
    ]

    if cpu_stats:
        lines.append("CPU Load (5min avg):")
        for name, avg_load, peak_load in cpu_stats:
            lines.append(f"  {name}: avg={avg_load}, peak={peak_load}")
        lines.append("")

    if mem_stats:
        lines.append("Memory:")
        for name, total_kb, min_avail_kb in mem_stats:
            if total_kb and total_kb > 0:
                total_gb = total_kb / 1024 / 1024
                if min_avail_kb is not None and min_avail_kb > 0:
                    used_pct = ((total_kb - min_avail_kb) / total_kb) * 100
                    lines.append(f"  {name}: {total_gb:.1f}GB total, peak usage {used_pct:.0f}%")
                else:
                    lines.append(f"  {name}: {total_gb:.1f}GB total")
        lines.append("")

    if alerts:
        lines.append(f"Alerts ({len(alerts)}):")
        for name, atype, val, thresh, ts in alerts:
            lines.append(f"  {ts.strftime('%H:%M')} {name}: {atype} ({val:.1f} > {thresh:.1f})")
        lines.append("")

    lines.append(f"Poll intervals: fast=60s, slow=300s. Retention: {30} days.")

    digest_text = "\n".join(lines)
    log(f"Digest: {total_metrics:,} metrics, {len(devices)} devices, {len(alerts)} alerts")

    # Ingest to vector memory
    remember(digest_text, {
        "type": "snmp_daily_digest",
        "date": date_str,
        "total_metrics": total_metrics,
        "devices": len(devices),
        "alerts": len(alerts),
    })

    # Post to Slack
    slack_msg = (
        f":bar_chart: *SNMP Daily Digest* ({date_str})\n"
        f"  :satellite: {len(devices)} devices · {total_metrics:,} data points\n"
    )
    if cpu_stats:
        top_cpu = max(cpu_stats, key=lambda x: x[2])
        slack_msg += f"  :zap: Peak CPU: {top_cpu[0]} @ {top_cpu[2]} load\n"
    if alerts:
        slack_msg += f"  :rotating_light: {len(alerts)} threshold alerts\n"
    else:
        slack_msg += f"  :white_check_mark: No threshold alerts\n"

    try:
        nova_config.post_both(slack_msg, slack_channel=nova_config.SLACK_NOTIFY)
    except Exception as e:
        log(f"Slack failed: {e}")

    log("Digest complete")


if __name__ == "__main__":
    run()
