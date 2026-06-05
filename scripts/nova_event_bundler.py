#!/usr/bin/env python3
"""
nova_event_bundler.py — Time-windowed event bundling (Frigate review items pattern).

Instead of listing every individual event, bundles related activity into
digestible groups with two-tier priority:
  - Alerts: needs immediate action (services still down, new attack sources, critical failures)
  - Detections: FYI (recovered issues, routine firewall blocks, single-failure retries)

Used by:
  - nova_morning_brief.py (overnight bundles)
  - nova_proactive_brief.py (2-hour bundles)
  - nova-control-web /api/events/bundled endpoint

Written by Jordan Koch.
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    import psycopg2
except ImportError:
    psycopg2 = None

OPS_DSN = "postgresql://kochj@127.0.0.1:5432/nova_ops"


def _query(sql, params=None):
    """Execute a query and return rows as dicts."""
    if not psycopg2:
        return []
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
    except Exception:
        return []


# ── Syslog Event Bundling ─────────────────────────────────────────────────────

def bundle_syslog_threats(hours: int = 12) -> list:
    """Bundle syslog threats by source IP and threat type."""
    rows = _query("""
        SELECT source_ip, threat_type, signature, COUNT(*) as count,
               MIN(received_at) as first_seen, MAX(received_at) as last_seen
        FROM syslog_events
        WHERE threat_type IS NOT NULL
          AND received_at > now() - make_interval(hours => %s)
        GROUP BY source_ip, threat_type, signature
        ORDER BY count DESC
    """, (hours,))

    bundles = []
    for row in rows:
        bundles.append({
            "type": "syslog_threat",
            "source_ip": str(row["source_ip"]) if row["source_ip"] else "unknown",
            "threat_type": row["threat_type"],
            "signature": row["signature"] or "",
            "count": row["count"],
            "first_seen": row["first_seen"].isoformat() if row["first_seen"] else "",
            "last_seen": row["last_seen"].isoformat() if row["last_seen"] else "",
            "priority": "alert" if row["count"] >= 5 or row["threat_type"] in ("exploit", "malware", "trojan") else "detection",
            "summary": f"{row['count']}x {row['threat_type']} from {row['source_ip'] or 'unknown'}" + (f" ({row['signature'][:40]})" if row['signature'] else ""),
        })
    return bundles


# ── Scheduler Failure Bundling ────────────────────────────────────────────────

def bundle_scheduler_failures(hours: int = 12) -> list:
    """Bundle scheduler task failures by task name."""
    rows = _query("""
        SELECT task_id, COUNT(*) as fail_count,
               COUNT(*) FILTER (WHERE exit_code = 0) as success_count,
               MAX(started_at) as last_attempt
        FROM scheduler_runs
        WHERE started_at > now() - make_interval(hours => %s)
          AND exit_code != 0
        GROUP BY task_id
        ORDER BY fail_count DESC
    """, (hours,))

    bundles = []
    for row in rows:
        is_persistent = row["fail_count"] >= 3 and row["success_count"] == 0
        bundles.append({
            "type": "scheduler_failure",
            "task": row["task_id"],
            "fail_count": row["fail_count"],
            "last_attempt": row["last_attempt"].isoformat() if row["last_attempt"] else "",
            "priority": "alert" if is_persistent else "detection",
            "summary": f"{row['task_id']}: {row['fail_count']} failures" + (" (persistent)" if is_persistent else " (retried ok)"),
        })
    return bundles


# ── Big Brother Event Bundling ────────────────────────────────────────────────

def bundle_bb_events(hours: int = 12) -> list:
    """Bundle Big Brother events by service name."""
    # BB events are stored in its state file or Redis — try reading from its API
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:37461/bb/events?n=200", timeout=5) as r:
            data = json.loads(r.read())
            events = data if isinstance(data, list) else data.get("events", [])
    except Exception:
        events = []

    if not events:
        return []

    # Filter to time window
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    by_service = defaultdict(list)
    for ev in events:
        ts_str = ev.get("ts") or ev.get("timestamp", "")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
        service = ev.get("service") or ev.get("issue", "unknown")
        by_service[service].append(ev)

    bundles = []
    for service, evts in by_service.items():
        severity = max((e.get("severity", "info") for e in evts),
                       key=lambda s: ["info", "warning", "critical"].index(s) if s in ["info", "warning", "critical"] else 0)
        has_unresolved = any(not e.get("resolved") for e in evts)
        bundles.append({
            "type": "bb_event",
            "service": service,
            "count": len(evts),
            "severity": severity,
            "priority": "alert" if has_unresolved and severity in ("warning", "critical") else "detection",
            "summary": f"{service}: {len(evts)} events ({severity})" + (" — UNRESOLVED" if has_unresolved else " — resolved"),
        })
    return bundles


# ── SNMP Threshold Alerts ─────────────────────────────────────────────────────

def bundle_snmp_alerts(hours: int = 12) -> list:
    """Bundle SNMP threshold alerts."""
    rows = _query("""
        SELECT device_name, alert_type, alert_value, threshold, triggered_at
        FROM snmp_alert_state
        WHERE triggered_at > now() - make_interval(hours => %s)
        ORDER BY triggered_at DESC
    """, (hours,))

    bundles = []
    for row in rows:
        bundles.append({
            "type": "snmp_alert",
            "device": row["device_name"],
            "alert_type": row["alert_type"],
            "value": row["alert_value"],
            "threshold": row["threshold"],
            "priority": "alert",
            "summary": f"{row['device_name']}: {row['alert_type']} ({row['alert_value']:.1f} > {row['threshold']:.1f})",
        })
    return bundles


# ── Main Bundler ──────────────────────────────────────────────────────────────

def bundle_all_events(hours: int = 12) -> dict:
    """Bundle all event sources into alerts and detections.

    Returns:
        {
            "alerts": [...],       # needs immediate action
            "detections": [...],   # FYI, awareness only
            "summary": str,        # one-line overview
            "total_events": int,
        }
    """
    all_bundles = []
    all_bundles.extend(bundle_syslog_threats(hours))
    all_bundles.extend(bundle_scheduler_failures(hours))
    all_bundles.extend(bundle_bb_events(hours))
    all_bundles.extend(bundle_snmp_alerts(hours))

    alerts = [b for b in all_bundles if b["priority"] == "alert"]
    detections = [b for b in all_bundles if b["priority"] == "detection"]

    total = sum(b.get("count", 1) for b in all_bundles)
    summary = f"{len(alerts)} alerts, {len(detections)} detections ({total} total events)"

    return {
        "alerts": alerts,
        "detections": detections,
        "summary": summary,
        "total_events": total,
    }


def format_for_brief(bundles: dict) -> str:
    """Format bundled events for morning/proactive brief Slack post."""
    lines = []

    if bundles["alerts"]:
        lines.append("*Alerts (needs action):*")
        for b in bundles["alerts"][:10]:
            lines.append(f"  :rotating_light: {b['summary']}")
    else:
        lines.append(":white_check_mark: No alerts requiring action")

    if bundles["detections"]:
        lines.append(f"\n*Detections ({len(bundles['detections'])} FYI):*")
        for b in bundles["detections"][:5]:
            lines.append(f"  :information_source: {b['summary']}")
        if len(bundles["detections"]) > 5:
            lines.append(f"  _...and {len(bundles['detections']) - 5} more_")

    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Bundle events from all sources")
    parser.add_argument("--hours", type=int, default=12)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = bundle_all_events(args.hours)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"\n{result['summary']}\n")
        print(format_for_brief(result))
