#!/usr/bin/env python3
"""
nova_shared_observations.py — Joint Memory (#31)

Shared observations system for Nova and Claude to cross-pollinate insights.
Nova logs runtime patterns; Claude logs code patterns. Both read the other's notes.

Usage:
    # Record an observation
    from nova_shared_observations import observe, get_observations, get_unacked

    observe("nova", "runtime", "memory-server",
            "Response time spiking >2s at 3am consistently",
            severity="warning", metadata={"avg_ms": 2100, "time": "03:00"})

    # Read observations from the other party
    notes = get_observations(observer="claude", category="code", limit=20)

    # Acknowledge
    ack("nova", observation_id=42)

Also runs as HTTP API on port 37470 for Nova gateway integration.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg2
import psycopg2.extras

DB_DSN = "host=localhost dbname=nova_ops user=kochj"

def _conn():
    c = psycopg2.connect(DB_DSN)
    c.autocommit = True
    return c


def observe(observer, category, subject, observation, severity="info", metadata=None, expires_hours=None):
    """Record a shared observation."""
    conn = _conn()
    cur = conn.cursor()
    expires = None
    if expires_hours:
        from datetime import timedelta
        expires = datetime.now() + timedelta(hours=expires_hours)
    cur.execute("""
        INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (observer, category, subject, observation, severity,
          json.dumps(metadata or {}), expires))
    obs_id = cur.fetchone()[0]
    cur.close()
    conn.close()
    return obs_id


def get_observations(observer=None, category=None, subject=None, severity=None,
                     unacked_only=False, limit=50):
    """Query observations with filters."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    clauses = ["(expires_at IS NULL OR expires_at > now())"]
    params = []

    if observer:
        clauses.append("observer = %s")
        params.append(observer)
    if category:
        clauses.append("category = %s")
        params.append(category)
    if subject:
        clauses.append("subject ILIKE %s")
        params.append(f"%{subject}%")
    if severity:
        clauses.append("severity = %s")
        params.append(severity)
    if unacked_only:
        clauses.append("acknowledged_by IS NULL")

    where = " AND ".join(clauses)
    cur.execute(f"""
        SELECT * FROM shared_observations
        WHERE {where}
        ORDER BY observed_at DESC
        LIMIT %s
    """, params + [limit])
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


def ack(acker, observation_id):
    """Acknowledge an observation from the other party."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE shared_observations
        SET acknowledged_by = %s, acknowledged_at = now()
        WHERE id = %s AND acknowledged_by IS NULL
    """, (acker, observation_id))
    cur.close()
    conn.close()


def get_unacked(for_observer):
    """Get observations meant for this observer to review (from the other party)."""
    other = "claude" if for_observer == "nova" else "nova"
    return get_observations(observer=other, unacked_only=True)


def summary():
    """Get observation counts by observer and category."""
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT observer, category, severity, COUNT(*) as count
        FROM shared_observations
        WHERE expires_at IS NULL OR expires_at > now()
        GROUP BY observer, category, severity
        ORDER BY observer, category, severity
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]


# ── HTTP API (optional — run as standalone) ──────────────────────────────────

if __name__ == "__main__":
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import urllib.parse

    PORT = 37470

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)

            if parsed.path == "/health":
                self._json(200, {"status": "ok", "service": "shared_observations"})
            elif parsed.path == "/observations":
                obs = get_observations(
                    observer=params.get("observer", [None])[0],
                    category=params.get("category", [None])[0],
                    subject=params.get("subject", [None])[0],
                    severity=params.get("severity", [None])[0],
                    unacked_only=params.get("unacked", ["false"])[0] == "true",
                    limit=int(params.get("limit", ["50"])[0]),
                )
                self._json(200, obs)
            elif parsed.path == "/summary":
                self._json(200, summary())
            elif parsed.path == "/unacked":
                who = params.get("for", ["nova"])[0]
                self._json(200, get_unacked(who))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            if self.path == "/observe":
                obs_id = observe(
                    observer=body["observer"],
                    category=body["category"],
                    subject=body["subject"],
                    observation=body["observation"],
                    severity=body.get("severity", "info"),
                    metadata=body.get("metadata"),
                    expires_hours=body.get("expires_hours"),
                )
                self._json(201, {"id": obs_id})
            elif self.path == "/ack":
                ack(body["acker"], body["id"])
                self._json(200, {"acked": True})
            else:
                self._json(404, {"error": "not found"})

        def _json(self, code, data):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data, default=str).encode())

        def log_message(self, format, *args):
            pass

    print(f"Shared Observations API listening on port {PORT}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
