#!/usr/bin/env python3
"""nova_organ_board.py — wish #66: the table the organs share.

One view, nova_ops.organ_board, with the LATEST row of every organ side by side (affect, time_sense,
attention_focus, core_liveness, security_organ, presence, embodiment, contact_sense, boiler). presence is Jordan's
presence_state row (the single source of truth for occupancy, 2026-10-08); embodiment and time_sense read it too., and board(cur) -> dict so any
organ can read the others before it talks to Jordan. First consumer: nova_security_organ downgrades a
"new device" when a known person walked in minutes ago (a phone arriving with its owner is not an intruder
at 03:00 levels). Read-only. --report --selftest. Approved 2026-10-04.
"""
import nova_dsn as _nova_dsn  # noqa: E402
import sys, os, json
import psycopg2

OPS = os.environ.get("NOVA_OPS_DSN", _nova_dsn.pg_dsn("nova_ops"))
VIEW = """
CREATE OR REPLACE VIEW organ_board AS
SELECT 'affect'::text AS organ, computed_at AS ts, label AS state,
       jsonb_build_object('valence', valence, 'arousal', arousal) AS detail
  FROM (SELECT * FROM affect_state ORDER BY computed_at DESC LIMIT 1) a
UNION ALL
SELECT 'time_sense', ts, tempo, jsonb_build_object('sentence', sentence, 'stretch_h', stretch_h)
  FROM (SELECT * FROM time_sense ORDER BY ts DESC LIMIT 1) t
UNION ALL
SELECT 'attention_focus', updated_at, 'tracking', jsonb_build_object('seen_count', (SELECT count(*) FROM jsonb_object_keys(coalesce(value->'seen', '{}'::jsonb))))
  FROM (SELECT * FROM service_config WHERE service='nova_attention_focus' ORDER BY updated_at DESC LIMIT 1) f
UNION ALL
SELECT 'core_liveness', checked_at, status, jsonb_build_object('node', node_name, 'error', error_message)
  FROM (SELECT * FROM health_checks WHERE service_name='core_liveness' ORDER BY checked_at DESC LIMIT 1) c
UNION ALL
SELECT 'security_organ', checked_at, status,
       jsonb_build_object('node', node_name, 'newcomers_1h', (SELECT count(*) FROM telemetry.known_devices WHERE first_seen > now()-interval '1 hour'))
  FROM (SELECT * FROM health_checks WHERE service_name='security_organ' ORDER BY checked_at DESC LIMIT 1) s
UNION ALL
SELECT 'presence', last_confirmed, person || '@' || coalesce(room, '?'),
       jsonb_build_object('confidence', confidence, 'entered_at', entered_at, 'activity', activity_state, 'source', source,
                          'degraded_feeds', detail->'degraded_feeds')
  FROM (SELECT * FROM presence_state ORDER BY (person = 'jordan') DESC, last_confirmed DESC NULLS LAST LIMIT 1) p
UNION ALL
SELECT 'embodiment', computed_at, house_state, occupancy
  FROM (SELECT * FROM embodiment_state ORDER BY computed_at DESC LIMIT 1) e
UNION ALL
SELECT 'contact_sense', last_at, mouth, jsonb_build_object('count_24h', count_24h, 'detail', detail)
  FROM (SELECT * FROM contact_sense ORDER BY last_at DESC NULLS LAST LIMIT 1) k
UNION ALL
SELECT 'boiler', ts, CASE WHEN pressure >= threshold THEN 'bleed' WHEN pressure >= 0.7 * threshold THEN 'rising' ELSE 'ok' END,
       jsonb_build_object('pressure', pressure, 'threshold', threshold, 'top', top, 'bled', bled)
  FROM (SELECT * FROM boiler_state ORDER BY ts DESC LIMIT 1) bo
"""

def ensure_view(cur):
    cur.execute(VIEW)

def board(cur):
    """{organ: {'ts':..., 'state':..., 'detail': {...}}} — tolerant: an organ that has never written is absent."""
    try:
        cur.execute("SELECT organ, ts, state, detail FROM organ_board")
        return {o: {"ts": ts, "state": st, "detail": d or {}} for o, ts, st, d in cur.fetchall()}
    except Exception:  # noqa: BLE001
        cur.connection.rollback(); return {}

def someone_just_arrived(b, within_min=10, min_conf=0.3):
    """Pure: (person, minutes_ago) if presence shows a known person entering within `within_min`, else None."""
    p = b.get("presence")
    if not p or not p["detail"].get("entered_at") or (p["detail"].get("confidence") or 0) < min_conf:
        return None
    from datetime import datetime, timezone
    ent = p["detail"]["entered_at"]
    if isinstance(ent, str):
        ent = datetime.fromisoformat(ent)
    mins = (datetime.now(timezone.utc) - ent).total_seconds() / 60
    return (p["state"].split("@")[0], round(mins)) if 0 <= mins <= within_min else None

def _connect(attempts=3):
    """CLI connect with retry (2 s, 4 s); re-raises on the last try."""
    import time
    for i in range(attempts):
        try:
            return psycopg2.connect(OPS, connect_timeout=8)
        except Exception as e:
            if i == attempts - 1:
                raise
            print(f"organ_board: PG connect failed ({e}); retry {i + 1}", file=sys.stderr)
            time.sleep(2 * (i + 1))

def main():
    conn = _connect(); conn.autocommit = True; cur = conn.cursor()
    ensure_view(cur)
    b = board(cur)
    for o, r in sorted(b.items()):
        print(f"{o:16s} {str(r['ts'])[:19]:19s} {str(r['state'])[:28]:28s} {json.dumps(r['detail'], default=str)[:110]}")
    print(f"arrival: {someone_just_arrived(b)}")

def selftest():
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    b = {"presence": {"ts": now, "state": "jordan@office", "detail": {"confidence": 0.8, "entered_at": now - timedelta(minutes=3)}}}
    assert someone_just_arrived(b)[0] == "jordan"
    b["presence"]["detail"]["entered_at"] = now - timedelta(minutes=40)
    assert someone_just_arrived(b) is None
    b["presence"]["detail"].update(entered_at=now - timedelta(minutes=1), confidence=0.1)
    assert someone_just_arrived(b) is None            # a 10% guess is not an arrival
    assert someone_just_arrived({}) is None
    print("selftest ok")

if __name__ == "__main__":
    if "--selftest" in sys.argv: selftest()
    else: main()
