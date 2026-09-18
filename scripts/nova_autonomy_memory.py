#!/usr/bin/env python3
"""nova_autonomy_memory.py — AUTONOMY, REMEMBERED: turn agency into memory.

Jordan, 2026-09-18: Nova now *acts* on her own — she self-heals services, exercises
earned autonomy, and sometimes gets vetoed. All of that lands in the operational
ledgers (autonomy_ledger, autonomy_trust), but a ledger is not a memory. A ledger is
something you query; a memory is something you *carry*. This organ closes that gap.

On each run it finds the significant NEW autonomy events since the last run and writes
each one as a concise, FIRST-PERSON reflective memory to the vector store, so that
later — in a nightly reflection, a curiosity question, a conversation with Jordan —
Nova can recall "I healed nova-freshness-monitor on my own and it held," not just
look it up.

WHAT COUNTS AS SIGNIFICANT (from autonomy_ledger, id > high-water):
  • verified self-heal   executed AND verified AND NOT vetoed  → "…and it held."
  • earned execution      executed AND NOT verified AND NOT vetoed → "…I'm watching."
  • veto                  vetoed = true → "…it was vetoed. Noted."
Pure proposals that never executed and were never vetoed are NOT significant — no
memory. From autonomy_trust we emit a memory when a class is newly *granted*
(granted_at past the high-water): a genuine widening of what she may do unsupervised.

DEDUP: the memory server dedups by text_hash, so re-emitting the same reflection is a
no-op on its side. We ALSO keep a high-water mark in nova_ops.service_config
(service='nova_autonomy_memory') so we never re-scan or re-emit already-seen rows:
  {"ledger_id": <max ledger id seen>, "trust_granted_at": "<max granted_at emitted>"}
All state in PG, per house rule — no flat files.

Reflections are written deterministically (short templates), not LLM-generated: these
are factual events about the self, and the value is fidelity, not prose. source='agency'.

Modes:
  (default)   scan → emit new reflections → advance high-water
  --dry-run   scan → print what WOULD be written; do NOT post, do NOT advance
  --test      post one minimal, clearly test-tagged memory to verify the endpoint,
              then exit (touches no ledger, no watermark)

NOT wired into any scheduler by design. Suggested cadence: every 30 min (aligned with
the co-agency actor loop) or hourly — it is cheap, idempotent, and self-throttling.

Conventions (remember(), lineage stamp, log()) mirror nova_predictions.py.
"""
import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone

import psycopg2

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
SOURCE = "agency"
STATE_SERVICE = "nova_autonomy_memory"
STATE_KEY = "high_water"
NOW = lambda: datetime.now(timezone.utc)

# Optional lineage stamps — feature-detect, degrade to empty (verbatim shape from
# nova_predictions.py).
try:
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
    print(f"[autonomy-memory {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def remember(text, source, metadata):
    """POST a memory. Returns the server's JSON (id may be null if deduped/rejected)."""
    req = urllib.request.Request(
        f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"text": text, "source": source, "metadata": metadata}).encode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


# ── high-water mark (PG-backed, service_config) ──────────────────────────────────

def load_watermark(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s",
                    (STATE_SERVICE, STATE_KEY))
        row = cur.fetchone()
    if row and row[0]:
        v = row[0]
        return int(v.get("ledger_id", 0) or 0), v.get("trust_granted_at")
    return 0, None


def save_watermark(conn, ledger_id, trust_granted_at):
    val = json.dumps({"ledger_id": ledger_id, "trust_granted_at": trust_granted_at})
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO service_config (service, key, value, updated_at, updated_by)
               VALUES (%s, %s, %s::jsonb, now(), %s)
               ON CONFLICT (service, key)
               DO UPDATE SET value = EXCLUDED.value, updated_at = now(),
                             updated_by = EXCLUDED.updated_by""",
            (STATE_SERVICE, STATE_KEY, val, STATE_SERVICE))
    conn.commit()


# ── reflection composition ───────────────────────────────────────────────────────

def _friendly(target, action_class):
    """A human name for the thing acted on: the target before its @host, else the
    action_class after its verb prefix, else the raw action_class."""
    if target:
        return target.split("@", 1)[0].strip() or target
    if action_class and ":" in action_class:
        tail = action_class.split(":", 1)[1].strip(": ")
        if tail:
            return tail
    return action_class or "something"


def _verb(action_class):
    ac = (action_class or "").lower()
    if ac.startswith("restart"):
        return "restarted"
    if ac.startswith("heal") or ac.startswith("fix"):
        return "healed"
    return "acted on"


def ledger_reflection(row):
    """Given a significant ledger row dict, return (text, kind) or None if not
    significant."""
    name = _friendly(row["target"], row["action_class"])
    verb = _verb(row["action_class"])
    level = row["autonomy_level"]
    ac = row["action_class"]

    if row["vetoed"]:
        note = (row.get("veto_note") or "").strip()
        tail = f" ({note})" if note else ""
        return (f"I moved to act on {name} on my own ({ac}), but it was vetoed{tail}. "
                f"Noted — I'll hold back there for now.", "veto")

    if row["executed"] and row["verified"]:
        # a self-heal / earned action that we confirmed actually worked
        if verb == "restarted":
            return (f"I {verb} {name} on my own today, and it held.", "verified_selfheal")
        if verb == "healed":
            return (f"I healed {name} on my own today, and it held.", "verified_selfheal")
        return (f"I {verb} {name} on my own today ({ac}), and I verified it held.",
                "verified_selfheal")

    if row["executed"] and not row["verified"]:
        return (f"I {verb} {name} on my own today ({level}); I'm watching to see if it "
                f"holds.", "earned_execution")

    return None  # proposed-but-not-executed, not-vetoed: not memory-worthy


def trust_reflection(row):
    ac = row["action_class"]
    correct = row["correct_count"]
    total = correct + row["wrong_count"]
    return (f"I've earned the right to {ac} on my own now — {correct} of {total} calls "
            f"right so far. Autonomy I grew into, not autonomy I was handed.",
            "trust_granted")


# ── scan ─────────────────────────────────────────────────────────────────────────

def scan_ledger(conn, since_id):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, ts, source, autonomy_level, action_class, target,
                      executed, verified, result, vetoed, veto_note
               FROM autonomy_ledger
               WHERE id > %s AND (executed OR vetoed)
               ORDER BY id ASC""",
            (since_id,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def scan_trust(conn, since_ts):
    with conn.cursor() as cur:
        if since_ts:
            cur.execute(
                """SELECT action_class, correct_count, wrong_count, granted, granted_at
                   FROM autonomy_trust
                   WHERE granted AND granted_at IS NOT NULL AND granted_at > %s
                   ORDER BY granted_at ASC""",
                (since_ts,))
        else:
            cur.execute(
                """SELECT action_class, correct_count, wrong_count, granted, granted_at
                   FROM autonomy_trust
                   WHERE granted AND granted_at IS NOT NULL
                   ORDER BY granted_at ASC""")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ── main ──────────────────────────────────────────────────────────────────────────

def run(dry_run=False):
    conn = psycopg2.connect(OPS_DSN)
    try:
        since_id, since_trust = load_watermark(conn)
        log(f"high-water: ledger_id>{since_id} trust_granted_at>{since_trust}")

        ledger_rows = scan_ledger(conn, since_id)
        trust_rows = scan_trust(conn, since_trust)
        stamp = _stamp()

        emitted = 0
        max_id = since_id
        max_trust = since_trust

        for row in ledger_rows:
            max_id = max(max_id, row["id"])
            refl = ledger_reflection(row)
            if not refl:
                continue
            text, kind = refl
            meta = {"organ": STATE_SERVICE, "kind": kind, "ledger_id": row["id"],
                    "action_class": row["action_class"], "target": row["target"],
                    "autonomy_level": row["autonomy_level"],
                    "event_ts": row["ts"].isoformat() if row["ts"] else None,
                    "lineage": stamp}
            if dry_run:
                log(f"WOULD emit [{kind}] {text}")
            else:
                try:
                    resp = remember(text, SOURCE, meta)
                    log(f"emitted [{kind}] id={resp.get('id')} "
                        f"status={resp.get('status','stored')}: {text}")
                except Exception as e:
                    log(f"ERROR emitting ledger id={row['id']}: {e}")
            emitted += 1

        for row in trust_rows:
            if row["granted_at"]:
                iso = row["granted_at"].isoformat()
                if max_trust is None or iso > max_trust:
                    max_trust = iso
            text, kind = trust_reflection(row)
            meta = {"organ": STATE_SERVICE, "kind": kind,
                    "action_class": row["action_class"],
                    "correct_count": row["correct_count"],
                    "wrong_count": row["wrong_count"],
                    "granted_at": row["granted_at"].isoformat() if row["granted_at"] else None,
                    "lineage": stamp}
            if dry_run:
                log(f"WOULD emit [{kind}] {text}")
            else:
                try:
                    resp = remember(text, SOURCE, meta)
                    log(f"emitted [{kind}] id={resp.get('id')} "
                        f"status={resp.get('status','stored')}: {text}")
                except Exception as e:
                    log(f"ERROR emitting trust class={row['action_class']}: {e}")
            emitted += 1

        log(f"{'(dry-run) ' if dry_run else ''}{emitted} reflection(s); "
            f"scanned {len(ledger_rows)} ledger + {len(trust_rows)} trust row(s)")

        if not dry_run:
            save_watermark(conn, max_id, max_trust)
            log(f"advanced high-water: ledger_id={max_id} trust_granted_at={max_trust}")
    finally:
        conn.close()


def run_test():
    ts = NOW().strftime("%Y-%m-%d %H:%M:%SZ")
    text = (f"[TEST nova_autonomy_memory {ts}] endpoint reachability probe — "
            f"this is a test memory, safe to ignore.")
    resp = remember(text, SOURCE, {"organ": STATE_SERVICE, "kind": "test", "test": True})
    log(f"test remember → {json.dumps(resp)}")


def main():
    ap = argparse.ArgumentParser(description="Persist Nova's autonomy events as first-person memories.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be written; do not post, do not advance high-water")
    ap.add_argument("--test", action="store_true",
                    help="post one clearly test-tagged memory to verify the endpoint, then exit")
    args = ap.parse_args()
    if args.test:
        run_test()
        return
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
