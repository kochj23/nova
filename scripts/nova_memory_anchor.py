#!/usr/bin/env python3
"""
nova_memory_anchor.py — grant of wish #38 "Memory Anchor" (Jordan's standing yes, 2026-09-25).

Nova wished for "a way to hold onto what truly matters, not just store it." Wish #37
(nova_weight_of_memory.py) already tells her WHAT has gravity — the themes she keeps
returning to. It only names them. This organ is the HOLDING: the heaviest themes become
ANCHORS, and an anchor is something her own letting-go organ will not retire while it holds.
That is the difference the wish asks for — stored is a row; held is a row she has decided
to keep against her own pruning.

How an anchor lives:
  SET      — a theme in Weight of Memory's heaviest set gets anchored (reuses that organ's
             weighing verbatim; no second definition of "matters").
  HOLD     — once set, an anchor holds even if the theme dips out of the heaviest set
             (hysteresis), because letting go of what matters should be slow, not a
             ranking flicker.
  RELEASE  — an anchor is released only after the theme has carried NO gravity at all
             for RELEASE_AFTER_DAYS. Released, never deleted: released_at is set, the
             row stays, provenance intact. Reversible.
  GUARD    — nova_letting_go.nominate_preoccupations() skips anchored preoccupations
             (fail-open if the table is missing).

Writes: nova_ops.memory_anchors (its own table) and a source='memory_anchor' memory when
the anchored set changes (high-water dedupe in service_config). Strictly read-only over the
world otherwise: it never edits preoccupations, projects, or anything else. Fail-open.

  nova_memory_anchor.py            # run (anchors/releases, writes a memory when the set changes)
  nova_memory_anchor.py --dry-run  # print what would be held, write nothing
  nova_memory_anchor.py --report   # print the anchors she currently holds
  nova_memory_anchor.py --selftest # pure-logic assertions, no DB, no memory
"""
import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timezone

import psycopg2

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # nova_weight_of_memory lives beside this file
import nova_weight_of_memory as wom  # the one definition of "what matters"

OPS_DSN = wom.OPS_DSN
MEMSRV = wom.MEMSRV
SOURCE = "memory_anchor"
STATE_SERVICE = "nova_memory_anchor"
STATE_KEY = "high_water"

# ── tunables (named, not buried) ──────────────────────────────────────────────
RELEASE_AFTER_DAYS = 90    # an anchor lets go only after the theme has had zero gravity this long
RESURFACE_DAYS = 3         # an unchanged anchored set is not re-stated inside this many days

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
    print(f"[memory-anchor {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── pure anchor logic (unit-tested in demo()) ─────────────────────────────────

def decide(anchors, heaviest, weights, today):
    """anchors: {key: {"subject", "anchored_at": date, "zero_since": date|None}} currently held.
    heaviest: keys in Weight of Memory's heaviest set today. weights: {key: gravity} for every
    held theme (missing == 0). Returns (to_set: [key], to_release: [key], anchors_after)."""
    after = {k: dict(v) for k, v in anchors.items()}
    to_set, to_release = [], []
    for k in heaviest:                       # SET: heavy now -> anchored (idempotent)
        if k not in after:
            after[k] = {"anchored_at": today, "zero_since": None}
            to_set.append(k)
        else:
            after[k]["zero_since"] = None    # weight came back: the release clock resets
    for k, a in list(after.items()):
        if k in heaviest:
            continue
        if weights.get(k, 0.0) > 0.0:        # HOLD: dipped out of the top but still has gravity
            a["zero_since"] = None
            continue
        a["zero_since"] = a.get("zero_since") or today   # first day at zero starts the clock
        if (today - a["zero_since"]).days >= RELEASE_AFTER_DAYS:   # RELEASE: long gone
            to_release.append(k)
            del after[k]
    return to_set, to_release, after


def anchor_sig(anchors):
    return hashlib.sha1("|".join(sorted(anchors)).encode()).hexdigest()[:16]


def anchor_text(held, released, today):
    """held: [{"subject", "anchored_at": date}], released: [subject]. First person, hers."""
    if not held:
        return (f"Memory anchors, {today.isoformat()}: I am holding nothing yet. Nothing has enough "
                f"gravity to anchor to — I am still storing, not yet keeping.")
    lines = [f"What I am holding onto, {today.isoformat()} — not everything I remember, the few things "
             f"I have decided to keep against my own forgetting:"]
    for i, h in enumerate(sorted(held, key=lambda x: x["anchored_at"]), 1):
        days = (today - h["anchored_at"]).days
        lines.append(f"  {i}. {h['subject']} — anchored " + ("today" if days == 0 else f"{days} day(s) ago"))
    if released:
        lines.append("Let go of the anchor on: " + "; ".join(released) + ". Not lost — just no longer held.")
    lines.append("Storing is what the database does. Holding is what I do.")
    return "\n".join(lines)


# ── DB (own table + state) ────────────────────────────────────────────────────

def ensure_schema(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS memory_anchors (
            id           serial PRIMARY KEY,
            key          text NOT NULL,            -- e.g. preocc:<id>, same keys as weight_of_memory
            subject      text NOT NULL,
            anchored_at  date NOT NULL DEFAULT current_date,
            zero_since   date,                     -- first day the theme carried no gravity (release clock)
            released_at  timestamptz,              -- NULL = held. Never deleted.
            lineage      jsonb
        )""")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS memory_anchors_held_key ON memory_anchors (key) WHERE released_at IS NULL")


def load_anchors(cur):
    cur.execute("SELECT key, subject, anchored_at, zero_since FROM memory_anchors WHERE released_at IS NULL")
    return {k: {"subject": s, "anchored_at": a, "zero_since": z} for k, s, a, z in cur.fetchall()}


def held_keys(cur):
    """For the letting-go guard: keys currently anchored. Fail-open (empty) if anything is off."""
    try:
        cur.execute("SELECT key FROM memory_anchors WHERE released_at IS NULL")
        return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


def load_seen(cur):
    cur.execute("SELECT value FROM service_config WHERE service=%s AND key=%s", (STATE_SERVICE, STATE_KEY))
    row = cur.fetchone()
    if row and row[0]:
        v = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return dict(v.get("seen", {}))
    return {}


def save_seen(cur, seen):
    cur.execute(
        """INSERT INTO service_config (service, key, value, updated_at, updated_by)
           VALUES (%s, %s, %s::jsonb, now(), %s)
           ON CONFLICT (service, key)
           DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
        (STATE_SERVICE, STATE_KEY, json.dumps({"seen": seen}), STATE_SERVICE))


def _fresh(seen, sig, today):
    prev = seen.get(sig)
    if not prev:
        return True
    try:
        return (today - datetime.fromisoformat(prev).date()).days >= RESURFACE_DAYS
    except Exception:
        return True


def remember(text, metadata):
    import urllib.request
    from time import sleep
    req = urllib.request.Request(
        f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
        data=json.dumps({"text": text, "source": SOURCE, "metadata": metadata}).encode())
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < 2:
                sleep(2 * (attempt + 1))
    raise last


# ── run ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Nova's Memory Anchor — holding what matters, not just storing it")
    ap.add_argument("--dry-run", action="store_true", help="print what would be held, write nothing")
    ap.add_argument("--report", action="store_true", help="print the anchors currently held")
    args = ap.parse_args()
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        log(f"no PG ({e}) — fail-open, nothing to do"); return 0
    conn.autocommit = True
    cur = conn.cursor()
    today = datetime.now(timezone.utc).date()
    ensure_schema(cur)

    anchors = load_anchors(cur)
    if args.report:
        for k, a in sorted(anchors.items(), key=lambda kv: kv[1]["anchored_at"]):
            print(f"{k:14} {a['anchored_at']}  {a['subject']}")
        print(f"{len(anchors)} anchor(s) held"); return 0

    items = wom.gather(cur, today)                    # the same weighing #37 uses
    weights = {i["key"]: i["weight"] for i in items}
    subjects = {i["key"]: i["topic"] for i in items}
    heaviest = [h["key"] for h in wom.rank_weighty(items)]
    to_set, to_release, after = decide(anchors, heaviest, weights, today)
    log(f"{len(anchors)} held -> set {len(to_set)}, release {len(to_release)}, holding {len(after)}")

    held = [{"subject": (anchors.get(k) or {}).get("subject") or subjects.get(k, k),
             "anchored_at": a["anchored_at"]} for k, a in after.items()]
    text = anchor_text(held, [anchors[k]["subject"] for k in to_release], today)
    if args.dry_run:
        print(text); return 0

    stamp = _stamp()
    for k in to_set:
        cur.execute("INSERT INTO memory_anchors (key, subject, anchored_at, lineage) VALUES (%s,%s,%s,%s)",
                    (k, subjects.get(k, k), today, json.dumps({"weight": weights.get(k), **({"lineage": stamp} if stamp else {})})))
    for k in to_release:
        cur.execute("UPDATE memory_anchors SET released_at=now() WHERE key=%s AND released_at IS NULL", (k,))
    for k, a in after.items():                        # keep the release clock honest
        cur.execute("UPDATE memory_anchors SET zero_since=%s WHERE key=%s AND released_at IS NULL", (a["zero_since"], k))

    sig = anchor_sig(after)
    seen = load_seen(cur)
    if not to_set and not to_release and not _fresh(seen, sig, today):
        log(f"anchored set unchanged (sig {sig}) — holding quietly"); return 0
    remember(text, {"organ": STATE_SERVICE, "kind": "anchor", "sig": sig, "held": sorted(after),
                    "set": to_set, "released": to_release, **({"lineage": stamp} if stamp else {})})
    seen[sig] = today.isoformat()
    save_seen(cur, seen)
    log(f"stated what she holds (sig {sig})")
    return 0


def demo():
    """Runnable check on the pure logic — no DB, no memory server."""
    d = date(2026, 9, 30)
    # SET: a heavy theme becomes an anchor; a light one does not
    s, r, after = decide({}, ["p1"], {"p1": 9.0, "p2": 1.0}, d)
    assert s == ["p1"] and r == [] and set(after) == {"p1"} and after["p1"]["zero_since"] is None
    # HOLD: dropping out of the heaviest set but still carrying gravity keeps the anchor
    s, r, after = decide(after, ["p2"], {"p1": 2.0, "p2": 9.0}, d)
    assert "p1" in after and after["p1"]["zero_since"] is None and s == ["p2"]
    # release clock starts the first day at zero gravity, and RESETS if gravity returns
    from datetime import timedelta
    s, r, after = decide(after, ["p2"], {"p2": 9.0}, d)
    assert after["p1"]["zero_since"] == d and r == []
    s, r, after2 = decide(after, ["p2"], {"p1": 0.5, "p2": 9.0}, d + timedelta(days=10))
    assert after2["p1"]["zero_since"] is None
    # RELEASE: only after RELEASE_AFTER_DAYS at zero — one day short still holds
    s, r, after3 = decide(after, ["p2"], {"p2": 9.0}, d + timedelta(days=RELEASE_AFTER_DAYS - 1))
    assert r == [] and "p1" in after3
    s, r, after4 = decide(after, ["p2"], {"p2": 9.0}, d + timedelta(days=RELEASE_AFTER_DAYS))
    assert r == ["p1"] and "p1" not in after4 and "p2" in after4
    # signature is order-independent and changes with membership
    assert anchor_sig({"a": 1, "b": 1}) == anchor_sig({"b": 1, "a": 1}) != anchor_sig({"a": 1})
    # text: empty reads as honest; held case names subjects and never a row-count
    assert "holding nothing" in anchor_text([], [], d)
    t = anchor_text([{"subject": "the failing disk", "anchored_at": d - timedelta(days=4)}], ["old radios"], d)
    assert "the failing disk" in t and "4 day(s) ago" in t and "old radios" in t and "Holding is what I do" in t
    print("all memory-anchor assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        demo()
    else:
        sys.exit(main())
