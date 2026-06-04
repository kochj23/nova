#!/usr/bin/env python3
"""
nova_syslog_daily_digest.py — Daily syslog summary ingested into Nova's vector memory.

Summarizes yesterday's syslog activity: top sources, threats, new devices,
unusual patterns. Ingests as a memory under source="infrastructure" so Nova
can reference network activity in conversations and journal posts.

Runs daily at 5am via scheduler.

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
LOG_FILE = Path.home() / ".openclaw/logs/syslog_daily_digest.log"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[syslog_digest {ts}] {msg}"
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


def generate_digest(conn, start, end):
    """Generate a syslog digest for the given time range."""
    cur = conn.cursor()

    # Total events
    cur.execute("SELECT count(*) FROM syslog_events WHERE received_at >= %s AND received_at < %s", (start, end))
    total = cur.fetchone()[0]

    # By source device
    cur.execute("""
        SELECT coalesce(hostname, host(source_ip)) as device, count(*) as cnt
        FROM syslog_events WHERE received_at >= %s AND received_at < %s
        GROUP BY device ORDER BY cnt DESC LIMIT 10
    """, (start, end))
    top_sources = cur.fetchall()

    # Threats
    cur.execute("""
        SELECT threat_type, signature, src_addr, dst_addr, action, received_at
        FROM syslog_events
        WHERE received_at >= %s AND received_at < %s AND threat_type IS NOT NULL
        ORDER BY received_at DESC LIMIT 20
    """, (start, end))
    threats = cur.fetchall()

    # Threat summary by type
    cur.execute("""
        SELECT threat_type, count(*) FROM syslog_events
        WHERE received_at >= %s AND received_at < %s AND threat_type IS NOT NULL
        GROUP BY threat_type ORDER BY count(*) DESC
    """, (start, end))
    threat_summary = cur.fetchall()

    # Unique hostnames (devices seen)
    cur.execute("""
        SELECT DISTINCT coalesce(hostname, host(source_ip)) as device
        FROM syslog_events WHERE received_at >= %s AND received_at < %s
    """, (start, end))
    devices_seen = [r[0] for r in cur.fetchall() if r[0]]

    # Severity breakdown
    cur.execute("""
        SELECT severity, count(*) FROM syslog_events
        WHERE received_at >= %s AND received_at < %s AND severity IS NOT NULL
        GROUP BY severity ORDER BY severity
    """, (start, end))
    severity_breakdown = cur.fetchall()

    # Top app_names (what's generating logs)
    cur.execute("""
        SELECT app_name, count(*) as cnt FROM syslog_events
        WHERE received_at >= %s AND received_at < %s AND app_name IS NOT NULL
        GROUP BY app_name ORDER BY cnt DESC LIMIT 10
    """, (start, end))
    top_apps = cur.fetchall()

    # Alerts fired
    cur.execute("""
        SELECT count(*) FROM syslog_events
        WHERE received_at >= %s AND received_at < %s AND alert_fired = true
    """, (start, end))
    alerts_fired = cur.fetchone()[0]

    cur.close()
    return {
        "total": total,
        "top_sources": top_sources,
        "threats": threats,
        "threat_summary": threat_summary,
        "devices_seen": devices_seen,
        "severity_breakdown": severity_breakdown,
        "top_apps": top_apps,
        "alerts_fired": alerts_fired,
    }


def format_digest(data, date_str):
    """Format digest data into a readable memory text."""
    lines = [f"Nova Syslog Daily Digest — {date_str}"]
    lines.append(f"Total events: {data['total']:,}")
    lines.append(f"Devices reporting: {len(data['devices_seen'])} ({', '.join(data['devices_seen'][:8])})")
    lines.append(f"Alerts fired to Slack: {data['alerts_fired']}")

    if data["top_sources"]:
        lines.append("\nTop sources:")
        for device, cnt in data["top_sources"][:7]:
            lines.append(f"  {device}: {cnt:,} events")

    if data["severity_breakdown"]:
        sev_names = {0: "emerg", 1: "alert", 2: "crit", 3: "err", 4: "warning", 5: "notice", 6: "info", 7: "debug"}
        lines.append("\nSeverity breakdown:")
        for sev, cnt in data["severity_breakdown"]:
            lines.append(f"  {sev_names.get(sev, f'sev{sev}')}: {cnt:,}")

    if data["top_apps"]:
        lines.append("\nTop processes/apps:")
        for app, cnt in data["top_apps"][:7]:
            lines.append(f"  {app}: {cnt:,}")

    if data["threat_summary"]:
        lines.append("\nThreat summary:")
        for ttype, cnt in data["threat_summary"]:
            lines.append(f"  {ttype}: {cnt}")

    if data["threats"]:
        lines.append("\nRecent threats (up to 5):")
        for ttype, sig, src, dst, action, ts in data["threats"][:5]:
            lines.append(f"  [{ttype}] {sig[:80]} (src={src}, action={action})")

    if not data["threat_summary"]:
        lines.append("\nNo security threats detected. Network clean.")

    return "\n".join(lines)


def main():
    log("=== Syslog Daily Digest starting ===")

    # Default: yesterday. If run with --today, use today.
    if "--today" in sys.argv:
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
        date_str = start.strftime("%Y-%m-%d") + " (partial, today so far)"
    else:
        now = datetime.now(timezone.utc)
        end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=1)
        date_str = start.strftime("%Y-%m-%d")

    log(f"Period: {start} to {end}")

    conn = psycopg2.connect(OPS_DSN)
    data = generate_digest(conn, start, end)
    conn.close()

    if data["total"] == 0:
        log("No syslog events in period. Skipping.")
        return

    digest_text = format_digest(data, date_str)
    log(f"Digest: {data['total']:,} events, {len(data['devices_seen'])} devices, {len(data['threat_summary'])} threat types")

    # Print summary
    print("\n" + digest_text + "\n")

    # Ingest into Nova's vector memory
    metadata = {
        "type": "syslog_daily_digest",
        "date": date_str,
        "total_events": data["total"],
        "devices_count": len(data["devices_seen"]),
        "threats_count": sum(cnt for _, cnt in data["threat_summary"]),
        "alerts_fired": data["alerts_fired"],
    }

    if remember(digest_text, metadata):
        log("✓ Digest ingested into Nova vector memory")
    else:
        log("✗ Failed to ingest digest")

    # Post summary to Slack
    slack_lines = [f":bar_chart: *Syslog Daily Digest* — {date_str}"]
    slack_lines.append(f"  Events: {data['total']:,} | Devices: {len(data['devices_seen'])} | Alerts: {data['alerts_fired']}")
    if data["threat_summary"]:
        threat_str = ", ".join(f"{t}({c})" for t, c in data["threat_summary"][:5])
        slack_lines.append(f"  Threats: {threat_str}")
    else:
        slack_lines.append("  :white_check_mark: No threats detected")
    if data["top_sources"]:
        top = data["top_sources"][0]
        slack_lines.append(f"  Top talker: {top[0]} ({top[1]:,} events)")

    try:
        nova_config.post_both("\n".join(slack_lines), slack_channel=nova_config.SLACK_NOTIFY)
    except Exception as e:
        log(f"Slack post error: {e}")

    log("Done.")


if __name__ == "__main__":
    main()
