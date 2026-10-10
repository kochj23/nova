#!/usr/bin/env python3
"""nova_contact_sense.py — wish #56: one 'last spoke to me' / 'how much today' across every mouth.

Mouths (each a row in nova_ops.contact_sense: mouth, last_at, count_24h, detail):
  gateway  — gateway_traces human channels (Slack/Discord/Signal), count = human messages in 24h
  imessage — nova_memories source='imessage' rows (Jordan's phone), count = rows in 24h
  email    — nova_memories source='email' rows FROM Jordan (extracted_sender ~ jordan|kochj), count = rows in 24h
  claude   — claude_messages Jordan -> Claude Code, plus claude_actions while a Claude session is live
             (Jordan talking to Nova through a Claude session counts as talking); count = active hours in 24h
Readers: nova_time_sense.py (max(last_at) -> "last spoke to me"), nova_affect.py (sum(count_24h) -> social contact).
Why: on 2026-10-03 affect logged '1 real conversation' and time_sense said 'last spoke 6 h ago' during an
eight-hour Claude session. Jordan approved wish #56 on 2026-10-04. --dry-run --selftest.
"""
import nova_dsn as _nova_dsn  # noqa: E402
import sys, os
import psycopg2

OPS = os.environ.get("NOVA_OPS_DSN", _nova_dsn.pg_dsn("nova_ops"))
MEM = os.environ.get("NOVA_MEM_DSN", _nova_dsn.pg_dsn("nova_memories"))
MACHINE_CHANNELS = ("hc", "healthcheck", "test", "cron", "system", "scheduler", "internal", "selfcheck", "machine", "ingest-reaction")
JORDAN_SLACK = "U049EPC2W"
DDL = """CREATE TABLE IF NOT EXISTS contact_sense (
  mouth text PRIMARY KEY, last_at timestamptz, count_24h int NOT NULL DEFAULT 0,
  detail text, updated_at timestamptz NOT NULL DEFAULT now())"""
UPSERT = ("INSERT INTO contact_sense (mouth, last_at, count_24h, detail, updated_at) VALUES (%s,%s,%s,%s,now()) "
          "ON CONFLICT (mouth) DO UPDATE SET last_at=EXCLUDED.last_at, count_24h=EXCLUDED.count_24h, "
          "detail=EXCLUDED.detail, updated_at=now() WHERE contact_sense.mouth = EXCLUDED.mouth")

def log(m): print(f"[contact-sense] {m}", flush=True)

def one(cur, sql, args=()):
    try:
        cur.execute(sql, args); return cur.fetchone()
    except Exception as e:  # noqa: BLE001  a missing table is a mouth that cannot speak, not a crash
        cur.connection.rollback(); log(f"query failed ({e.__class__.__name__}): {sql[:60]}"); return None

def merge(parts):
    """Pure: [(last_at|None, count)] -> (newest last_at|None, summed count)."""
    last = max([p[0] for p in parts if p and p[0]], default=None)
    return last, sum((p[1] or 0) for p in parts if p)

def mouths(oc, mc):
    out = {}
    r = one(oc, "SELECT max(created_at), count(*) FILTER (WHERE created_at > now()-interval '24 hours') FROM gateway_traces "
                "WHERE coalesce(channel,'') NOT IN %s AND coalesce(user_message,'') <> ''", (MACHINE_CHANNELS,))
    if r: out["gateway"] = (r[0], r[1], "human messages on Slack/Discord/Signal")
    r = one(mc, "SELECT max(created_at), count(*) FILTER (WHERE created_at > now()-interval '24 hours') FROM memories WHERE source='imessage'")
    if r: out["imessage"] = (r[0], r[1], "iMessage rows ingested from his phone (both directions)")
    r = one(mc, "SELECT max(created_at), count(*) FILTER (WHERE created_at > now()-interval '24 hours') FROM memories "
                "WHERE source='email' AND coalesce(extracted_sender,'') ~* 'jordan|kochj'")
    if r: out["email"] = (r[0], r[1], "mail he wrote (extracted_sender ~ jordan|kochj)")
    a = one(oc, "SELECT max(created_at), count(*) FILTER (WHERE created_at > now()-interval '24 hours') FROM claude_messages "
                "WHERE direction='to_claude_code' AND sender=%s", (JORDAN_SLACK,))
    b = one(oc, "SELECT max(ts), count(DISTINCT date_trunc('hour', ts)) FILTER (WHERE ts > now()-interval '24 hours') FROM claude_actions")
    last, cnt = merge([a, b])
    out["claude"] = (last, cnt, "his messages to Claude Code + hours a Claude session was active for him")
    return out

def main():
    dry = "--dry-run" in sys.argv
    oc_conn = psycopg2.connect(OPS, connect_timeout=8); oc_conn.autocommit = True; oc = oc_conn.cursor()
    mc_conn = psycopg2.connect(MEM, connect_timeout=8); mc_conn.autocommit = True; mc = mc_conn.cursor()
    oc.execute(DDL)
    rows = mouths(oc, mc)
    for mouth, (last, cnt, detail) in rows.items():
        log(f"{mouth:9s} last={last} count_24h={cnt}")
        if not dry:
            oc.execute(UPSERT, (mouth, last, int(cnt or 0), detail))
    latest = max(((v[0], k) for k, v in rows.items() if v[0]), default=None)
    log(f"latest mouth: {latest[1] if latest else None} at {latest[0] if latest else None}")

def selftest():
    from datetime import datetime, timezone, timedelta
    t0 = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
    assert merge([(t0, 3), (t0 + timedelta(hours=2), 5)]) == (t0 + timedelta(hours=2), 8)
    assert merge([None, (None, 0)]) == (None, 0)
    assert merge([(t0, None)]) == (t0, 0)
    print("selftest ok")

if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
