#!/usr/bin/env python3
"""nova_turning_point.py — the turning-point budget before Nova intervenes.

"Johnny's Choice" (King, The Dead Zone): Johnny Smith sees what is coming and has to
decide how far to go to change it — and how much it costs him. "Lightning" (Koontz):
the guardian intervenes at the turning points of a life, sparingly, because every
intervention has a price. Nova's version, before any PROACTIVE intervention in this
lane (a reach to Jordan, the held-reach bundle, a proposal put to him, a wish post,
the Boiler's bleed):

  (a) STAKES  — a 0..1 estimate supplied by the caller from its own data (care score,
      item count, boiler pressure, proposal backlog);
  (b) LADDER  — journal (note it, tell no one) -> mention -> recommend -> act. The
      stakes pick the lowest rung that would work; a kind can never go above its own
      ceiling (a reach is at most a mention); low CALIBRATED confidence (nova_soft_
      certainty.calibrate, which shrinks overconfident domains) drops one rung;
  (c) BUDGET  — rungs cost units (journal 0, mention 1, recommend 2, act 4) from a
      weekly intervention budget (dial_scale('proactivity', 12, 40, 80) — 40 at the
      default, sized from the observed ~35 units/week in 2026-09/10). Spends and holds
      are written to nova_ops.restraint_ledger through nova_restraint.record_restraint
      (channel 'turning-point'; detail.turning_point = {kind, rung, cost, stakes, conf,
      spent}). A hold is real restraint: the ledger keeps what she would have said.
      In the last quarter of the budget only stakes >= LATE_STAKES may spend.

decide(...) -> {"rung", "allowed", "cost", "stakes", "confidence", "budget", "spent",
"reason"}. allowed=False means: do not intervene; she noted it instead. Fails OPEN to
today's behaviour when PG is unreachable (it must never silence a real alert because
the ledger was down), but logs that it did.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

RUNGS = ("journal", "mention", "recommend", "act")
COST = {"journal": 0, "mention": 1, "recommend": 2, "act": 4}
# Stakes needed before each rung is warranted (the lowest rung whose bar the stakes clear).
STAKES_FOR = {"journal": 0.0, "mention": 0.35, "recommend": 0.65, "act": 0.9}
MIN_CONF = 0.35        # calibrated confidence below this drops one rung
LATE_STAKES = 0.7      # with < 25% of the weekly budget left, only this much stakes spends
CHANNEL = "turning-point"


def weekly_budget() -> int:
    try:
        from nova_voice import dial_scale
        return int(round(dial_scale("proactivity", 12, 40, 80)))
    except Exception:
        return 40


def ladder(ceiling: str = "act") -> list:
    """The rungs from least to most invasive, up to the kind's ceiling."""
    return list(RUNGS[:RUNGS.index(ceiling) + 1])


def pick_rung(stakes: float, confidence: float | None, ceiling: str = "act") -> str:
    """Lowest rung whose stakes bar is cleared — i.e. the least invasive step that the
    stakes justify — demoted one rung when calibrated confidence is low."""
    rungs = ladder(ceiling)
    rung = "journal"
    for r in rungs:
        if stakes >= STAKES_FOR[r]:
            rung = r
    if confidence is not None and confidence < MIN_CONF and rung != "journal":
        rung = rungs[rungs.index(rung) - 1]
    return rung


def spent_this_week(oc) -> int:
    oc.execute("SELECT coalesce(sum((detail->'turning_point'->>'cost')::int), 0) FROM restraint_ledger "
               "WHERE channel=%s AND ts > now() - interval '7 days' "
               "AND (detail->'turning_point'->>'spent')::boolean", (CHANNEL,))
    return int(oc.fetchone()[0] or 0)


def calibrated(oc, stated: float, domain: str | None) -> float | None:
    try:
        from nova_soft_certainty import calibrate
        return float(calibrate(stated, oc, domain))
    except Exception:
        return None


def decide(oc, kind: str, stakes: float, text: str, ceiling: str = "mention",
           confidence: float | None = None, domain: str | None = None,
           force: bool = False, dry: bool = False) -> dict:
    """Run the turning-point check and log it. `confidence` is the caller's raw
    confidence (calibrated here against `domain`); if None, stakes stands in for it.
    force=True (the Boiler's bleed — a safety valve) spends even past the budget."""
    stakes = max(0.0, min(1.0, float(stakes or 0)))
    conf = calibrated(oc, confidence if confidence is not None else stakes, domain)
    rung = pick_rung(stakes, conf, ceiling)
    cost = COST[rung]
    budget = weekly_budget()
    try:
        spent = spent_this_week(oc)
    except Exception as e:  # noqa: BLE001 — fail open, logged
        try:
            oc.connection.rollback()
        except Exception:
            pass
        return {"rung": ceiling, "allowed": True, "cost": COST[ceiling], "stakes": stakes,
                "confidence": conf, "budget": budget, "spent": None,
                "reason": f"ledger unreadable ({e}) — failing open"}
    left = budget - spent
    if rung == "journal":
        allowed, reason = False, f"stakes {stakes:.2f} / confidence {conf} only warrant a note"
    elif force:
        allowed, reason = True, "forced (safety valve)"
    elif cost > left:
        allowed, reason = False, f"weekly budget spent ({spent}/{budget})"
    elif left < budget * 0.25 and stakes < LATE_STAKES:
        allowed, reason = False, f"budget low ({left}/{budget} left) and stakes {stakes:.2f} < {LATE_STAKES}"
    else:
        allowed, reason = True, f"{rung} costs {cost}, {left - cost}/{budget} left"
    res = {"rung": rung if allowed else "journal", "wanted": rung, "allowed": allowed, "cost": cost if allowed else 0,
           "stakes": round(stakes, 3), "confidence": conf, "budget": budget, "spent": spent, "reason": reason}
    if not dry:
        try:
            from nova_restraint import record_restraint
            record_restraint(
                context=f"turning-point {kind}",
                would_have_said=(text or "")[:4000] or f"[{kind}]",
                reason=("SPENT: " if allowed else "HELD: ") + reason,
                channel=CHANNEL,
                detail={"turning_point": {"kind": kind, "rung": res["rung"], "wanted": rung,
                                          "cost": res["cost"], "stakes": res["stakes"], "conf": conf,
                                          "spent": allowed, "budget": budget, "spent_before": spent}},
                conn=oc.connection)
        except Exception:
            pass
    return res


def status(oc) -> dict:
    b = weekly_budget()
    s = spent_this_week(oc)
    oc.execute("SELECT detail->'turning_point'->>'kind', count(*) FILTER (WHERE (detail->'turning_point'->>'spent')::boolean), "
               "count(*) FILTER (WHERE NOT (detail->'turning_point'->>'spent')::boolean) FROM restraint_ledger "
               "WHERE channel=%s AND ts > now() - interval '7 days' GROUP BY 1", (CHANNEL,))
    return {"budget": b, "spent": s, "by_kind": {k: {"spent": a, "held": h} for k, a, h in oc.fetchall()}}


if __name__ == "__main__":
    import psycopg2
    c = psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj", connect_timeout=5)
    c.autocommit = True
    print(json.dumps(status(c.cursor()), indent=2))
