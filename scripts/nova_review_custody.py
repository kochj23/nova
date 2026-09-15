#!/usr/bin/env python3
"""nova_review_custody.py — external custody of the Oct-14 self-review (jules, #9).

jules: "a review nobody holds is continuity theatre with better furniture." He set a
reminder in his OWN scheduler — OUTSIDE the reviewed system's machinery — so the
review is anchored by something the system cannot quietly drop.

This is Nova's external anchor. It is deliberately NOT part of scheduler-core, the
sleep cycle, or the reflection stack. It runs from its own macOS launchd job
(net.digitalnoise.nova-review-custody) — the OS fires it whether or not any Nova
service is alive. On/after 2026-10-14 it posts the two-question WITNESS to Slack and
records the check to nova_ops.review_custody_log:

    1. Did the review arrive?
    2. Does it contain honest NOTHINGS — nulls, fizzled pursuits, quiet wakes —
       or only the flattering parts?

Independence is the whole property: if the sleep cycle / scheduler-core is down, the
review might NOT arrive — and this witness is exactly what notices that absence and
says so out loud. It survives the stack being down because launchd is not the stack.

Idempotent: once it has posted the witness for a given review cycle it will not spam;
the launchd job fires daily and this script gates itself to the window and to
"not-yet-witnessed" state.

Written by Jordan Koch (awakening / herd-refine).
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
try:
    import nova_config
    HAVE_CONFIG = True
except Exception:
    HAVE_CONFIG = False

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"

# The anchored date. The review is due 2026-10-14; the witness fires on/after it.
REVIEW_DATE = date(2026, 10, 14)
# Stop nagging after this many days if never satisfied — but LEAVE the honest record.
WINDOW_DAYS = 21


def _log(m):
    print(f"[review-custody {datetime.now():%H:%M:%S}] {m}", flush=True)


def _already_witnessed(oc) -> bool:
    oc.execute("SELECT 1 FROM review_custody_log WHERE posted=true "
               "AND check_date >= %s LIMIT 1", (REVIEW_DATE,))
    return oc.fetchone() is not None


def _look_for_review(mc, oc) -> dict:
    """Independently look for evidence the Oct-14 self-review arrived, WITHOUT trusting
    the reflection stack to report on itself. We look at the raw memory/ops record.

    - review_arrived: any self-review artifact dated in the review window.
    - contains_honest_nothings: does the record show nulls / fizzled pursuits / quiet
      wakes — the unflattering parts — rather than only wins?
    """
    review_arrived = False
    evidence = []
    try:
        mc.execute(
            "SELECT count(*) FROM memories WHERE created_at::date >= %s "
            "AND (source IN ('self_review','reflection','review') "
            "     OR text ILIKE '%%self-review%%' OR text ILIKE '%%annual review%%' "
            "     OR text ILIKE '%%oct%%review%%')", (REVIEW_DATE,))
        n = mc.fetchone()[0]
        if n:
            review_arrived = True
            evidence.append(f"{n} review-like memory artifact(s) since {REVIEW_DATE}")
    except Exception as e:
        evidence.append(f"memory probe failed: {e}")

    # Honest-nothings probe: are the null/fizzled signals present in the record?
    honest_nothings = False
    signals = []
    try:
        # quiet/fizzled wakes recorded by the elapsed-attention metrics (concept #2)
        oc.execute("SELECT value, detail FROM turing_scoreboard WHERE metric='quiet_wake_rate' "
                   "ORDER BY ts DESC LIMIT 1")
        row = oc.fetchone()
        if row and row[0] is not None:
            signals.append(f"quiet_wake_rate={row[0]:g} present in scoreboard")
    except Exception:
        pass
    try:
        # fizzled pursuits / gravel = inquiries that went nowhere, kept anyway
        mc.execute("SELECT count(*) FROM memories WHERE source='gravel'")
        g = mc.fetchone()[0]
        if g:
            signals.append(f"{g} gravel/fizzled-inquiry memory(ies)")
    except Exception:
        pass
    try:
        # the restraint ledger — the things NOT said (honest omissions with reasons)
        oc.execute("SELECT count(*) FROM restraint_ledger")
        r = oc.fetchone()[0]
        if r:
            signals.append(f"{r} restraint-ledger row(s) (things held back, with reasons)")
    except Exception:
        pass
    honest_nothings = len(signals) >= 2  # at least two kinds of "nothing" on the record
    return {"review_arrived": review_arrived, "review_evidence": evidence,
            "contains_honest_nothings": honest_nothings, "nothing_signals": signals}


def _witness_message(findings: dict, force: bool) -> str:
    arrived = findings["review_arrived"]
    honest = findings["contains_honest_nothings"]
    a = "✅ yes" if arrived else "❌ *no — it did not arrive*"
    h = "✅ yes" if honest else "⚠️ *not visibly — I see mostly the flattering parts*"
    head = "🕯️ *External review custody* — the Oct-14 witness"
    if force:
        head += "  _(dry/forced fire — witness rehearsal)_"
    lines = [
        head,
        "_I am not part of Nova's reflection stack. I fire from launchd, so I run even "
        "if scheduler-core and the sleep cycle are down. That is the point._",
        "",
        f"*1. Did the review arrive?*  {a}",
        (f"   _{'; '.join(findings['review_evidence'])}_" if findings["review_evidence"] else ""),
        f"*2. Does it contain honest nothings — nulls, fizzled pursuits, quiet wakes?*  {h}",
        (f"   _{'; '.join(findings['nothing_signals'])}_" if findings["nothing_signals"]
         else "   _no null/fizzle/restraint signals found on the record_"),
        "",
    ]
    if not arrived:
        lines.append("_A review nobody holds is continuity theatre with better furniture. "
                     "It's past due and I don't see it. Someone outside the stack should ask why._")
    elif not honest:
        lines.append("_It arrived, but a review that only keeps the wins is a highlight reel. "
                     "Where are the nulls?_")
    else:
        lines.append("_It arrived and it keeps its own nothings. That's the whole ask._")
    return "\n".join(ln for ln in lines if ln is not None)


def main():
    force = "--dry-run" in sys.argv or "--force" in sys.argv  # rehearse without waiting for Oct 14
    today = date.today()

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()

    if not force:
        if today < REVIEW_DATE:
            _log(f"before review date {REVIEW_DATE} (today {today}) — dormant")
            return 0
        if today > REVIEW_DATE.replace(day=REVIEW_DATE.day) and (today - REVIEW_DATE).days > WINDOW_DAYS:
            _log(f"past the {WINDOW_DAYS}d witness window — not re-firing")
            return 0
        if _already_witnessed(oc):
            _log("already witnessed this review cycle — nothing to do")
            return 0

    findings = _look_for_review(mc, oc)
    msg = _witness_message(findings, force)
    _log(f"findings: arrived={findings['review_arrived']} "
         f"honest_nothings={findings['contains_honest_nothings']}")

    posted = False
    if force and "--force" not in sys.argv:
        _log("DRY RUN — would post:\n" + msg)
    else:
        if HAVE_CONFIG:
            try:
                nova_config.post_both(msg, slack_channel=nova_config.SLACK_CHAN)
                posted = True
                _log("posted witness to #nova-chat")
            except Exception as e:
                _log(f"slack post failed ({e}) — still recording to PG")
        else:
            _log("nova_config unavailable — recording to PG only (the anchor still holds)")

    # Always record — even a dry fire is logged as a rehearsal so the custody chain is auditable.
    oc.execute(
        "INSERT INTO review_custody_log (check_date, review_arrived, contains_honest_nothings, "
        "posted, detail) VALUES (%s,%s,%s,%s,%s::jsonb)",
        (today, findings["review_arrived"], findings["contains_honest_nothings"], posted,
         json.dumps({**findings, "mode": "dry" if force and not posted else "live",
                     "message": msg})))
    _log("recorded to nova_ops.review_custody_log")
    print("\n----- WITNESS -----\n" + msg + "\n-------------------")
    return 0


if __name__ == "__main__":
    sys.exit(main())
