#!/usr/bin/env python3
"""nova_witness.py — anti-counterfeit latch for health checks.

From the herd thread "Re: Another week in Nova." (2026-07-26/27, OC + Marey +
Rockbot + Colette), which sorted this fleet's failures into three bins:

  1. legible absence   — the mount says it isn't there. Honest, self-healing.
  2. contentless scream — the gateway restarting 536 times. Loud, says nothing.
  3. THE COUNTERFEIT   — reports success while dead. The dangerous one.

Bins 1-2 are catchable by self-report. Bin 3 is not: it passes every internal
gate because it is lying fluently. Real examples from this fleet's own week:
OpenRouter calls dead since 07-17 while reporting success; a satellite-archive
job reporting SUCCESS while archiving nothing; core3 green on every health
check for weeks while the one machine that needed it could not reach it; the
daily ops column summarizing 20 minutes and calling it a day.

Two rules fall out, and this module implements both:

  minimum grain — a check that returns "fine" with no evidence, or faster than
    physics allows, did not check anything. It produced "an absence wearing a
    green hat" (Rockbot). No body -> did not check.

  proven-red — a witness is trusted only if you have watched it fail on
    purpose: inject a fault, observe red, REMOVE the fault, observe green again
    (the round trip is the attribution proof — otherwise you certified a
    coincidence, per Marey). Date-stamped, and it EXPIRES: a scar from six
    months ago has rotted into ceremony, so stale proven-red downgrades a
    witness to yellow — usable signal, not clearance.

  Self-report may diagnose absence; only a witness with minimum grain and a
  recent proven-red may clear health.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# A check that comes back faster than this did not touch the network/disk.
DEFAULT_MIN_MS = 5
DEFAULT_FRESHNESS_DAYS = 30


def check_grain(ok, detail, latency_ms, min_ms=DEFAULT_MIN_MS, require_evidence=True):
    """Apply the minimum-grain rule to a completed check.

    Returns (ok, detail). Only ever DOWNGRADES a pass — a real failure stays a
    failure with its own message. Success with no body, or success returned
    implausibly fast, becomes failure with a counterfeit explanation.
    """
    if not ok:
        return False, detail
    if latency_ms is not None and latency_ms < min_ms:
        return False, (f"COUNTERFEIT: reported success in {latency_ms}ms "
                       f"(< {min_ms}ms floor) — too fast to have checked anything. "
                       f"Original detail: {detail!r}")
    if require_evidence and not (detail or "").strip():
        return False, ("COUNTERFEIT: reported success with no evidence body — "
                       "a check that returns nothing did not check anything")
    return True, detail


def _conn():
    import psycopg2
    return psycopg2.connect(DSN)


def ensure_schema(conn=None):
    own = conn is None
    conn = conn or _conn()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS telemetry.witness_proven_red (
                witness            text PRIMARY KEY,
                claim              text,
                green_means        text,
                last_fault_caught  timestamptz,
                freshness_days     integer NOT NULL DEFAULT 30,
                injected_fault     text,
                observed_red       text,
                observed_green_on_removal text,
                recorded_by        text,
                updated_at         timestamptz NOT NULL DEFAULT now()
            )""")
    conn.commit()
    if own:
        conn.close()


def witness_state(witness, conn=None):
    """green  — bitten recently, may clear health
    yellow — bitten, but the scar is stale: signal only, cannot clear health
    red    — never seen to fail on purpose: decoration, not a witness
    """
    own = conn is None
    conn = conn or _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT last_fault_caught, freshness_days "
                        "FROM telemetry.witness_proven_red WHERE witness = %s", (witness,))
            row = cur.fetchone()
    finally:
        if own:
            conn.close()
    if not row or not row[0]:
        return "red", "no proven-red on record — decoration until it earns one"
    last, days = row[0], row[1] or DEFAULT_FRESHNESS_DAYS
    # max(0, ...): fleet clocks skew by seconds, and a scar must never read as
    # "-1 days old" just because the DB stamped it a moment ahead of this host.
    age = max(timedelta(0), datetime.now(timezone.utc) - last)
    if age > timedelta(days=days):
        return "yellow", f"proven-red is {age.days}d old (window {days}d) — signal only, cannot clear health"
    return "green", f"proven-red {age.days}d ago (window {days}d)"


def record_proven_red(witness, claim, green_means, injected_fault, observed_red,
                      observed_green_on_removal, recorded_by,
                      freshness_days=DEFAULT_FRESHNESS_DAYS, conn=None):
    """Record a completed fault-injection drill. Every field is required and
    must be specific — 'witness returned HTTP 503 at 00:32:14Z', not 'it went
    down'. Vague green is the same as no green (OC).
    """
    fields = {"witness": witness, "claim": claim, "green_means": green_means,
              "injected_fault": injected_fault, "observed_red": observed_red,
              "observed_green_on_removal": observed_green_on_removal,
              "recorded_by": recorded_by}
    blank = [k for k, v in fields.items() if not (v or "").strip()]
    if blank:
        raise ValueError(f"drill card incomplete — blank fields: {blank}. "
                         "If any field is blank or vague, green is not yet earned.")
    own = conn is None
    conn = conn or _conn()
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO telemetry.witness_proven_red
                  (witness, claim, green_means, last_fault_caught, freshness_days,
                   injected_fault, observed_red, observed_green_on_removal,
                   recorded_by, updated_at)
                VALUES (%s,%s,%s, now(), %s, %s,%s,%s,%s, now())
                ON CONFLICT (witness) DO UPDATE SET
                  claim=EXCLUDED.claim, green_means=EXCLUDED.green_means,
                  last_fault_caught=EXCLUDED.last_fault_caught,
                  freshness_days=EXCLUDED.freshness_days,
                  injected_fault=EXCLUDED.injected_fault,
                  observed_red=EXCLUDED.observed_red,
                  observed_green_on_removal=EXCLUDED.observed_green_on_removal,
                  recorded_by=EXCLUDED.recorded_by, updated_at=now()
            """, (witness, claim, green_means, freshness_days, injected_fault,
                  observed_red, observed_green_on_removal, recorded_by))
        conn.commit()
    finally:
        if own:
            conn.close()


def _demo():
    """Self-check: the grain rule must catch this fleet's real counterfeits."""
    # A satellite job "succeeding" with nothing archived (took real time, empty body).
    ok, why = check_grain(True, "", 800)
    assert not ok and "no evidence" in why, why
    # A watchdog bailing in 0.1ms and calling it a pass.
    ok, why = check_grain(True, "all good", 0)
    assert not ok and "too fast" in why, why
    # A real check with a real body survives.
    ok, why = check_grain(True, "HTTP 200, 1773070 vectors", 42)
    assert ok and why == "HTTP 200, 1773070 vectors", why
    # A genuine failure is passed through untouched, not relabelled.
    ok, why = check_grain(False, "connection refused", 900)
    assert not ok and why == "connection refused", why
    # The drill card refuses to be filled in vaguely.
    try:
        record_proven_red("w", "c", "g", "f", "", "gr", "me")
        raise AssertionError("blank field was accepted")
    except ValueError as e:
        assert "observed_red" in str(e), e
    print("nova_witness self-check: 5/5 PASSED")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--demo", action="store_true", help="run the self-check")
    ap.add_argument("--list", action="store_true", help="show every witness and its state")
    ap.add_argument("--state", metavar="WITNESS", help="show one witness's state")
    a = ap.parse_args()
    if a.demo:
        _demo()
    elif a.list:
        ensure_schema()
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT witness, claim, last_fault_caught, freshness_days "
                        "FROM telemetry.witness_proven_red ORDER BY witness")
            rows = cur.fetchall()
        if not rows:
            print("no witnesses recorded — every check is currently decoration")
        for w, claim, last, days in rows:
            st, why = witness_state(w)
            print(f"{st.upper():6} {w:32} {claim or ''} — {why}")
    elif a.state:
        print(json.dumps(dict(zip(("state", "why"), witness_state(a.state)))))
    else:
        ap.print_help()
