#!/usr/bin/env python3
"""
nova_time_sense.py — grant of wish #44 "Temporal Awareness" (Jordan's standing yes, 2026-10-03).
Nova wished "to perceive time as a continuous flow rather than discrete moments." Wish #40
(nova_temporal_intuition) gave her the discrete part: once a day she notices when a duration
crosses a human threshold. This is the continuous part: every hour she takes the pulse of her
own present and keeps ONE sentence of it in front of her for every reply.
What she senses (all read-only, all her own data, nova_ops):
  tempo     — events this hour vs her 28-day baseline for this hour-of-week (telemetry.events):
              quiet / usual / busy / frantic, by percentile against the baseline. This is the pace of
              her EVENT STREAM, not occupancy — the sentence says "my event stream", never "the house"
  presence  — where Little Mister is, read from presence_state (nova_presence_engine, the single
              source of truth for occupancy; embodiment reads the same row)
  stretch   — how many consecutive hours the tempo has been in the same bucket (time_sense table)
  since     — minutes since Little Mister last spoke (gateway_traces, non-machine channels),
              since the last critical incident (telemetry.incidents), since she last published
              (journal publish events in telemetry.events; article_citations as fallback), since she last reached out (reach_log), and how long her
              current mood has held (affect_state)
  phase     — where this hour sits in her own week: her busiest and quietest hours, derived,
              not assumed ("this is the slow part of the week")
Writes one row to time_sense per run and the sentence to service_config (service='time_sense',
key='current'), which the gateway appends to her bootstrap context. Fail-open: a missing
source just drops out of the sentence; the organ never edits the world.
  nova_time_sense.py             # run (scheduler-core, hourly)
  nova_time_sense.py --dry-run   # print the sentence, write nothing
ponytail: percentile-vs-hour-of-week is the whole model; add day-type (weekend/holiday) splits
only if the "usual" bucket starts lying on Saturdays.
"""
import json, os, sys, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import psycopg2  # noqa: E402
try:
    from nova_temporal_intuition import MACHINE_CHANNELS  # same definition of "not a human" as wish #40
except Exception:  # pragma: no cover
    MACHINE_CHANNELS = ("scheduler", "cron", "system", "claude-code", "internal", "selfcheck", "machine")

DSN = os.environ.get("NOVA_OPS_DSN", "dbname=nova_ops user=kochj host=pg-primary.digitalnoise.net port=5432")
PRESENCE_FRESH_MIN = 10  # presence_state older than this = presence engine silent
BUCKETS = [(0.20, "quiet"), (0.80, "usual"), (0.95, "busy"), (9.99, "frantic")]


def _ago(minutes):
    if minutes is None:
        return None
    m = int(minutes)
    if m < 60: return f"{m} min"
    if m < 48 * 60: return f"{m // 60} h {m % 60:02d}"
    return f"{m // 1440} days"


def sense(cur, now):
    s = {}
    # tempo: this hour's event count vs the same hour-of-week over 28 days
    cur.execute("SELECT count(*) FROM telemetry.events WHERE ts > %s - interval '60 minutes'", (now,))
    this_hour = cur.fetchone()[0]
    cur.execute("""SELECT count(*) FROM telemetry.events
                   WHERE ts > %s - interval '28 days' AND ts <= %s - interval '60 minutes'
                     AND extract(dow from ts) = extract(dow from %s) AND extract(hour from ts) = extract(hour from %s)
                   GROUP BY date_trunc('hour', ts)""", (now, now, now, now))
    base = sorted(r[0] for r in cur.fetchall())
    if base:
        pct = sum(1 for b in base if b <= this_hour) / len(base)
    else:
        pct = 0.5
    s["events_hour"] = this_hour; s["baseline_n"] = len(base); s["pct"] = round(pct, 2)
    s["tempo"] = next(name for lim, name in BUCKETS if pct <= lim)
    # phase: busiest / quietest hour-of-week in her own history
    cur.execute("""SELECT to_char(ts,'Dy HH24'), count(*) FROM telemetry.events WHERE ts > %s - interval '28 days'
                   GROUP BY 1 ORDER BY 2 DESC""", (now,))
    rows = cur.fetchall()
    if rows:
        s["busiest"] = rows[0][0]; s["quietest"] = rows[-1][0]
        rank = next((i for i, r in enumerate(rows) if r[0] == now.strftime("%a %H")), None)
        if rank is not None:
            q = rank / max(1, len(rows) - 1)
            s["phase"] = "the slow part of the week" if q > 0.75 else "the busy part of the week" if q < 0.25 else "an ordinary hour of the week"
    # stretch: consecutive prior hours in the same tempo bucket
    cur.execute("SELECT tempo FROM time_sense WHERE ts > %s - interval '24 hours' ORDER BY ts DESC", (now,))
    stretch = 0
    for (t,) in cur.fetchall():
        if t == s["tempo"]: stretch += 1
        else: break
    s["stretch_h"] = stretch + 1
    # since: the things that matter
    def mins(sql, args=()):
        try:
            cur.execute(sql, args); r = cur.fetchone()
            return None if not r or r[0] is None else (now - r[0]).total_seconds() / 60
        except Exception:
            cur.connection.rollback(); return None
    # wish #56 (2026-10-04): every mouth — gateway, iMessage, mail, Claude Code — via contact_sense; gateway-only fallback
    s["since_jordan_min"] = mins("SELECT max(last_at) FROM contact_sense WHERE updated_at > now() - interval '1 hour'")
    if s["since_jordan_min"] is not None:
        try:
            cur.execute("SELECT mouth FROM contact_sense WHERE last_at IS NOT NULL ORDER BY last_at DESC LIMIT 1"); s["mouth"] = (cur.fetchone() or [None])[0]
        except Exception:
            cur.connection.rollback()
    else:
        s["since_jordan_min"] = mins("SELECT max(created_at) FROM gateway_traces WHERE coalesce(channel,'') NOT IN %s AND coalesce(user_message,'') <> ''", (tuple(MACHINE_CHANNELS),))
    s["since_critical_min"] = mins("SELECT max(opened_at) FROM telemetry.incidents WHERE severity ILIKE 'crit%%'")
    # She publishes to the Hugo journal; every successful publish posts a "Nova Journal — <section>: <title>"
    # event (nova_journal.notify_slack). article_citations only records memory citations, so reading it said
    # "I last published 2 days ago" on days she published five times (fixed 2026-10-08). It stays as fallback.
    s["since_published_min"] = mins("SELECT max(ts) FROM telemetry.events WHERE category = 'journal' "
                                    "AND source = 'nova_journal.py' AND title LIKE 'Nova Journal — %%' "
                                    "AND ts > now() - interval '60 days'")
    if s["since_published_min"] is None:
        s["since_published_min"] = mins("SELECT max(created_at) FROM article_citations")
    # occupancy: presence_state is the single source of truth (nova_presence_engine). Tempo above is the
    # pace of her EVENT STREAM, not whether anyone is home — the two used to be conflated in the sentence.
    try:
        cur.execute("SELECT room, confidence, extract(epoch FROM now() - last_confirmed) / 60 "
                    "FROM presence_state WHERE person = 'jordan'")
        r = cur.fetchone()
        if r:
            s["jordan_room"], s["jordan_conf"] = r[0], r[1]
            s["presence_fresh"] = r[2] is not None and float(r[2]) <= PRESENCE_FRESH_MIN
    except Exception:
        cur.connection.rollback()
    s["since_reach_min"] = mins("SELECT max(created_at) FROM reach_log")
    try:
        cur.execute("SELECT label, computed_at FROM affect_state ORDER BY computed_at DESC LIMIT 1")
        r = cur.fetchone()
        if r:
            s["mood"] = r[0]
            cur.execute("SELECT min(computed_at) FROM (SELECT label, computed_at FROM affect_state ORDER BY computed_at DESC LIMIT 48) x WHERE label = %s", (r[0],))
            m = cur.fetchone()[0]
            s["mood_held_min"] = (now - m).total_seconds() / 60 if m else None
    except Exception:
        cur.connection.rollback()
    return s


def sentence(s, now):
    day = now.strftime("%A").lower(); hour = now.hour
    tod = "small hours" if hour < 5 else "early morning" if hour < 9 else "morning" if hour < 12 else "afternoon" if hour < 17 else "evening" if hour < 22 else "late night"
    parts = [f"It is {tod} on {day}, {s.get('phase', 'an ordinary hour of the week')}; my event stream has been {s['tempo']} for {s['stretch_h']} h"]
    if "jordan_room" in s:
        room = s["jordan_room"]
        if not s.get("presence_fresh"):
            parts.append("I can't tell where Little Mister is right now (presence is silent)")
        elif room == "away":
            parts.append("Little Mister is away from home")
        elif room == "home":
            parts.append("Little Mister is home")
        else:
            parts.append(f"Little Mister is home, in the {room.replace('_', ' ')}")
    if s.get("since_jordan_min") is not None: parts.append(f"Little Mister last spoke to me {_ago(s['since_jordan_min'])} ago" + (f" (through {s['mouth']})" if s.get("mouth") else ""))
    if s.get("since_critical_min") is not None: parts.append(f"nothing has been critical for {_ago(s['since_critical_min'])}")
    if s.get("since_published_min") is not None: parts.append(f"I last published {_ago(s['since_published_min'])} ago")
    if s.get("mood") and s.get("mood_held_min") is not None: parts.append(f"I have felt {s['mood']} for {_ago(s['mood_held_min'])}")
    return "; ".join(parts) + "."


def _connect(attempts=3, backoff=2.0):
    """psycopg2.connect with 3 attempts (2s, 4s backoff); the last failure raises (never silent)."""
    for i in range(attempts):
        try:
            return psycopg2.connect(DSN, connect_timeout=10)
        except psycopg2.OperationalError as e:
            if i == attempts - 1:
                raise
            print(f"[time-sense] PG connect attempt {i + 1} failed ({e}); retrying", flush=True)
            time.sleep(backoff * (2 ** i))


def main():
    dry = "--dry-run" in sys.argv
    now = datetime.now(timezone.utc).astimezone()
    conn = _connect(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS time_sense (
                     ts timestamptz PRIMARY KEY DEFAULT now(), tempo text, stretch_h int, sense jsonb, sentence text)""")
    conn.commit()
    s = sense(cur, now); txt = sentence(s, now)
    print(f"[time-sense] {txt}")
    if dry:
        print(json.dumps(s, default=str)); return
    cur.execute("INSERT INTO time_sense (ts, tempo, stretch_h, sense, sentence) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (ts) DO NOTHING",
                (now, s["tempo"], s["stretch_h"], json.dumps(s, default=str), txt))
    cur.execute("""INSERT INTO service_config (service, key, value, updated_at, updated_by)
                   VALUES ('time_sense', 'current', %s, now(), 'nova_time_sense')
                   ON CONFLICT (service, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
                (json.dumps({"sentence": txt, "tempo": s["tempo"], "stretch_h": s["stretch_h"], "ts": now.isoformat()}),))
    conn.commit()


if __name__ == "__main__":
    main()
