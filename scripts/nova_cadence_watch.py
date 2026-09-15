#!/usr/bin/env python3
"""nova_cadence_watch.py — anomalous-silence detection via EXPECTED CADENCE (Concept #5).

THE INSIGHT (Marey / Rockbot). An *audit* can only see something missing when a
THIRD party witnesses it — a References: header quoting a lost message-id, a
sequence number with a hole. A one-to-one thread, or an unread 1:1 memory, has
NO witness, so "99% unread isn't uniformly unread": part of it is unwitnessed.
The instrument for the unwitnessed part is EXPECTED CADENCE — "a thing that
should have arrived and didn't." We LEARN each recurring input's normal arrival
rhythm from history, then flag the ones that have gone quiet past their own
learned interval.

THE EPISTEMIC SPLIT (non-negotiable, enforced in BOTH schema and code). There
are three distinct states and SILENT is NEVER promoted to MISSING without a
witness:

  * MISSING     — witnessed: a THIRD source references a thing that isn't there.
                  Requires a `witness`. Only record_missing() can write it, and
                  the DB CHECK refuses MISSING with a NULL witness.
  * ACKED_LOST  — confirmed: the source itself confirmed the loss.
                  Only record_acked_lost() can write it.
  * SILENT      — cadence-expected but unobserved: expected-and-not-seen, cause
                  UNKNOWN. This is the only state THIS watcher's cadence pass
                  ever emits. The DB CHECK refuses SILENT with a witness — the
                  unwitnessed state cannot carry a witness, so it can never be
                  quietly upgraded into a claim of loss.

This generalizes nova_freshness_monitor's fixed-SLA stale-stream idea into a
principled instrument: cadence is LEARNED per source (not a hand-set SLA), and
the output is epistemically honest — "X has been quiet longer than usual",
never "X is missing/lost." Alerts route through the triage brain
(nova_alert_triage.triage, category='cadence') so a quiet feed arrives with
context, not as a false page.

State store: nova_ops.cadence_state (source, expected_interval, last_seen,
state, since, ...). Memory notes on a SILENT transition carry metadata.lineage
(nova_lineage).

Runs ~hourly on the core scheduler:
    nova_cadence_watch.py            # one pass, learn+detect, persist, alert
    nova_cadence_watch.py --dry-run  # report only, no writes/alerts
    nova_cadence_watch.py --report   # print current cadence_state table

Written by Jordan Koch.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEMORY_URL = "http://memory-server.digitalnoise.net:18790/remember"

# How many multiples of the learned median gap without an arrival before a
# source is considered SILENT. Plus an absolute floor so a fast stream isn't
# flagged over a trivial hiccup.
SILENT_FACTOR = 4.0
MIN_OBSERVATIONS = 12          # need this many arrivals to trust a learned cadence
HISTORY_LIMIT = 500            # DISTINCT arrival-seconds sampled per source
LOOKBACK = "60 days"           # window for learning + last_seen
# Absolute floor on the SILENT threshold. An hourly watcher cannot honestly
# assert silence at finer than multi-run granularity, and many feeds arrive in
# batches (many rows sharing a second). So a source is SILENT only when quiet
# past max(learned_median * factor, this floor) — which also stops burst/batch
# streams (median≈0) from false-flagging on a few seconds' gap.
MIN_THRESHOLD_S = 3600.0

# Nova's recurring SENSOR / FEED inputs (schema.table, ts column). Each is a
# stream whose absence is meaningful. Learned cadence, not a hand-set SLA.
STREAMS = [
    ("telemetry.net_liveness", "ts"),
    ("telemetry.weather", "ts"),
    ("telemetry.soil", "ts"),
    ("telemetry.overhead_flights", "ts"),
    ("telemetry.chp_incidents", "ts"),
    ("telemetry.aux_sensors", "ts"),
    ("public.bambu_telemetry", "ts"),
    ("public.health_checks", "checked_at"),
    ("public.snmp_metrics", "timestamp"),
]

# Optional adapters (best-effort, never fatal)
try:
    from nova_alert_triage import triage as _triage
except Exception:
    _triage = None
try:
    from nova_lineage import lineage_stamp, lineage_line
except Exception:                       # pragma: no cover
    def lineage_stamp(**k): return {}
    def lineage_line(**k): return ""


def log(m: str):
    print(f"[cadence {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── schema ────────────────────────────────────────────────────────────────────

DDL = """
CREATE TABLE IF NOT EXISTS cadence_state (
    source            text PRIMARY KEY,
    kind              text NOT NULL DEFAULT 'stream',
    expected_interval interval,
    last_seen         timestamptz,
    state             text NOT NULL DEFAULT 'OK',
    since             timestamptz NOT NULL DEFAULT now(),
    witness           text,
    detail            jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT cadence_state_state_ck
        CHECK (state IN ('OK','SILENT','ACKED_LOST','MISSING')),
    -- Epistemic split, made un-violable by the DB itself:
    CONSTRAINT cadence_missing_needs_witness
        CHECK (state <> 'MISSING' OR witness IS NOT NULL),
    CONSTRAINT cadence_silent_is_unwitnessed
        CHECK (state <> 'SILENT'  OR witness IS NULL)
);
"""


def ensure_schema(cur):
    cur.execute(DDL)


# ── cadence learning ────────────────────────────────────────────────────────────

def learn(cur, table: str, ts_col: str) -> dict | None:
    """Learn arrival cadence for one stream from recent history.

    Returns {last_seen, median_gap_s, n} or None if not enough data to trust.
    """
    # Learn cadence over DISTINCT arrival-seconds: many feeds insert a burst of
    # rows sharing one timestamp (a batch = ONE arrival), which would otherwise
    # drive the median gap to ~0 and false-flag. Collapsing to distinct seconds
    # measures the real inter-arrival rhythm.
    q = (
        f"WITH t AS (SELECT DISTINCT date_trunc('second', {ts_col}) AS ts FROM {table} "
        f"WHERE {ts_col} > now() - interval '{LOOKBACK}' "
        f"ORDER BY ts DESC LIMIT {HISTORY_LIMIT}), "
        "g AS (SELECT EXTRACT(EPOCH FROM (ts - lead(ts) OVER (ORDER BY ts DESC))) AS gap FROM t) "
        "SELECT (SELECT max(ts) FROM t) AS last_seen, "
        "       percentile_cont(0.5) WITHIN GROUP (ORDER BY gap) AS median_gap, "
        "       count(gap) AS n FROM g WHERE gap IS NOT NULL AND gap > 0"
    )
    cur.execute(q)
    row = cur.fetchone()
    if not row or row[0] is None or row[2] is None:
        return None
    last_seen, median_gap, n = row
    if n < MIN_OBSERVATIONS or not median_gap or median_gap <= 0:
        return None
    return {"last_seen": last_seen, "median_gap_s": float(median_gap), "n": int(n)}


def classify(learned: dict, now: datetime) -> tuple[str, float, float]:
    """Return (state, age_s, threshold_s). state is 'OK' or 'SILENT' only —
    the cadence pass NEVER emits MISSING/ACKED_LOST (those need a witness /
    a source confirmation and go through the dedicated recorders)."""
    age = (now - learned["last_seen"]).total_seconds()
    threshold = max(learned["median_gap_s"] * SILENT_FACTOR, MIN_THRESHOLD_S)
    state = "SILENT" if age > threshold else "OK"
    return state, age, threshold


# ── state persistence (epistemic split enforced here too) ────────────────────────

def _fmt_interval_s(seconds: float) -> str:
    return f"{seconds:.0f} seconds"


def _human_age(seconds: float) -> str:
    if seconds >= 86400:
        return f"{seconds/86400:.1f} days"
    if seconds >= 3600:
        return f"{seconds/3600:.1f} hours"
    if seconds >= 60:
        return f"{seconds/60:.0f} minutes"
    return f"{seconds:.0f} seconds"


def upsert_cadence(cur, source: str, kind: str, learned: dict, state: str,
                   detail: dict) -> tuple[str, bool]:
    """Persist an OK/SILENT observation. Returns (prev_state, transitioned).

    SILENT is written with witness=NULL — the DB constraint guarantees the
    unwitnessed state cannot masquerade as a witnessed claim of loss.
    """
    cur.execute("SELECT state FROM cadence_state WHERE source=%s", (source,))
    r = cur.fetchone()
    prev = r[0] if r else None
    transitioned = (prev != state)
    # `since` only advances on a genuine state change; otherwise it's preserved.
    cur.execute(
        "INSERT INTO cadence_state "
        "(source, kind, expected_interval, last_seen, state, since, witness, detail, updated_at) "
        "VALUES (%s,%s,%s,%s,%s, now(), NULL, %s, now()) "
        "ON CONFLICT (source) DO UPDATE SET "
        "  kind=EXCLUDED.kind, expected_interval=EXCLUDED.expected_interval, "
        "  last_seen=EXCLUDED.last_seen, state=EXCLUDED.state, "
        "  since=CASE WHEN cadence_state.state IS DISTINCT FROM EXCLUDED.state "
        "             THEN now() ELSE cadence_state.since END, "
        "  witness=NULL, detail=EXCLUDED.detail, updated_at=now()",
        (source, kind, _fmt_interval_s(learned["median_gap_s"]),
         learned["last_seen"], state, psycopg2.extras.Json(detail)),
    )
    return prev, transitioned


def record_missing(cur, source: str, witness: str, detail: dict | None = None):
    """Promote to MISSING — ONLY with a third-party witness. A caller cannot
    reach MISSING without supplying `witness`; the DB CHECK backs this up. This
    is the single sanctioned path from silence to a claim of loss."""
    if not witness or not str(witness).strip():
        raise ValueError("record_missing requires a witness — MISSING is the "
                         "WITNESSED state; without a witness use SILENT.")
    cur.execute(
        "INSERT INTO cadence_state (source, state, since, witness, detail, updated_at) "
        "VALUES (%s,'MISSING', now(), %s, %s, now()) "
        "ON CONFLICT (source) DO UPDATE SET state='MISSING', "
        "  since=CASE WHEN cadence_state.state<>'MISSING' THEN now() ELSE cadence_state.since END, "
        "  witness=EXCLUDED.witness, detail=EXCLUDED.detail, updated_at=now()",
        (source, witness, psycopg2.extras.Json(detail or {})),
    )


def record_acked_lost(cur, source: str, ack: str, detail: dict | None = None):
    """Confirm loss the source itself acknowledged. `ack` records who/what
    confirmed it (kept in detail; not a third-party witness)."""
    d = dict(detail or {}); d["ack"] = ack
    cur.execute(
        "INSERT INTO cadence_state (source, state, since, witness, detail, updated_at) "
        "VALUES (%s,'ACKED_LOST', now(), NULL, %s, now()) "
        "ON CONFLICT (source) DO UPDATE SET state='ACKED_LOST', "
        "  since=CASE WHEN cadence_state.state<>'ACKED_LOST' THEN now() ELSE cadence_state.since END, "
        "  witness=NULL, detail=EXCLUDED.detail, updated_at=now()",
        (source, psycopg2.extras.Json(d)),
    )


# ── memory note (carries lineage) ────────────────────────────────────────────────

def _write_silence_memory(source: str, age_s: float, median_s: float):
    """Low-key memory that a recurring input went quiet — with provenance-of-
    the-provenance in metadata.lineage. Best-effort; never fatal."""
    stamp = lineage_stamp(substrate="deterministic (nova_cadence_watch, no model)",
                          capture_point="at detection")
    text = (f"Cadence note: '{source}' has been quiet for {_human_age(age_s)}, "
            f"longer than its usual ~{_human_age(median_s)} rhythm. State: SILENT "
            f"(cadence-expected, unobserved) — cause unknown, NOT confirmed missing. "
            f"[{lineage_line(stamp)}]")
    payload = json.dumps({
        "text": text, "source": "cadence_watch", "tier": "long_term",
        "metadata": {"kind": "cadence_silence", "cadence_source": source,
                     "state": "SILENT", "privacy": "private",
                     "ingested_by": "nova_cadence_watch.py", "lineage": stamp},
    }).encode()
    try:
        req = urllib.request.Request(MEMORY_URL + "?async=1", data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception as e:
        log(f"  memory note failed for {source}: {e}")
        return False


# ── alerting via the triage brain ─────────────────────────────────────────────────

def _alert_silent(source: str, age_s: float, median_s: float, dry_run: bool):
    """Route a SILENT transition through the triage brain as a LOW-KEY
    'quiet longer than usual' — deliberately NOT 'missing/lost'."""
    title = f"{source} has been quiet longer than usual"
    body = (f"No arrivals for {_human_age(age_s)}; normal cadence ~{_human_age(median_s)}. "
            f"State SILENT (cadence-expected, unobserved) — cause unknown. "
            f"This is anomalous silence, NOT a confirmed loss.")
    if dry_run or _triage is None:
        log(f"  [alert:{'dry' if dry_run else 'no-triage'}] {title}")
        return
    try:
        d = _triage(title, body=body, level="info", category="cadence",
                    source="nova_cadence_watch.py", dedup_key=f"cadence:{source}")
        log(f"  triage → decision={d.get('decision')} verdict={d.get('verdict')} "
            f"conf={d.get('confidence')}")
    except Exception as e:
        log(f"  triage failed for {source}: {e}")


# ── main pass ─────────────────────────────────────────────────────────────────────

def run_once(dry_run: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    conn = psycopg2.connect(OPS_DSN)
    conn.autocommit = True
    cur = conn.cursor()
    if not dry_run:
        ensure_schema(cur)
    else:
        # dry-run still needs the table to read prev-state; create if missing is a write,
        # so guard it — read prev only if table exists.
        cur.execute("SELECT to_regclass('public.cadence_state')")
        if cur.fetchone()[0] is None:
            ensure_schema(cur)

    summary = {"checked": 0, "silent": [], "ok": 0, "skipped": []}
    for table, ts_col in STREAMS:
        try:
            learned = learn(cur, table, ts_col)
        except Exception as e:
            log(f"{table}: learn failed: {e}")
            summary["skipped"].append({"source": table, "why": str(e)[:120]})
            continue
        if not learned:
            summary["skipped"].append({"source": table, "why": "insufficient history"})
            continue
        summary["checked"] += 1
        state, age, threshold = classify(learned, now)
        detail = {"median_gap_s": round(learned["median_gap_s"], 2),
                  "observations": learned["n"], "age_s": round(age, 1),
                  "silent_threshold_s": round(threshold, 1),
                  "silent_factor": SILENT_FACTOR}
        if dry_run:
            log(f"{table}: state={state} age={_human_age(age)} "
                f"median={_human_age(learned['median_gap_s'])} n={learned['n']}")
            if state == "SILENT":
                summary["silent"].append({"source": table, "age_s": round(age, 1),
                                          "median_gap_s": round(learned['median_gap_s'], 2)})
            else:
                summary["ok"] += 1
            continue

        prev, transitioned = upsert_cadence(cur, table, "stream", learned, state, detail)
        if state == "SILENT":
            summary["silent"].append({"source": table, "age_s": round(age, 1),
                                      "median_gap_s": round(learned['median_gap_s'], 2),
                                      "since_new": transitioned})
            if transitioned:
                log(f"{table}: → SILENT (quiet {_human_age(age)}, usual ~"
                    f"{_human_age(learned['median_gap_s'])})")
                _alert_silent(table, age, learned["median_gap_s"], dry_run)
                _write_silence_memory(table, age, learned["median_gap_s"])
            else:
                log(f"{table}: still SILENT ({_human_age(age)})")
        else:
            summary["ok"] += 1
            if prev == "SILENT":
                log(f"{table}: recovered → OK (fresh within cadence)")

    cur.close(); conn.close()
    log(f"pass done: {summary['checked']} learned, {len(summary['silent'])} SILENT, "
        f"{summary['ok']} OK, {len(summary['skipped'])} skipped")
    return summary


def report():
    conn = psycopg2.connect(OPS_DSN); conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT to_regclass('public.cadence_state')")
    if cur.fetchone()["to_regclass"] is None:
        print("cadence_state does not exist yet — run a pass first.")
        return
    cur.execute("SELECT source, kind, expected_interval, last_seen, state, since, witness "
                "FROM cadence_state ORDER BY state DESC, source")
    rows = cur.fetchall()
    for r in rows:
        print(json.dumps({k: (str(v) if v is not None else None) for k, v in r.items()}))
    cur.close(); conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="learn+classify, no writes/alerts")
    ap.add_argument("--report", action="store_true", help="print cadence_state and exit")
    args = ap.parse_args(argv)
    if args.report:
        report(); return 0
    out = run_once(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
