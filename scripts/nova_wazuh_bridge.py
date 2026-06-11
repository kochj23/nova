#!/usr/bin/env python3
"""
nova_wazuh_bridge.py — Unified security operations bridge.

Connects Wazuh SIEM, Big Brother, Nova's vector memory, shared observations,
the incidents table, and Grafana annotations into a closed-loop security system.

Runs every 2 minutes via scheduler. Each run:
  1. Pulls new Wazuh alerts from OpenSearch → writes to security_events (PG)
  2. Correlates events across sources (SNMP + syslog + Wazuh) → creates incidents
  3. Computes per-host threat scores
  4. Writes Grafana annotations for significant events
  5. Ingests novel threats into Nova's vector memory
  6. Auto-gathers forensics for high-severity events
  7. Escalates to Claude queue when human action needed

Written by Jordan Koch.
"""

import base64
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg2
import psycopg2.extras

DB_DSN = "host=localhost dbname=nova_ops user=kochj"
WAZUH_URL = "https://192.168.1.7:9200"
WAZUH_CREDS = base64.b64encode(b"admin:admin").decode()
MEMORY_SERVER = "http://192.168.1.6:18790"
POLL_WINDOW_MINUTES = 3
HIGH_SEVERITY_THRESHOLD = 10
INCIDENT_CORRELATION_WINDOW_S = 300
THREAT_SCORE_DECAY = 0.9

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[wazuh-bridge {ts}] {msg}", flush=True)


def pg_connect():
    return psycopg2.connect(DB_DSN)


def wazuh_query(query_body):
    req = urllib.request.Request(
        f"{WAZUH_URL}/wazuh-alerts-*/_search",
        data=json.dumps(query_body).encode(),
        headers={
            "Authorization": f"Basic {WAZUH_CREDS}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=15, context=_ssl_ctx)
    return json.loads(resp.read())


# ── Step 1: Pull Wazuh alerts → security_events ─────────────────────────────

def sync_wazuh_to_pg():
    """Pull recent Wazuh alerts and write to security_events table."""
    conn = pg_connect()
    cur = conn.cursor()

    cur.execute("SELECT MAX(ts) FROM security_events WHERE source = 'wazuh';")
    last_ts = cur.fetchone()[0]
    since = last_ts.isoformat() if last_ts else f"now-{POLL_WINDOW_MINUTES}m"

    query = {
        "size": 200,
        "sort": [{"timestamp": {"order": "asc"}}],
        "query": {
            "bool": {
                "must": [
                    {"range": {"timestamp": {"gt": since}}},
                    {"range": {"rule.level": {"gte": 3}}},
                ]
            }
        },
    }

    try:
        data = wazuh_query(query)
    except Exception as e:
        log(f"Wazuh query failed: {e}")
        conn.close()
        return 0

    hits = data.get("hits", {}).get("hits", [])
    if not hits:
        conn.close()
        return 0

    inserted = 0
    for hit in hits:
        src = hit["_source"]
        rule = src.get("rule", {})
        agent = src.get("agent", {})
        net = src.get("data", {})

        try:
            cur.execute("""
                INSERT INTO security_events (ts, source, agent_name, rule_id, rule_level,
                    rule_description, rule_groups, src_ip, dst_ip, full_log, metadata)
                VALUES (%s, 'wazuh', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (
                src.get("timestamp"),
                agent.get("name"),
                rule.get("id"),
                rule.get("level"),
                rule.get("description"),
                rule.get("groups", []),
                net.get("srcip"),
                net.get("dstip"),
                src.get("full_log", "")[:2000],
                json.dumps({"wazuh_id": hit["_id"], "agent_id": agent.get("id")}),
            ))
            inserted += 1
        except Exception as e:
            log(f"Insert error: {e}")
            conn.rollback()
            continue

    conn.commit()
    conn.close()
    log(f"Synced {inserted} Wazuh alerts to PG")
    return inserted


# ── Step 2: Correlate events → create incidents ──────────────────────────────

def correlate_events():
    """Find clusters of related events and create incidents."""
    conn = pg_connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT agent_name, rule_level, rule_description, rule_groups, ts, id
        FROM security_events
        WHERE ts > NOW() - INTERVAL '10 minutes'
          AND correlated = FALSE
          AND rule_level >= %s
        ORDER BY ts;
    """, (HIGH_SEVERITY_THRESHOLD,))

    events = cur.fetchall()
    if not events:
        conn.close()
        return

    # Group by agent + time window
    incidents = {}
    for evt in events:
        key = evt["agent_name"] or "unknown"
        if key not in incidents:
            incidents[key] = []
        incidents[key].append(evt)

    for agent, agent_events in incidents.items():
        if len(agent_events) < 1:
            continue

        # Create or update incident
        title = f"Security event on {agent}: {agent_events[0]['rule_description']}"
        if len(agent_events) > 1:
            title = f"Correlated security events on {agent} ({len(agent_events)} events)"

        severity = "critical" if any(e["rule_level"] >= 12 for e in agent_events) else "warning"

        cur.execute("""
            INSERT INTO incidents (title, severity, affected_services, events)
            VALUES (%s, %s, %s, %s)
            RETURNING id;
        """, (
            title,
            severity,
            [agent],
            json.dumps([{"event_id": e["id"], "desc": e["rule_description"],
                         "level": e["rule_level"], "ts": e["ts"].isoformat()}
                        for e in agent_events]),
        ))
        incident_id = cur.fetchone()["id"]

        # Mark events as correlated
        event_ids = [e["id"] for e in agent_events]
        cur.execute("""
            UPDATE security_events SET correlated = TRUE, incident_id = %s
            WHERE id = ANY(%s);
        """, (incident_id, event_ids))

        # Write Grafana annotation
        cur.execute("""
            INSERT INTO grafana_annotations (ts, title, text, tags, source)
            VALUES (%s, %s, %s, %s, 'wazuh');
        """, (
            agent_events[0]["ts"],
            f"🚨 {title}",
            "\n".join(f"L{e['rule_level']}: {e['rule_description']}" for e in agent_events[:5]),
            ["security", "incident", severity, agent],
        ))

        log(f"Created incident: {title} (severity={severity})")

    conn.commit()
    conn.close()


# ── Step 3: Compute threat scores ────────────────────────────────────────────

def compute_threat_scores():
    """Rolling threat score per host based on recent security events."""
    conn = pg_connect()
    cur = conn.cursor()

    cur.execute("""
        SELECT agent_name,
            COUNT(*) FILTER (WHERE rule_level >= 10) AS critical_events,
            COUNT(*) FILTER (WHERE rule_level BETWEEN 7 AND 9) AS warning_events,
            COUNT(*) FILTER (WHERE rule_level < 7) AS info_events,
            COUNT(*) FILTER (WHERE rule_groups && ARRAY['syscheck','rootcheck']) AS fim_events,
            COUNT(*) FILTER (WHERE rule_groups && ARRAY['authentication_failed','sshd']) AS auth_events
        FROM security_events
        WHERE ts > NOW() - INTERVAL '1 hour'
          AND agent_name IS NOT NULL
        GROUP BY agent_name;
    """)

    for row in cur.fetchall():
        agent, critical, warning, info, fim, auth = row
        score = (critical * 30) + (warning * 5) + (info * 1) + (fim * 10) + (auth * 8)
        components = {
            "critical_events": critical,
            "warning_events": warning,
            "fim_changes": fim,
            "auth_failures": auth,
        }

        cur.execute("""
            INSERT INTO host_threat_scores (ts, host_name, score, components)
            VALUES (NOW(), %s, %s, %s);
        """, (agent, score, json.dumps(components)))

    conn.commit()
    conn.close()


# ── Step 4: Write shared observations ────────────────────────────────────────

def write_observations():
    """Write notable security findings as shared observations."""
    conn = pg_connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT agent_name, rule_description, rule_level, ts
        FROM security_events
        WHERE ts > NOW() - INTERVAL '5 minutes'
          AND rule_level >= %s
        ORDER BY rule_level DESC
        LIMIT 5;
    """, (HIGH_SEVERITY_THRESHOLD,))

    events = cur.fetchall()
    for evt in events:
        cur.execute("""
            INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
            VALUES ('wazuh_bridge', 'security', %s, %s, %s, %s)
            ON CONFLICT DO NOTHING;
        """, (
            f"Alert on {evt['agent_name']}",
            f"[L{evt['rule_level']}] {evt['rule_description']}",
            "critical" if evt["rule_level"] >= 12 else "warning",
            json.dumps({"agent": evt["agent_name"], "rule_level": evt["rule_level"],
                        "ts": evt["ts"].isoformat()}),
        ))

    conn.commit()
    conn.close()


# ── Step 5: Ingest novel threats into vector memory ──────────────────────────

def ingest_to_memory():
    """Ingest novel high-severity security events into Nova's vector memory."""
    conn = pg_connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT DISTINCT rule_description, rule_level, agent_name, rule_groups
        FROM security_events
        WHERE ts > NOW() - INTERVAL '5 minutes'
          AND rule_level >= 10
        LIMIT 5;
    """)

    events = cur.fetchall()
    conn.close()

    for evt in events:
        text = (
            f"Security alert (level {evt['rule_level']}) on {evt['agent_name']}: "
            f"{evt['rule_description']}. "
            f"Groups: {', '.join(evt['rule_groups'] or [])}. "
            f"Detected by Wazuh SIEM."
        )

        try:
            payload = json.dumps({
                "text": text,
                "source": "security",
                "metadata": {"type": "wazuh_alert", "level": evt["rule_level"]},
            }).encode()
            req = urllib.request.Request(
                f"{MEMORY_SERVER}/ingest",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f"Memory ingest failed: {e}")


# ── Step 6: Auto-forensics for high severity ─────────────────────────────────

def auto_forensics():
    """Capture system state on high-severity events."""
    conn = pg_connect()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT id, agent_name, rule_description, rule_level
        FROM security_events
        WHERE ts > NOW() - INTERVAL '5 minutes'
          AND rule_level >= 12
          AND auto_response IS NULL
        LIMIT 3;
    """)

    events = cur.fetchall()
    if not events:
        conn.close()
        return

    for evt in events:
        log(f"Auto-forensics for L{evt['rule_level']} on {evt['agent_name']}")
        forensics = {}

        try:
            r = subprocess.run(["netstat", "-an"], capture_output=True, text=True, timeout=5)
            forensics["netstat"] = r.stdout[:5000]
        except Exception:
            pass

        try:
            r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
            forensics["processes"] = r.stdout[:5000]
        except Exception:
            pass

        cur.execute("""
            UPDATE security_events
            SET auto_response = 'forensics_captured',
                metadata = metadata || %s
            WHERE id = %s;
        """, (json.dumps({"forensics": forensics}), evt["id"]))

        # Escalate to Claude queue
        cur.execute("""
            INSERT INTO claude_queue (session_id, description, priority, context)
            VALUES (
                (SELECT session_id FROM claude_sessions ORDER BY started_at DESC LIMIT 1),
                %s, 1, %s
            );
        """, (
            f"SECURITY: L{evt['rule_level']} alert on {evt['agent_name']} — {evt['rule_description']}",
            json.dumps({
                "event_id": evt["id"],
                "agent": evt["agent_name"],
                "description": evt["rule_description"],
                "forensics_captured": True,
                "action_needed": "Investigate and remediate",
            }),
        ))

    conn.commit()
    conn.close()


# ── Step 7: Write BB heal events as annotations ──────────────────────────────

def sync_bb_annotations():
    """Pull recent Big Brother heal events and write as Grafana annotations."""
    try:
        resp = urllib.request.urlopen("http://192.168.1.6:37461/bb/events?n=20", timeout=5)
        events = json.loads(resp.read())
    except Exception:
        return

    if not events:
        return

    conn = pg_connect()
    cur = conn.cursor()

    for evt in events[:10]:
        ts = evt.get("ts")
        if not ts:
            continue

        cur.execute("""
            INSERT INTO grafana_annotations (ts, title, text, tags, source)
            VALUES (%s, %s, %s, %s, 'big_brother')
            ON CONFLICT DO NOTHING;
        """, (
            ts,
            f"🔧 BB: {evt.get('event', 'heal')}",
            evt.get("detail", ""),
            ["big_brother", evt.get("severity", "info"), evt.get("service", "system")],
        ))

    conn.commit()
    conn.close()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("Nova Wazuh Bridge starting...")

    count = sync_wazuh_to_pg()
    correlate_events()
    compute_threat_scores()
    write_observations()
    sync_bb_annotations()

    if count > 0:
        ingest_to_memory()

    auto_forensics()

    log("Bridge run complete")


if __name__ == "__main__":
    main()
