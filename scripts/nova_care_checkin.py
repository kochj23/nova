#!/usr/bin/env python3
"""
nova_care_checkin.py — Baymax-style "are you satisfied with your care?" (weekly, Sunday evening).

--ask      posts ONE #nova-chat message to Jordan listing 3-4 concrete things she did to him this
           week (top alerts sent, articles published, direct reach-outs) with a one-line reply
           format ("1 yes 2 no 3 meh"). Recorded in nova_ops.nova_care_checkins (one row per week).
(default)  harvests replies: reads the thread of every unanswered check-in from the last 14 days
           (nova_slack_answers.read_answer — Jordan-only), stores reply + parsed verdicts, and saves
           a summary to vector memory (source 'jordan_feedback') so she learns what helps.

  nova_care_checkin.py --ask [--dry-run]
  nova_care_checkin.py [--dry-run]
  nova_care_checkin.py --selftest

Written by Jordan Koch.
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
CHANNEL = "C0AMNQ5GX70"   # #nova-chat (same as nova_config.SLACK_CHAN / nova_slack_answers.CHANNEL)
MAX_ITEMS = 4

SCHEMA = """CREATE TABLE IF NOT EXISTS nova_care_checkins (
    id bigserial PRIMARY KEY,
    week date NOT NULL UNIQUE,
    items jsonb NOT NULL,
    message text,
    asked_at timestamptz,
    slack_channel text,
    slack_ts text,
    reply text,
    reply_at timestamptz,
    verdicts jsonb,
    memory_id text)"""

_VERDICT_WORDS = {"yes": "helped", "y": "helped", "helped": "helped", "good": "helped", "+": "helped",
                  "no": "noise", "n": "noise", "noise": "noise", "useless": "noise", "-": "noise",
                  "meh": "meh", "m": "meh", "eh": "meh"}
_REPLY_RE = re.compile(r"(\d)\s*[:.)=-]?\s*(yes|y|no|n|helped|good|noise|useless|meh|eh|m|\+|-)(?![a-z])", re.I)


def log(m):
    print(f"[care-checkin {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def week_of(d=None) -> date:
    """The Sunday that closes the week containing d (Sunday itself maps to itself)."""
    d = d or date.today()
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _clean(t, n=90):
    t = re.sub(r"^[^\w\"'(\[]+", "", (t or "").strip())          # drop leading emoji/symbols
    t = re.sub(r"\s+", " ", t)
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


# ── gathering (all parameterized, read-only) ─────────────────────────────────

def gather(ops_cur, mem_cur):
    items = []
    ops_cur.execute("""SELECT title, level, count(*) FROM telemetry.events
                       WHERE ts > now() - interval '7 days' AND status = 'sent' AND level IN ('critical','warning')
                       GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 2""")
    for title, level, n in ops_cur.fetchall():
        items.append({"kind": "alert", "text": f"{level} alert \"{_clean(title)}\" (sent {n}x)"})
    try:
        mem_cur.execute("""SELECT metadata->>'section', metadata->>'title' FROM memories
                           WHERE source = %s AND created_at > now() - interval '7 days'
                             AND metadata->>'section' IS NOT NULL AND metadata->>'title' IS NOT NULL
                           ORDER BY created_at DESC""", ("nova_articles",))
        arts = mem_cur.fetchall()
    except Exception:  # noqa: BLE001
        arts = []
    if arts:
        by = {}
        for sec, _ in arts:
            by[sec or "other"] = by.get(sec or "other", 0) + 1
        top = ", ".join(f"{s} {c}" for s, c in sorted(by.items(), key=lambda x: -x[1])[:3])
        items.append({"kind": "articles", "text": f"{len(arts)} articles published ({top}), "
                                                  f"latest \"{_clean(arts[0][1], 70)}\""})
    ops_cur.execute("""SELECT topic, message FROM reach_log WHERE lower(audience) = 'jordan'
                       AND status = 'sent' AND ts > now() - interval '7 days' ORDER BY ts DESC LIMIT 1""")
    r = ops_cur.fetchone()
    if r:
        items.append({"kind": "reach", "text": f"reach-out{(' on ' + r[0]) if r[0] else ''}: \"{_clean(r[1], 80)}\""})
    return items[:MAX_ITEMS]


def compose(items, week) -> str:
    lines = [f"Little Mister, Sunday care check for the week ending {week.strftime('%b %-d')}. "
             "I'm contractually obligated to ask whether you are satisfied with your care, so here's "
             "what I actually did to you this week:"]
    lines += [f"{i}. {it['text']}" for i, it in enumerate(items, 1)]
    lines.append("Reply in this thread like `1 yes 2 no 3 meh` (yes = helped, no = noise, meh = shrug). "
                 "Add a sentence if something should change — I'll remember it, which is more than "
                 "most of your monitoring can say.")
    return "\n".join(lines)


def parse_reply(text, n_items):
    """-> {item_index(str): 'helped'|'noise'|'meh'}; a bare yes/no applies to every item."""
    out = {}
    for num, word in _REPLY_RE.findall(text or ""):
        if 1 <= int(num) <= n_items:
            out[num] = _VERDICT_WORDS[word.lower()]
    if not out:
        m = re.match(r"^\s*(yes|no|meh|helped|noise)\b", text or "", re.I)
        if m:
            out = {str(i): _VERDICT_WORDS[m.group(1).lower()] for i in range(1, n_items + 1)}
    return out


def summarize(week, items, reply, verdicts) -> str:
    parts = [f"{it['text']} -> {verdicts.get(str(i), 'no verdict')}" for i, it in enumerate(items, 1)]
    return (f"Jordan's weekly care check-in feedback (week ending {week}): " + "; ".join(parts)
            + f". His words: \"{reply.strip()[:500]}\"")


def remember(text, metadata):
    """Store to vector memory; returns id or None (never raises)."""
    body = json.dumps({"text": text, "source": "jordan_feedback", "metadata": metadata}).encode()
    try:
        req = urllib.request.Request(MEMSRV + "/remember", method="POST", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("id")
    except Exception as e:  # noqa: BLE001
        log(f"remember failed: {e}")
        return None


# ── runs ─────────────────────────────────────────────────────────────────────

def _connect(dsn):
    import psycopg2
    c = psycopg2.connect(dsn, connect_timeout=5)
    c.autocommit = True
    return c


def ask(dry=False):
    week = week_of()
    ops = _connect(OPS_DSN)
    oc = ops.cursor()
    if not dry:
        oc.execute(SCHEMA)
        oc.execute("SELECT 1 FROM nova_care_checkins WHERE week = %s", (week,))
        if oc.fetchone():
            log(f"already asked for week {week}"); return 0
    try:
        mem = _connect(MEM_DSN)
        items = gather(oc, mem.cursor())
    except Exception as e:  # noqa: BLE001 — memories DB down: alerts/reaches still work
        log(f"memories db unavailable ({e}); gathering without articles")
        items = gather(oc, _NullCursor())
    if not items:
        log("nothing happened this week — no check-in"); return 0
    msg = compose(items, week)
    if dry:
        print(msg); return 0
    import nova_slack_answers as nsa
    res = nsa.slack("chat.postMessage", channel=CHANNEL, text=msg)
    if not res.get("ok"):
        log(f"post failed: {res.get('error')}"); return 1
    oc.execute("INSERT INTO nova_care_checkins (week, items, message, asked_at, slack_channel, slack_ts) "
               "VALUES (%s, %s::jsonb, %s, now(), %s, %s) ON CONFLICT (week) DO NOTHING",
               (week, json.dumps(items), msg, CHANNEL, res.get("ts")))
    log(f"asked for week {week} ({len(items)} items)")
    return 0


class _NullCursor:
    def execute(self, *a, **k): raise RuntimeError("no db")
    def fetchall(self): return []


def harvest(dry=False):
    import nova_slack_answers as nsa
    ops = _connect(OPS_DSN)
    oc = ops.cursor()
    oc.execute(SCHEMA)
    oc.execute("SELECT id, week, items, slack_channel, slack_ts FROM nova_care_checkins "
               "WHERE reply IS NULL AND slack_ts IS NOT NULL AND asked_at > now() - interval '14 days'")
    n = 0
    for cid, week, items, chan, ts in oc.fetchall():
        text, _ = nsa.read_answer(chan, ts)
        if not text:
            continue
        verdicts = parse_reply(text, len(items))
        summary = summarize(week, items, text, verdicts)
        if dry:
            print(summary); continue
        mid = remember(summary, {"type": "care_checkin", "week": str(week), "verdicts": verdicts})
        oc.execute("UPDATE nova_care_checkins SET reply = %s, reply_at = now(), verdicts = %s::jsonb, "
                   "memory_id = %s WHERE id = %s", (text, json.dumps(verdicts), str(mid) if mid else None, cid))
        n += 1
    log(f"harvested {n} reply(ies)")
    return 0


def selftest():
    assert week_of(date(2026, 10, 11)) == date(2026, 10, 11)       # Sunday
    assert week_of(date(2026, 10, 8)) == date(2026, 10, 4)         # Thursday -> previous Sunday
    assert parse_reply("1 yes 2 no 3 meh", 3) == {"1": "helped", "2": "noise", "3": "meh"}
    assert parse_reply("yes all good", 2) == {"1": "helped", "2": "helped"}
    assert parse_reply("9 yes", 3) == {}
    items = [{"kind": "alert", "text": "a"}, {"kind": "reach", "text": "b"}]
    m = compose(items, date(2026, 10, 11))
    assert "1. a" in m and "2. b" in m and "1 yes 2 no" in m
    assert "-> helped" in summarize(date(2026, 10, 11), items, "1 yes", {"1": "helped"})
    print("selftest ok")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Weekly Baymax-style care check-in")
    ap.add_argument("--ask", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    return ask(a.dry_run) if a.ask else harvest(a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
