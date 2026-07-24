#!/usr/bin/env python3
"""
nova_notify.py — the ONE way Nova emits a notification.

Every script calls `notify(...)` to describe WHAT happened and HOW important it is.
It does NOT choose a Slack channel — it writes a structured row to the event bus
(telemetry.events) and the nova_notifier daemon decides routing, dedup, rate-limit,
and correlation centrally. This replaces ~95 scripts each hardcoding SLACK_NOTIFY/
SLACK_BB.

    from nova_notify import notify
    notify("UNAS storage low", body="1.4TB free", level="warning", category="storage",
           dedup_key="unas-storage-low")

Levels: info | warning | critical.  Routing (level/category -> channel) lives in
nova_notifier, so changing where something goes is a one-line policy edit there.

Never raises — a notification must never crash its caller.
"""
import json
import os
import sys
import subprocess

_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
_VALID_LEVELS = ("info", "warning", "critical")


def notify(title, body=None, level="info", category=None, source=None,
           dedup_key=None, correlation_id=None, meta=None) -> bool:
    """Enqueue a notification onto the event bus. Returns True if persisted."""
    if level not in _VALID_LEVELS:
        level = "info"
    if not source:
        # Auto-detect the emitting script from argv[0] (e.g. nova_calendar.py).
        source = os.path.basename(sys.argv[0]) or "unknown"
    payload = {
        "source": source, "level": level, "category": category,
        "title": str(title)[:500], "body": (str(body) if body is not None else None),
        "dedup_key": dedup_key, "correlation_id": correlation_id,
        "meta": json.dumps(meta or {}),
    }
    try:
        import psycopg2
        with psycopg2.connect(_DSN, connect_timeout=5) as c:
            with c.cursor() as cur:
                cur.execute(
                    "INSERT INTO telemetry.events "
                    "(source, level, category, title, body, dedup_key, correlation_id, meta) "
                    "VALUES (%(source)s, %(level)s, %(category)s, %(title)s, %(body)s, "
                    "%(dedup_key)s, %(correlation_id)s, %(meta)s::jsonb)",
                    payload)
        return True
    except Exception:
        # Fallback: psql subprocess (psycopg2 may be missing in some venvs).
        try:
            subprocess.run(
                ["psql", _DSN, "-v", "ON_ERROR_STOP=1", "-c",
                 "INSERT INTO telemetry.events (source,level,category,title,body,dedup_key,correlation_id,meta) "
                 "VALUES (:'s',:'l',:'c',:'t',:'b',:'dk',:'ci',:'m'::jsonb)",
                 "-v", f"s={source}", "-v", f"l={level}", "-v", f"c={category or ''}",
                 "-v", f"t={title}", "-v", f"b={body or ''}", "-v", f"dk={dedup_key or ''}",
                 "-v", f"ci={correlation_id or ''}", "-v", "m={}"],
                capture_output=True, timeout=10)
            return True
        except Exception:
            return False


if __name__ == "__main__":
    # CLI: nova_notify.py "<title>" [level] [category]  — for shell-script emitters.
    if len(sys.argv) < 2:
        print("usage: nova_notify.py <title> [level] [category] [body]", file=sys.stderr)
        sys.exit(2)
    ok = notify(sys.argv[1],
                level=sys.argv[2] if len(sys.argv) > 2 else "info",
                category=sys.argv[3] if len(sys.argv) > 3 else None,
                body=sys.argv[4] if len(sys.argv) > 4 else None)
    print("queued" if ok else "FAILED")
    sys.exit(0 if ok else 1)
