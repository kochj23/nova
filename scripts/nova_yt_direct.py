#!/usr/bin/env python3
"""nova_yt_direct.py — YouTube channels Nova ingests WITHOUT Jordan subscribing to them.

Jordan, 2026-10-19: "as long as you are grabbing them, I don't need to sub to them." nova_yt_subs_audio.py
appends these to his subscription list each night (same latest-video audio + local Whisper path, same
yt_ingest_seen bookkeeping), so a section can use a channel he has never followed.

The list lives in PG: nova_ops.belief_sources rows with fetch_mode = 'direct' and active (scripts/sql/
belief_sources.sql; how-to in agent_docs 'belief-sources'). Add or retire a channel there, not here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_dsn as _nova_dsn  # noqa: E402

DSN = _nova_dsn.pg_dsn("nova_ops")


def direct_channels(cur=None):
    """[(channel_id, handle, name)] of every active direct channel. Raises if PG is unreachable."""
    if cur is None:
        import psycopg2
        conn = psycopg2.connect(DSN, connect_timeout=10)
        cur = conn.cursor()
    cur.execute("SELECT channel_id, handle, name FROM belief_sources WHERE fetch_mode = 'direct' AND active "
                "ORDER BY name")
    return [tuple(r) for r in cur.fetchall()]


def as_subscriptions(subs=(), cur=None, log=print):
    """Direct channels as (channel_id, handle, name) subscription tuples, minus any he already follows.
    PG unreachable: logs an ERROR and returns [] — the nightly ingest of his own subscriptions still runs."""
    try:
        direct = direct_channels(cur)
    except Exception as e:  # noqa: BLE001
        log(f"ERROR direct channel list unreadable ({e}) — ingesting subscriptions only tonight")
        return []
    have = {s[0] for s in subs}
    return [c for c in direct if c[0] not in have]


if __name__ == "__main__":
    rows = direct_channels()
    ids = [c[0] for c in rows]
    assert len(ids) == len(set(ids)), "duplicate channel id"
    assert all(i.startswith("UC") and len(i) == 24 for i in ids), "malformed channel id"
    assert rows and as_subscriptions([(ids[0], "", "")]) and ids[0] not in [c[0] for c in as_subscriptions([(ids[0], "", "")])]
    print(f"{len(rows)} direct channels OK")
