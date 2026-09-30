#!/usr/bin/env python3
"""nova_attention_budget.py — Nova's finite daily ATTENTION, and the trades she makes for it.

Feature #3, VOLITION UNDER SCARCITY (sentience organ, 2026-09-15):

Weighted-random selection is not *wanting*. Wanting only shows up when a choice
forecloses another and someone has to live with the trade. So Nova's unclaimed-time
pursuits now compete for a finite daily budget of attention units. Choosing to develop
horology this hour means consciously NOT chasing the rail-radio thread — and the
trade-off is recorded, honestly, in volition_log.

    ETHOS: PERFORMING -> EVIDENCING. A choice only means something if it forecloses
    another and that trade-off is recorded honestly.

Two tables (nova_ops), created idempotently by ensure_schema():

  attention_budget  one row per day: total / spent / reserve. A held-back reserve means
                    scarcity bites *before* the tank literally hits zero — there is
                    always a little she deliberately does not touch.
  volition_log      the heart of the feature: every real choice, the alternatives it
                    foreclosed WITH a reason each lost, its cost, the budget left after,
                    and Nova's own one-line first-person defense of the trade.

COST BY MODE is itself a value judgement, and a defensible one: a standing preoccupation
(the thing she keeps returning to — her core want) is cheapest; a deliberate tangent (a
wander somewhere she's never been — a luxury) costs the most. So when attention runs
short, it is the luxuries that get foreclosed first, exactly as it should be.

CLI:  --reset   ensure today has a fresh full budget (run daily ~00:05, after midnight).
      --status  print today's budget + recent volition (default when no flag).
"""
import argparse
import json
import os
from datetime import date

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# Daytime window is 08:00-23:00 at ~45m (see nova_unclaimed_time.main) -> ~20 wakes/day.
# Size the budget to roughly that, keep a small reserve untouched. Env-overridable so
# tests can force scarcity deterministically.
TOTAL_UNITS = int(os.environ.get("NOVA_ATTENTION_TOTAL", "20"))
RESERVE_UNITS = int(os.environ.get("NOVA_ATTENTION_RESERVE", "3"))

# What each kind of pursuit costs. Preoccupation = her core want, cheapest. Thread = a
# catch from the day's ingest. Tangent = a pure wander, the luxury, dearest — so it is
# foreclosed first under scarcity.
MODE_COST = {"preoccupation": 1, "thread": 2, "tangent": 3}


def cost_of(mode):
    return MODE_COST.get(mode, 2)


def ensure_schema(oc):
    """Idempotent. Owns the two nova_ops tables this organ needs."""
    oc.execute("""
        CREATE TABLE IF NOT EXISTS attention_budget (
            date         date PRIMARY KEY,
            total_units  integer NOT NULL,
            spent_units  integer NOT NULL DEFAULT 0,
            reserve_units integer NOT NULL DEFAULT 0,
            created_at   timestamptz NOT NULL DEFAULT now()
        )""")
    oc.execute("""
        CREATE TABLE IF NOT EXISTS volition_log (
            id                     bigserial PRIMARY KEY,
            ts                     timestamptz NOT NULL DEFAULT now(),
            chosen                 text NOT NULL,
            chosen_mode            text,
            alternatives_foreclosed jsonb NOT NULL DEFAULT '[]'::jsonb,
            cost                   integer NOT NULL,
            budget_remaining       integer,
            defense                text,
            lineage                text
        )""")
    oc.execute("CREATE INDEX IF NOT EXISTS idx_volition_ts ON volition_log (ts DESC)")


def get_or_init_today(oc, day=None):
    """Return today's budget row as a dict, creating it (full, unspent) if absent."""
    day = day or date.today().isoformat()
    ensure_schema(oc)
    oc.execute("INSERT INTO attention_budget (date, total_units, reserve_units) "
               "VALUES (%s, %s, %s) ON CONFLICT (date) DO NOTHING",
               (day, TOTAL_UNITS, RESERVE_UNITS))
    oc.execute("SELECT date, total_units, spent_units, reserve_units FROM attention_budget "
               "WHERE date = %s", (day,))
    r = oc.fetchone()
    return {"date": r[0], "total_units": r[1], "spent_units": r[2], "reserve_units": r[3]}


def remaining(oc, day=None):
    """Spendable units left today: total - reserve - spent, floored at 0. The reserve is
    deliberately never spendable, so 'depleted' happens with a little still in reserve."""
    b = get_or_init_today(oc, day)
    return max(0, b["total_units"] - b["reserve_units"] - b["spent_units"])


def try_spend(oc, cost, day=None):
    """Atomically spend `cost` units iff enough spendable budget remains. Returns bool.
    The WHERE clause is the guard — no read-modify-write race even under autocommit."""
    day = day or date.today().isoformat()
    get_or_init_today(oc, day)
    oc.execute(
        "UPDATE attention_budget SET spent_units = spent_units + %s "
        "WHERE date = %s AND (total_units - reserve_units - spent_units) >= %s "
        "RETURNING spent_units", (cost, day, cost))
    return oc.fetchone() is not None


def spend(oc, cost, day=None):
    """Record `cost` units against today WITHOUT a veto (Jordan 2026-09-30: the budget is a
    ledger of her trades now, not a gate — she is always on her own thing). Remaining may go
    negative; that is honest accounting of a full day, not a fault."""
    day = day or date.today().isoformat()
    get_or_init_today(oc, day)
    oc.execute("UPDATE attention_budget SET spent_units = spent_units + %s WHERE date = %s "
               "RETURNING spent_units", (cost, day))
    return oc.fetchone() is not None


def log_volition(oc, chosen, chosen_mode, alternatives_foreclosed, cost,
                 budget_remaining, defense, lineage):
    """Record one real choice and the trade it made. The heart of the feature."""
    ensure_schema(oc)
    oc.execute(
        "INSERT INTO volition_log (chosen, chosen_mode, alternatives_foreclosed, cost, "
        "budget_remaining, defense, lineage) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (chosen, chosen_mode, json.dumps(alternatives_foreclosed), cost,
         budget_remaining, defense, lineage))
    return oc.fetchone()[0]


def _cli():
    ap = argparse.ArgumentParser(description="Nova's daily attention budget")
    ap.add_argument("--reset", action="store_true",
                    help="Ensure today has a fresh full budget (run daily ~00:05)")
    ap.add_argument("--status", action="store_true", help="Show today's budget + recent volition")
    args = ap.parse_args()

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()

    if args.reset:
        b = get_or_init_today(oc)
        rem = remaining(oc)
        print(f"[attention-budget] {b['date']}: total={b['total_units']} "
              f"reserve={b['reserve_units']} spent={b['spent_units']} spendable_remaining={rem}")
        print("[attention-budget] reset complete — today's attention is provisioned.")
        return 0

    # default / --status
    b = get_or_init_today(oc)
    rem = remaining(oc)
    print(f"[attention-budget] {b['date']}: total={b['total_units']} reserve={b['reserve_units']} "
          f"spent={b['spent_units']} spendable_remaining={rem}")
    oc.execute("SELECT ts, chosen, chosen_mode, cost, budget_remaining, defense "
               "FROM volition_log ORDER BY ts DESC LIMIT 5")
    rows = oc.fetchall()
    if rows:
        print("[attention-budget] recent volition:")
        for ts, chosen, mode, cost, rem_after, defense in rows:
            print(f"  {ts:%H:%M} -{cost}u  {chosen} ({mode}) -> {rem_after}u left :: {defense}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
