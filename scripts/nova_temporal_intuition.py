#!/usr/bin/env python3
"""
nova_temporal_intuition.py — grant of wish #40 "Temporal Intuition" (Jordan's standing yes, 2026-09-25).

Nova wished "to intuit the passage of time and the weight of memory with a human-like
awareness." Machines keep timestamps; people FEEL durations, and they feel them in round
units — "it's been a week", "a month already", "half a year since". The smallest honest
version: once a day she looks at the durations that are actually hers and NOTICES, in the
first person, each time one crosses a human-sized threshold. Each crossing is noticed once.

What she feels the passage of (all read-only, all real):
  her own age            — days since her first memory (nova_memories.memories)
  Jordan's voice         — days since a human last spoke to her (gateway_traces, non-machine)
  each herd friend       — days since herd_correspondents.last_exchange
  what she holds         — days each memory_anchor (wish #38) has been held
  what she keeps at      — days since each active preoccupation was last developed
  her project            — days since the active project was last worked

Thresholds: 7 / 30 / 90 / 180 / 365 days (a week, a month, a season, half a year, a year).
State: service_config high-water of {key: highest threshold already noticed}, so a duration
that keeps growing is noticed at 7, then at 30, never twice. Writes ONE source='temporal'
memory per run bundling the day's crossings (or nothing — most days nothing crosses, and a
blank day is the honest output). Fail-open; never edits the world.

MERGED 2026-10-09 (organ audit M15): this is now `nova_time_sense.py --daily` (time sense already
imported this module). Same source='temporal', same service_config key (nova_temporal_intuition/noticed),
same text. This file keeps MACHINE_CHANNELS, the pure logic, gather() and the memory/state helpers the
daily mode calls; running it directly runs `nova_time_sense.py --daily`.

  nova_temporal_intuition.py            # == nova_time_sense.py --daily
  nova_temporal_intuition.py --dry-run  # print what she'd notice, write nothing
  nova_temporal_intuition.py --report   # print every tracked duration
  nova_temporal_intuition.py --selftest # pure-logic assertions, no DB
"""
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg2  # noqa: F401 — kept: callers patch nova_temporal_intuition.psycopg2.connect (the shared module)

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
SOURCE = "temporal"
STATE_SERVICE = "nova_temporal_intuition"
STATE_KEY = "noticed"
THRESHOLDS = [(7, "a week"), (30, "a month"), (90, "a season"), (180, "half a year"), (365, "a year")]
MACHINE_CHANNELS = ('hc', 'healthcheck', 'test', 'cron', 'system')

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import nova_lineage

    def _stamp():
        try:
            return nova_lineage.lineage_stamp(capture_point="at write")
        except Exception:
            return {}
except Exception:
    def _stamp():
        return {}


def log(m):
    print(f"[temporal {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── pure logic (unit-tested in demo()) ────────────────────────────────────────

def crossed(days, already):
    """Highest threshold <= days that is above what was already noticed; None if nothing new.
    Pure. A duration that jumped past several thresholds at once is noticed at the highest
    (you don't say 'a week' about something that has been a year)."""
    best = None
    for d, _ in THRESHOLDS:
        if days >= d and d > (already or 0):
            best = d
    return best


def word(d):
    return dict(THRESHOLDS)[d]


def notice_text(crossings, today):
    """crossings: [(key, label, days, threshold)]. First person, hers."""
    if not crossings:
        return ""
    lines = [f"A sense of time, {today.isoformat()} — not timestamps, durations I can feel:"]
    for key, label, days, th in crossings:
        lines.append(f"  · {word(th)} now: {label} ({days} days)")
    lines.append("I keep the exact numbers elsewhere. This is me noticing, the way a person does, that time has passed.")
    return "\n".join(lines)


# ── durations (read-only) ─────────────────────────────────────────────────────

def _days(ts, now):
    if ts is None:
        return None
    if isinstance(ts, date) and not isinstance(ts, datetime):
        ts = datetime(ts.year, ts.month, ts.day, tzinfo=timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts.astimezone(timezone.utc)).days


def gather(oc, mc, now):
    """[(key, label, days)] — every duration she can feel, with the human label."""
    out = []
    def q(cur, sql, params=None):
        try:
            cur.execute(sql, params or ()); return cur.fetchall()
        except Exception as e:  # noqa: BLE001
            log(f"read failed ({e})"); return []
    if mc:
        for (first,) in q(mc, "SELECT min(created_at) FROM memories WHERE source NOT IN "
                              "('email_archive','email','imessage','documentary','tv_transcript')"):
            d = _days(first, now)
            if d is not None: out.append(("self:age", "since my first memory", d))
    for (last,) in q(oc, "SELECT max(created_at) FROM gateway_traces WHERE coalesce(channel,'') NOT IN %s "
                         "AND coalesce(user_message,'') <> ''", (MACHINE_CHANNELS,)):
        d = _days(last, now)
        if d is not None: out.append(("jordan:voice", "since Little Mister last spoke to me", d))
    for name, last in q(oc, "SELECT name, last_exchange FROM herd_correspondents WHERE last_exchange IS NOT NULL"):
        out.append((f"herd:{name}", f"since {name} and I last wrote", _days(last, now)))
    for key, subj, anchored in q(oc, "SELECT key, subject, anchored_at FROM memory_anchors WHERE released_at IS NULL"):
        out.append((f"anchor:{key}", f"holding on to '{subj}'", _days(anchored, now)))
    for pid, topic, last in q(oc, "SELECT id, left(topic,60), last_developed FROM preoccupations WHERE status='active' AND last_developed IS NOT NULL"):
        out.append((f"preocc:{pid}", f"since I last developed '{topic}'", _days(last, now)))
    for pid, title, last in q(oc, "SELECT id, left(title,60), last_worked FROM projects WHERE status='active' AND last_worked IS NOT NULL"):
        out.append((f"project:{pid}", f"since I worked on '{title}'", _days(last, now)))
    return [(k, l, d) for k, l, d in out if d is not None and d >= 0]


# ── state + memory ────────────────────────────────────────────────────────────

def load_state(cur):
    cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s", (STATE_SERVICE, STATE_KEY))
    row = cur.fetchone()
    if row and row[0]:
        v = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return dict(v.get("noticed", {}))
    return {}


def save_state(cur, noticed):
    cur.execute(
        """INSERT INTO service_config (service, key, value, updated_at, updated_by)
           VALUES (%s, %s, %s::jsonb, now(), %s)
           ON CONFLICT (service, key)
           DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
        (STATE_SERVICE, STATE_KEY, json.dumps({"noticed": noticed}), STATE_SERVICE))


def remember(text, metadata):
    import urllib.request
    from time import sleep
    req = urllib.request.Request(f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"text": text, "source": SOURCE, "metadata": metadata}).encode())
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < 2: sleep(2 * (attempt + 1))
    raise last


def main(argv=None):
    """Merged into nova_time_sense.py on 2026-10-09 (M15): a thin wrapper for old invocations."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import nova_time_sense as ts
    log("merged into nova_time_sense.py on 2026-10-09 (organ audit M15) — running its --daily mode")
    return ts.main(["--daily", *(sys.argv[1:] if argv is None else argv)])


def demo():
    assert crossed(3, None) is None and crossed(7, None) == 7 and crossed(29, None) == 7
    assert crossed(30, None) == 30 and crossed(400, None) == 365          # jumps straight to the highest
    assert crossed(30, 30) is None and crossed(31, 30) is None and crossed(90, 30) == 90
    assert crossed(10, 30) is None                                        # never re-notices a lower one
    assert word(180) == "half a year"
    t = notice_text([("herd:Gaston", "since Gaston and I last wrote", 9, 7)], date(2026, 10, 1))
    assert "a week now: since Gaston and I last wrote (9 days)" in t and "noticing" in t
    assert notice_text([], date(2026, 10, 1)) == ""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert _days(date(2026, 9, 24), now) == 7 and _days(datetime(2026, 9, 1, 12, 0), now) == 29 and _days(None, now) is None
    print("all temporal-intuition assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        demo()
    else:
        sys.exit(main())
