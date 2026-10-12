#!/usr/bin/env python3
"""
nova_ops_context.py — Shared context provider for all article generation scripts.

Provides unified access to:
  - security_events (Wazuh → PG pipeline)
  - incidents (correlated security incidents)
  - host_threat_scores (per-host risk)
  - shared_observations (cross-system findings)
  - capacity_snapshots (infrastructure health)
  - snmp_metrics (network/host metrics)
  - syslog_events (firewall, auth, crash storms)
  - scheduler_runs (task success/failure)
  - grafana_annotations (BB heals, security events)

Import in any article script:
    from nova_ops_context import get_security_context, get_infra_context, get_full_context

Written by Jordan Koch.
"""

import json
import subprocess
from datetime import datetime


import nova_dsn as _nova_dsn  # noqa: E402
DB_DSN = _nova_dsn.pg_dsn("nova_ops")


def _pg_query(sql, params=None):
    """Run a PG query and return rows as dicts."""
    import psycopg2
    import psycopg2.extras
    try:
        conn = psycopg2.connect(DB_DSN)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        return []


def get_security_context(hours: int = 24) -> dict:
    """Get security context for article generation."""
    hours = int(hours)  # interpolated into SQL below; coerce so only a number can ever land there
    ctx = {}

    # Recent security events from Wazuh pipeline
    events = _pg_query("""
        SELECT agent_name, rule_level, rule_description, rule_groups, ts
        FROM security_events
        WHERE ts > NOW() - INTERVAL '%s hours'
        ORDER BY rule_level DESC, ts DESC
        LIMIT 50;
    """ % hours)
    ctx["security_events"] = events
    ctx["security_event_count"] = len(events)
    ctx["high_severity_count"] = sum(1 for e in events if e.get("rule_level", 0) >= 10)

    # Open incidents
    incidents = _pg_query("""
        SELECT title, severity, started_at, affected_services, events
        FROM incidents WHERE status = 'open'
        ORDER BY started_at DESC LIMIT 10;
    """)
    ctx["open_incidents"] = incidents

    # Threat scores
    scores = _pg_query("""
        SELECT DISTINCT ON (host_name) host_name, score, components, ts
        FROM host_threat_scores
        ORDER BY host_name, ts DESC;
    """)
    ctx["threat_scores"] = {s["host_name"]: s["score"] for s in scores}
    ctx["highest_threat_host"] = max(scores, key=lambda s: s["score"]) if scores else None

    # Wazuh alert level distribution
    levels = _pg_query("""
        SELECT rule_level, COUNT(*) as cnt
        FROM security_events
        WHERE ts > NOW() - INTERVAL '%s hours'
        GROUP BY rule_level ORDER BY rule_level DESC;
    """ % hours)
    ctx["alert_levels"] = {r["rule_level"]: r["cnt"] for r in levels}

    # Active response actions
    responses = _pg_query("""
        SELECT agent_name, rule_description, auto_response, ts
        FROM security_events
        WHERE auto_response IS NOT NULL
          AND ts > NOW() - INTERVAL '%s hours'
        ORDER BY ts DESC LIMIT 10;
    """ % hours)
    ctx["auto_responses"] = responses

    return ctx


def get_syslog_context(hours: int = 24) -> dict:
    """Get syslog/firewall context."""
    hours = int(hours)  # interpolated into SQL below; coerce so only a number can ever land there
    ctx = {}

    # Threat types
    threats = _pg_query("""
        SELECT threat_type, COUNT(*) as cnt
        FROM syslog_events
        WHERE threat_type IS NOT NULL
          AND received_at > NOW() - INTERVAL '%s hours'
        GROUP BY threat_type ORDER BY cnt DESC;
    """ % hours)
    ctx["threat_types"] = {t["threat_type"]: t["cnt"] for t in threats}

    # Firewall blocks
    blocks = _pg_query("""
        SELECT COUNT(*) as cnt
        FROM syslog_events
        WHERE action IN ('blocked', 'drop')
          AND received_at > NOW() - INTERVAL '%s hours';
    """ % hours)
    ctx["firewall_blocks"] = blocks[0]["cnt"] if blocks else 0

    # SSH events
    ssh = _pg_query("""
        SELECT hostname, COUNT(*) as cnt
        FROM syslog_events
        WHERE app_name = 'sshd'
          AND received_at > NOW() - INTERVAL '%s hours'
        GROUP BY hostname ORDER BY cnt DESC;
    """ % hours)
    ctx["ssh_events_by_host"] = {s["hostname"]: s["cnt"] for s in ssh}

    # Event rate
    rate = _pg_query("""
        SELECT COUNT(*) as total,
               COUNT(*) FILTER (WHERE severity <= 3) as warnings,
               COUNT(*) FILTER (WHERE severity <= 2) as errors
        FROM syslog_events
        WHERE received_at > NOW() - INTERVAL '%s hours';
    """ % hours)
    if rate:
        ctx["total_events"] = rate[0]["total"]
        ctx["warning_events"] = rate[0]["warnings"]
        ctx["error_events"] = rate[0]["errors"]

    return ctx


def get_infra_context(hours: int = 24) -> dict:
    """Get infrastructure/capacity context."""
    hours = int(hours)  # interpolated into SQL below; coerce so only a number can ever land there
    ctx = {}

    # Latest capacity per host
    capacity = _pg_query("""
        SELECT DISTINCT ON (device_name)
            device_name, cpu_load_5m, cpu_headroom_pct,
            mem_used_mb, mem_total_mb, mem_headroom_pct,
            disk_worst_pct, overall_status, ts
        FROM capacity_snapshots
        ORDER BY device_name, ts DESC;
    """)
    ctx["hosts"] = {c["device_name"]: dict(c) for c in capacity}

    # Hosts in warning/critical state
    ctx["degraded_hosts"] = [c["device_name"] for c in capacity if c["overall_status"] != "ok"]

    # Scheduler health
    sched = _pg_query("""
        SELECT
            COUNT(*) as total_runs,
            SUM(CASE WHEN exit_code = 0 THEN 1 ELSE 0 END) as success,
            SUM(CASE WHEN exit_code != 0 THEN 1 ELSE 0 END) as failures,
            ROUND(AVG(duration_s)::numeric, 1) as avg_duration
        FROM scheduler_runs
        WHERE started_at > NOW() - INTERVAL '%s hours';
    """ % hours)
    if sched:
        ctx["scheduler"] = dict(sched[0])

    # Big Brother heals
    heals = _pg_query("""
        SELECT title, ts, tags
        FROM grafana_annotations
        WHERE source = 'big_brother'
          AND ts > NOW() - INTERVAL '%s hours'
        ORDER BY ts DESC LIMIT 20;
    """ % hours)
    ctx["bb_heals"] = heals

    # Shared observations
    obs = _pg_query("""
        SELECT observer, category, subject, observation, severity, observed_at
        FROM shared_observations
        WHERE observed_at > NOW() - INTERVAL '%s hours'
        ORDER BY observed_at DESC LIMIT 30;
    """ % hours)
    ctx["observations"] = obs

    return ctx


def get_wireless_context(hours: int = 24) -> dict:
    """Passive Wi-Fi survey (nova_wifi_survey on nova-core7) + nova_wifi_guard findings. 2026-10-12."""
    runs = _pg_query("""SELECT count(*) AS runs, round(avg(aps)) AS avg_aps, max(stations) AS max_stations,
                               coalesce(sum((SELECT sum(v::int) FROM jsonb_each_text(deauth) AS e(k, v))), 0) AS deauth
                        FROM wifi_survey_runs WHERE ts > now() - make_interval(hours => %s)""", (hours,))
    finds = _pg_query("""SELECT kind, count(*) AS n, count(DISTINCT bssid) AS bssids FROM wifi_guard_findings
                         WHERE ts > now() - make_interval(hours => %s) GROUP BY kind""", (hours,))
    ctx = dict(runs[0]) if runs else {}
    ctx["findings"] = {f["kind"]: {"events": f["n"], "bssids": f["bssids"]} for f in finds}
    return ctx


def get_radio_context(hours: int = 24) -> dict:
    """SDR load balancer state (nova_sdr_placement / nova_sdr_actuator). 2026-10-12."""
    health = _pg_query("""SELECT count(*) AS tuners, count(*) FILTER (WHERE ok) AS healthy FROM sdr_health
                          WHERE ts > now() - interval '15 minutes'""")
    moves = _pg_query("""SELECT count(*) AS moves FROM sdr_placement_log WHERE action = 'apply'
                         AND ts > now() - make_interval(hours => %s)""", (hours,))
    placed = _pg_query("""SELECT t.label, a.tuner_id FROM sdr_assignments a JOIN sdr_targets t ON t.id = a.target_id
                          ORDER BY t.priority DESC""")
    ctx = dict(health[0]) if health else {}
    ctx["moves"] = moves[0]["moves"] if moves else 0
    ctx["placements"] = {p["label"]: p["tuner_id"] for p in placed}
    return ctx


def get_full_context(hours: int = 24) -> dict:
    """Get complete operational context for article generation."""
    return {
        "security": get_security_context(hours),
        "syslog": get_syslog_context(hours),
        "infra": get_infra_context(hours),
        "wireless": get_wireless_context(hours),
        "radio": get_radio_context(hours),
        "generated_at": datetime.now().isoformat(),
        "window_hours": hours,
    }


def format_security_brief(ctx: dict) -> str:
    """Format security context as a text block for LLM prompts."""
    sec = ctx.get("security", {})
    syslog = ctx.get("syslog", {})
    infra = ctx.get("infra", {})
    lines = []

    lines.append("=== INFRASTRUCTURE SECURITY STATUS ===")
    lines.append(f"Security events (last {ctx.get('window_hours', 24)}h): {sec.get('security_event_count', 0)}")
    lines.append(f"High severity (L10+): {sec.get('high_severity_count', 0)}")
    lines.append(f"Open incidents: {len(sec.get('open_incidents', []))}")
    lines.append(f"Firewall blocks: {syslog.get('firewall_blocks', 0)}")
    lines.append(f"Syslog events: {syslog.get('total_events', 0)} ({syslog.get('warning_events', 0)} warnings)")

    if sec.get("threat_scores"):
        lines.append(f"\nHost threat scores: {sec['threat_scores']}")

    if sec.get("open_incidents"):
        lines.append("\nOPEN INCIDENTS:")
        for inc in sec["open_incidents"][:5]:
            lines.append(f"  - [{inc.get('severity', '?')}] {inc.get('title', '?')}")

    if sec.get("auto_responses"):
        lines.append(f"\nAuto-responses fired: {len(sec['auto_responses'])}")
        for r in sec["auto_responses"][:3]:
            lines.append(f"  - {r.get('agent_name')}: {r.get('auto_response')} ({r.get('rule_description', '')})")

    if syslog.get("threat_types"):
        lines.append(f"\nSyslog threat types: {syslog['threat_types']}")

    if syslog.get("ssh_events_by_host"):
        lines.append(f"SSH events: {syslog['ssh_events_by_host']}")

    if sec.get("security_events"):
        lines.append(f"\nTOP SECURITY EVENTS:")
        for e in sec["security_events"][:15]:
            lines.append(f"  - [L{e.get('rule_level', '?')}] {e.get('agent_name', '?')}: {e.get('rule_description', '?')}")

    wl = ctx.get("wireless", {})
    if wl.get("runs"):
        lines.append(f"\nWIRELESS (passive survey, receive-only): {wl['runs']} surveys, ~{wl.get('avg_aps')} APs in range, "
                     f"max {wl.get('max_stations')} client devices, {wl.get('deauth', 0)} deauth/disassoc frames heard")
        if wl.get("findings"):
            lines.append(f"  Wi-Fi guard findings: {wl['findings']}")
        else:
            lines.append("  Wi-Fi guard: no evil twins or deauth floods against our access points")

    return "\n".join(lines)


def format_infra_brief(ctx: dict) -> str:
    """Format infrastructure context as a text block for LLM prompts."""
    infra = ctx.get("infra", {})
    lines = []

    lines.append("=== INFRASTRUCTURE STATUS ===")

    if infra.get("degraded_hosts"):
        lines.append(f"DEGRADED HOSTS: {', '.join(infra['degraded_hosts'])}")

    if infra.get("hosts"):
        lines.append("\nPer-host status:")
        for name, h in infra["hosts"].items():
            status = h.get("overall_status", "?")
            cpu = h.get("cpu_headroom_pct", "?")
            mem = h.get("mem_headroom_pct", "?")
            disk = h.get("disk_worst_pct", "?")
            lines.append(f"  {name}: status={status}, cpu_headroom={cpu}%, mem_headroom={mem}%, disk_worst={disk}%")

    if infra.get("scheduler"):
        s = infra["scheduler"]
        lines.append(f"\nScheduler: {s.get('total_runs', 0)} runs, {s.get('failures', 0)} failures, avg {s.get('avg_duration', '?')}s")

    if infra.get("bb_heals"):
        lines.append(f"\nBig Brother heals: {len(infra['bb_heals'])}")
        for h in infra["bb_heals"][:5]:
            lines.append(f"  - {h.get('title', '?')}")

    if infra.get("observations"):
        lines.append(f"\nShared observations ({len(infra['observations'])}):")
        for o in infra["observations"][:10]:
            lines.append(f"  - [{o.get('category')}] {o.get('observer')}: {o.get('observation', '')[:100]}")

    radio = ctx.get("radio", {})
    if radio.get("tuners"):
        lines.append(f"\nRADIO (SDR load balancer): {radio['healthy']}/{radio['tuners']} tuners healthy, "
                     f"{radio.get('moves', 0)} automatic frequency moves in window")
        for label, tuner in list(radio.get("placements", {}).items())[:8]:
            lines.append(f"  - {label} -> {tuner}")

    return "\n".join(lines)
