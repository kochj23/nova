#!/usr/bin/env python3
"""nova_identity_link.py — discover which unowned devices belong to which person.

Hand-labelling does not scale: there are 6,100 distinct BLE devices a week and three
mapped MACs. But ownership leaves a statistical fingerprint — your phone is present when
you are present and absent when you are not, and a neighbour's doorbell is not.

Method: bucket time into slots, build the set of slots each PERSON was present (from any
presence method) and each DEVICE was seen, then score every pair with the PHI COEFFICIENT
over the full 2x2 contingency table (present/absent x seen/unseen).

Phi, not support x specificity. The first attempt used the product of those two and every
always-on HomePod scored 0.99 against Jordan — because he was recorded present in 1,962 of
1,976 slots, and two things that are both always present correlate perfectly and mean
nothing. Phi accounts for base rates: a device scores only if it is present WITH the person
and ABSENT WITHOUT THEM.

The consequence is worth stating plainly: ALL the discriminating power lives in absence. A
person who never leaves cannot be learned from, and the script skips them by name rather
than emitting confident nonsense.

PROPOSES ONLY — writes to telemetry.identity_link_proposal and never to device_owner. A
wrong ownership link produces confidently wrong presence forever after, which is worse
than no link at all, so a human confirms. Same reasoning as the drill card: no body, no
clearance.
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import psycopg2

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
SLOT_MINUTES = 5
MIN_PERSON_SLOTS = 12      # a person needs real presence history before we infer anything
MIN_DEVICE_SLOTS = 6       # a device seen twice proves nothing
MIN_SCORE = 0.25          # phi: 0=independent, 1=perfectly co-present


def log(m):
    print(f"[identity-link] {m}", flush=True)


def main(days, apply_threshold, dry_run):
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS telemetry.identity_link_proposal (
            device_key   text NOT NULL,
            device_kind  text NOT NULL,
            person       text NOT NULL,
            support      real, specificity real, score real,
            device_slots int, person_slots int, overlap_slots int,
            evidence     jsonb,
            proposed_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (device_key, person)
        )""")

    # ── who was present, when ────────────────────────────────────────────────
    cur.execute(f"""
        SELECT person, floor(extract(epoch from ts)/{SLOT_MINUTES*60})::bigint
        FROM telemetry.presence
        WHERE ts > now() - interval '{days} days'
          AND person NOT IN ('unknown','occupant','motion','camera_detected',
                             'av_inferred','light_inferred','media_inferred')
        GROUP BY 1,2""")
    person_slots = defaultdict(set)
    for p, slot in cur.fetchall():
        person_slots[p].add(slot)
    person_slots = {p: s for p, s in person_slots.items() if len(s) >= MIN_PERSON_SLOTS}
    # A person present in ~every slot has no absence to discriminate against; any always-on
    # appliance will match them perfectly. Drop them and say why, rather than emit garbage.
    _total_slots = len(set().union(*person_slots.values())) if person_slots else 0
    for _p, _s in list(person_slots.items()):
        if _total_slots and len(_s) / _total_slots > 0.95:
            log(f"skipping '{_p}': present in {len(_s)}/{_total_slots} slots "
                f"({100*len(_s)/_total_slots:.0f}%) — too little absence to learn from")
            del person_slots[_p]
    log(f"people with usable presence history: "
        f"{ {p: len(s) for p, s in person_slots.items()} }")
    if not person_slots:
        log("no person has enough presence history yet — nothing to correlate")
        return 0

    # ── when each device was seen (fingerprint preferred; it survives MAC rotation) ──
    cur.execute(f"""
        SELECT coalesce(fingerprint, device_mac),
               CASE WHEN fingerprint IS NULL THEN 'mac' ELSE 'fingerprint' END,
               floor(extract(epoch from ts)/{SLOT_MINUTES*60})::bigint
        FROM telemetry.bluetooth
        WHERE ts > now() - interval '{days} days'
          AND device_mac <> '--:--:--:--:--:--'
          AND lower(coalesce(device_mac,'')) NOT IN
              (SELECT mac FROM telemetry.device_owner)
        GROUP BY 1,2,3""")
    dev_slots, dev_kind = defaultdict(set), {}
    for key, kind, slot in cur.fetchall():
        dev_slots[key].add(slot)
        dev_kind[key] = kind
    dev_slots = {k: v for k, v in dev_slots.items() if len(v) >= MIN_DEVICE_SLOTS}
    log(f"unowned devices with enough sightings: {len(dev_slots)}")

    # ── score every (device, person) pair ────────────────────────────────────
    # Support x specificity FAILS on base rates: measured 2026-07-27, Jordan was recorded
    # present in 1,962 of ~2,016 slots, so every always-on HomePod scored 0.99 against him.
    # Two things that are both always present correlate perfectly and mean nothing.
    # The PHI COEFFICIENT (Matthews) uses the full contingency table, so a device only
    # scores when it is present with the person AND ABSENT WITHOUT THEM. Discrimination
    # comes from absence — which also means a person who never leaves cannot be learned.
    import math
    all_slots = set()
    for sl in dev_slots.values():
        all_slots |= sl
    for sl in person_slots.values():
        all_slots |= sl
    total = len(all_slots)

    proposals = []
    for key, dslots in dev_slots.items():
        for person, pslots in person_slots.items():
            n11 = len(dslots & pslots)              # device seen,     person present
            n10 = len(dslots - pslots)              # device seen,     person absent
            n01 = len(pslots - dslots)              # device unseen,   person present
            n00 = total - n11 - n10 - n01           # neither
            if not n11:
                continue
            denom = math.sqrt((n11 + n10) * (n11 + n01) * (n00 + n10) * (n00 + n01))
            phi = ((n11 * n00 - n10 * n01) / denom) if denom else 0.0
            support = n11 / len(pslots)
            specificity = n11 / len(dslots)
            score = round(phi, 4)
            overlap = n11
            if score >= MIN_SCORE:
                proposals.append((key, dev_kind[key], person, support, specificity, score,
                                  len(dslots), len(pslots), overlap))
    proposals.sort(key=lambda r: -r[5])
    log(f"proposals above score {MIN_SCORE}: {len(proposals)}")

    for r in proposals[:15]:
        key, kind, person, sup, spec, score, ds, ps, ov = r
        flag = "  <-- would auto-apply" if score >= apply_threshold else ""
        log(f"  {score:.2f}  {person:8} <- {kind}:{key[:20]:22} "
            f"support={sup:.2f} spec={spec:.2f} overlap={ov}{flag}")

    if not dry_run and proposals:
        cur.executemany("""
            INSERT INTO telemetry.identity_link_proposal
              (device_key, device_kind, person, support, specificity, score,
               device_slots, person_slots, overlap_slots)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (device_key, person) DO UPDATE SET
              support=EXCLUDED.support, specificity=EXCLUDED.specificity,
              score=EXCLUDED.score, device_slots=EXCLUDED.device_slots,
              person_slots=EXCLUDED.person_slots, overlap_slots=EXCLUDED.overlap_slots,
              proposed_at=now()""", proposals)
        log(f"stored {len(proposals)} proposals (review, then insert into device_owner)")
    conn.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--apply-threshold", type=float, default=0.6,
                    help="score at which a link is confident enough to promote BY HAND")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    sys.exit(main(a.days, a.apply_threshold, a.dry_run))
