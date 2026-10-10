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
import re
import sys
import subprocess

import nova_dsn as _nova_dsn  # noqa: E402
_DSN = _nova_dsn.pg_dsn("nova_ops")
_VALID_LEVELS = ("info", "warning", "critical")


def notify(title, body=None, level="info", category=None, source=None,
           dedup_key=None, correlation_id=None, meta=None) -> bool:
    """Enqueue a notification onto the event bus. Returns True if persisted."""
    if level not in _VALID_LEVELS:
        level = "info"
    if not source:
        # Auto-detect the emitting script from argv[0] (e.g. nova_calendar.py).
        source = os.path.basename(sys.argv[0]) or "unknown"

    # STATE-CHANGE BY DEFAULT: if a producer didn't supply a dedup_key, derive a stable one from
    # (source, category, title-with-volatile-numbers-stripped) so an ongoing condition re-firing
    # every cycle collapses at the notifier instead of flooding. This makes dedup opt-OUT, not
    # opt-in — no producer can leak the whole storm by forgetting a key (the way negative_space
    # did: 196 NULL-key fires in one night). Digits are stripped because the parts that change on
    # each re-fire of the SAME condition are durations, counts, %, and timestamps; the words that
    # distinguish genuinely-different events (sensor names, "First"/"Second" bed) survive. A
    # producer with meaningful numeric IDs (incidents) or one that truly wants every event
    # distinct should pass its own dedup_key (include a uuid to force always-send).
    if not dedup_key:
        _stable = re.sub(r"\d[\d:.,%\s/_-]*", "#", str(title)).strip()[:80]
        dedup_key = f"auto:{source}:{category or '-'}:{_stable}"

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
                 "-v", f"ci={correlation_id or ''}", "-v", f"m={payload['meta']}"],
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
