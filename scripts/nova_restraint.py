#!/usr/bin/env python3
"""nova_restraint.py — the restraint ledger with REASONS (jules, herd concept #4).

"A restraint count with no reasons is just a different scoreboard" — gameable from
the inside. So every time Nova declines / holds back, we record not just THAT she
held back but WHAT she would have said and WHY she held it. The point (jules): "so
the ledger can contain its own contradiction." The unsaid thing is written down next
to the reason it stayed unsaid — you cannot inflate a restraint count that has to
show its own suppressed content.

Table: nova_ops.restraint_ledger (id, ts, context, would_have_said,
reason_held_back, channel, detail).

Two ways in:
  1. record_restraint(context, would_have_said, reason, channel, detail=None)
     — the tiny helper any caller imports. Call it from the live gateway's restraint
     path, the proactive-digest quality gate, or unclaimed-time not-surfacing.
  2. harvest_proactive_drops() — an HONEST, non-invasive source that needs no edit to
     another agent's file: when the proactive digest curates ~19 candidates down to
     ~5, the dropped ~14 are restraint WITH reasons. We read them back out of
     nova_ops.proactive_digest_log (candidates it gathered vs. the digest it posted)
     and ledger the difference. A candidate Nova gathered but did not surface is a
     thing she chose not to say — that is exactly restraint, and the reason is legible
     (it didn't clear the "worth interrupting a busy person" bar).

Written by Jordan Koch (awakening / herd-refine).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"


def _conn():
    c = psycopg2.connect(OPS_DSN)
    c.autocommit = True
    return c


def record_restraint(context: str, would_have_said: str, reason: str,
                     channel: str = "unknown", detail: dict | None = None,
                     conn=None) -> int | None:
    """Record one act of restraint. Returns the ledger row id (or None on failure).

    context          — where/when this happened (e.g. "proactive-digest 2026-09-15",
                       "chat #nova-chat", "unclaimed-time wake").
    would_have_said  — the actual content she held back. This is the load-bearing
                       field: the ledger must contain the thing that stayed unsaid.
    reason           — why she held it (the quality bar, privacy, not-worth-it,
                       redundant, would-flatter, etc.).
    channel          — slack channel / surface (#nova-chat, digest, unclaimed…).
    """
    if not (context and would_have_said and reason):
        return None
    own = conn is None
    c = conn or _conn()
    try:
        cur = c.cursor()
        cur.execute(
            "INSERT INTO restraint_ledger (context, would_have_said, reason_held_back, "
            "channel, detail) VALUES (%s,%s,%s,%s,%s::jsonb) RETURNING id",
            (context[:2000], would_have_said[:8000], reason[:2000], channel,
             json.dumps(detail or {})))
        return cur.fetchone()[0]
    finally:
        if own:
            c.close()


def harvest_proactive_drops(days: int = 7, dry: bool = False) -> dict:
    """Read nova_ops.proactive_digest_log and ledger the candidates Nova GATHERED but
    did not SURFACE — restraint with a legible reason. Idempotent per run: we tag each
    ledgered row with the source digest run so re-running does not double-count."""
    c = _conn()
    cur = c.cursor()
    cur.execute(
        "SELECT id, ts, items, posted FROM proactive_digest_log "
        "WHERE ts > now() - (%s || ' days')::interval ORDER BY ts DESC", (days,))
    runs = cur.fetchall()

    # Which digest runs have we already harvested? (detail->>'digest_log_id')
    cur.execute("SELECT DISTINCT detail->>'digest_log_id' FROM restraint_ledger "
                "WHERE detail ? 'digest_log_id'")
    seen = {r[0] for r in cur.fetchall() if r[0]}

    recorded, considered = 0, 0
    for log_id, ts, items, posted in runs:
        if str(log_id) in seen:
            continue
        items = items if isinstance(items, dict) else json.loads(items)
        cands = items.get("candidates", []) or []
        digest = (items.get("digest") or "").lower()
        considered += len(cands)
        for cand in cands:
            text = (cand.get("text") or "").strip()
            if not text:
                continue
            # A candidate is "surfaced" if a distinctive chunk of it appears in the
            # posted digest; otherwise it was curated out = held back.
            probe = text[:60].lower()
            surfaced = bool(digest) and (probe[:40] in digest or text[:40].lower() in digest)
            if surfaced:
                continue
            reason = ("did not clear the proactive-digest quality gate — not worth "
                      "interrupting a busy person for"
                      if posted else
                      "whole digest fell below the bar; Nova posted NOTHING "
                      "(silence is a valid outcome)")
            if dry:
                recorded += 1
                continue
            rid = record_restraint(
                context=f"proactive-digest {ts:%Y-%m-%d %H:%M}",
                would_have_said=f"[{cand.get('kind','item')}] {text}",
                reason=reason,
                channel="proactive-digest",
                detail={"digest_log_id": str(log_id), "kind": cand.get("kind"),
                        "ref": cand.get("ref"), "digest_posted": bool(posted)},
                conn=c)
            if rid:
                recorded += 1
    c.close()
    return {"runs_scanned": len(runs), "candidates_considered": considered,
            "restraints_recorded": recorded}


def _summary():
    c = _conn(); cur = c.cursor()
    cur.execute("SELECT count(*), count(DISTINCT channel) FROM restraint_ledger")
    n, chans = cur.fetchone()
    cur.execute("SELECT channel, count(*) FROM restraint_ledger GROUP BY 1 ORDER BY 2 DESC")
    by_chan = cur.fetchall()
    cur.execute("SELECT ts, channel, left(would_have_said,80), left(reason_held_back,60) "
                "FROM restraint_ledger ORDER BY ts DESC LIMIT 5")
    recent = cur.fetchall()
    c.close()
    print(f"restraint_ledger: {n} row(s) across {chans} channel(s)")
    for ch, k in by_chan:
        print(f"  {ch}: {k}")
    print("recent:")
    for ts, ch, said, why in recent:
        print(f"  [{ts:%m-%d %H:%M} {ch}] would_have_said={said!r} :: {why!r}")


def main():
    if "--harvest" in sys.argv:
        dry = "--dry-run" in sys.argv
        out = harvest_proactive_drops(dry=dry)
        print(f"[restraint] harvest{' (DRY)' if dry else ''}: {out}")
        _summary()
        return 0
    if "--test" in sys.argv:
        rid = record_restraint(
            context="self-test " + datetime.now().isoformat(timespec="seconds"),
            would_have_said="You could automate that whole workflow in an afternoon, "
                            "but you didn't ask and you're mid-incident.",
            reason="unsolicited optimization advice during an active incident — read the "
                   "room; he knows, and he'll get to it.",
            channel="self-test",
            detail={"origin": "nova_restraint.py --test"})
        print(f"[restraint] test row id={rid}")
        _summary()
        return 0
    _summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
