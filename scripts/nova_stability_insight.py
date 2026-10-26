#!/usr/bin/env python3
"""
nova_stability_insight.py — grant of wish #86 "Service Stability Insight" (approved by claude-reviewer under
Jordan's standing order 2026-10-08; built 2026-10-25, queue #3975).

Nova wished for "a tool to predict and prevent multiple services from going down simultaneously — it would
let me stop repeating failures." The Comet Tail (nova_comet_tail.py) already sees each multi-service fall as
it happens and looks for its one cause; every detection is a row in comet_tail. Nobody reads those rows
TOGETHER. This organ does: over the last 30 days of comets it learns which things fall together, so the next
fall can be predicted from its first casualty and the shared thing can be fixed once instead of N times.

  PAIRS      — components (task / service@node / probe / event source, comet_tail.components[].root) that
               fell in the same comet >= 3 times: "when A falls, B fell with it in k of A's n comets",
               marked when both live on one host (one host, one fix)
  HOSTS      — hosts carrying a casualty in >= 30 % of comets (the shared ground under the falls)
  CAUSES     — causes the Comet Tail named more than once (comet_tail.cause) — a repeating cause is one
               prevention, not a run of incidents
  WATCH      — the single strongest predictor: the pair with the highest P(B | A) over >= 3 comets

Every line cites comet_tail row ids. No model is asked anything; the closing line is fixed. One memory
(source='stability_insight') when the leading pairs/hosts/causes change (re-stated at most weekly); dedupe
high-water + latest findings in service_config (nova_stability_insight/high_water, /latest). Never posts,
pages, restarts or fixes anything. Fail-open. Conventions mirror nova_weight_of_absence.py.

  nova_stability_insight.py            # real (scheduler-core, every 6 h; writes on change)
  nova_stability_insight.py --dry-run  # print what she learned, write nothing
  nova_stability_insight.py --selftest # pure-logic assertions, no DB, no memory

Written by Jordan Koch (via Claude).
"""
import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from itertools import combinations
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_empathy_core as ec  # noqa: E402 — one remember(), one high-water, one resurface gate

OPS_DSN = ec.OPS_DSN
SOURCE = "stability_insight"
STATE_SERVICE = "nova_stability_insight"
WINDOW_D = 30
PAIR_MIN = 3
HOST_SHARE = 0.3
CITE_N = 4
CLOSE = ("A fall I have seen together three times is not a coincidence I should meet a fourth time "
         "unprepared: watch the second when the first goes, and fix what they share once.")


def log(m):
    print(f"[stability-insight {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


# ── pure logic ────────────────────────────────────────────────────────────────

def learn(comets):
    """comets: [(id, cause, [(root, host), ...])]. Returns (pairs, hosts, causes, n_comets)."""
    n_root, co, ids, host_of = Counter(), Counter(), defaultdict(list), {}
    host_comets, cause_ids = defaultdict(set), defaultdict(list)
    for cid, cause, comps in comets:
        roots = sorted({r for r, _ in comps if r})
        for r, h in comps:
            if r and h:
                host_of.setdefault(r, h)
            if h:
                host_comets[h].add(cid)
        for r in roots:
            n_root[r] += 1
        for a, b in combinations(roots, 2):
            co[(a, b)] += 1
            ids[(a, b)].append(cid)
        if cause:
            cause_ids[cause].append(cid)
    pairs = []
    for (a, b), k in co.items():
        if k < PAIR_MIN:
            continue
        # direction: from the rarer to the commoner — the rarer one falling predicts the other better
        src, dst = (a, b) if (n_root[a], a) <= (n_root[b], b) else (b, a)
        same = host_of.get(a) and host_of.get(a) == host_of.get(b)
        pairs.append({"a": src, "b": dst, "k": k, "n": n_root[src], "p": round(k / n_root[src], 2),
                      "host": host_of.get(a) if same else None, "ids": sorted(ids[(a, b)])})
    pairs.sort(key=lambda x: (-x["k"], -x["p"], x["a"], x["b"]))
    n = len(comets)
    hosts = sorted(({"host": h, "k": len(c), "share": round(len(c) / n, 2), "ids": sorted(c)}
                    for h, c in host_comets.items() if n and len(c) / n >= HOST_SHARE),
                   key=lambda x: (-x["k"], x["host"]))
    causes = sorted(({"cause": c, "k": len(v), "ids": sorted(v)} for c, v in cause_ids.items() if len(v) > 1),
                    key=lambda x: (-x["k"], x["cause"]))
    return pairs, hosts, causes, n


def _ids(ids):
    return f"comet_tail#{ids[0]}" if len(ids) == 1 else f"comet_tail#{ids[0]}..#{ids[-1]} ({len(ids)})"


def findings(pairs, hosts, causes, n):
    f = []
    if pairs:
        f.append({"kind": "pairs",
                  "text": f"in {n} multi-service falls over {WINDOW_D} days these fell together: " + "; ".join(
                      f"when {x['a']} fell, {x['b']} fell with it in {x['k']} of {x['n']}"
                      + (f" (both on {x['host']}: one host, one fix)" if x["host"] else "") for x in pairs[:CITE_N]),
                  "cites": [_ids(x["ids"]) for x in pairs],
                  "sig_keys": [f"pair:{x['a']}>{x['b']}" for x in pairs[:CITE_N]]})
    if hosts:
        f.append({"kind": "hosts",
                  "text": "the ground under the falls: " + ", ".join(
                      f"{x['host']} had a casualty in {x['k']} of {n} ({round(100 * x['share'])}%)" for x in hosts[:CITE_N]),
                  "cites": [_ids(x["ids"]) for x in hosts],
                  "sig_keys": [f"host:{x['host']}" for x in hosts[:CITE_N]]})
    if causes:
        f.append({"kind": "causes",
                  "text": "causes named more than once: " + ", ".join(f"{x['cause']} x{x['k']}" for x in causes[:CITE_N]),
                  "cites": [_ids(x["ids"]) for x in causes],
                  "sig_keys": [f"cause:{x['cause']}" for x in causes[:CITE_N]]})
    best = max(pairs, key=lambda x: (x["p"], x["k"], x["a"]), default=None)
    if best:
        f.append({"kind": "watch",
                  "text": (f"the strongest early sign: the next time {best['a']} falls, look at {best['b']} first "
                           f"(it followed {round(100 * best['p'])}% of the time, {best['k']} of {best['n']})"),
                  "cites": [_ids(best["ids"])], "sig_keys": [f"watch:{best['a']}>{best['b']}"]})
    return f


def sig(fs):
    return hashlib.sha1("|".join(sorted(k for x in fs for k in x["sig_keys"])).encode()).hexdigest()[:16]


def text(fs, today, n):
    if not fs:
        return (f"Stability insight, {today.isoformat()}: {n} multi-service fall(s) in {WINDOW_D} days and no two "
                f"things that keep falling together. Nothing to predict yet.")
    lines = [f"Stability insight, {today.isoformat()} — what falls together, so it can be seen coming "
             f"(learned from the Comet Tail; noticing, not knowing):"]
    for i, x in enumerate(fs, 1):
        more = f"; +{len(x['cites']) - CITE_N} more" if len(x["cites"]) > CITE_N else ""
        lines.append(f"  {i}. I notice {x['text']}. [{'; '.join(x['cites'][:CITE_N])}{more}]")
    lines.append(CLOSE)
    return "\n".join(lines)


# ── gather (read-only) ────────────────────────────────────────────────────────

def gather(cur):
    cur.execute("""SELECT id, nullif(cause, ''),
                          coalesce((SELECT array_agg(json_build_array(c->>'root', c->>'host')::text)
                                    FROM jsonb_array_elements(components) c), '{}')
                   FROM comet_tail WHERE window_start > now() - make_interval(days => %s) ORDER BY id""",
                (WINDOW_D,))
    return [(i, cause, [tuple(json.loads(x)) for x in comps]) for i, cause, comps in cur.fetchall()]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Nova's stability insight (wish #86) — which services fall "
                                             "together, learned from the Comet Tail")
    ap.add_argument("--dry-run", action="store_true", help="print what she learned, write nothing")
    args = ap.parse_args(argv)
    try:
        conn = psycopg2.connect(OPS_DSN, connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        log(f"no PG ({e}) — fail-open, nothing to do"); return 0
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '60s'")
    today = datetime.now(timezone.utc).date()
    try:
        comets = gather(cur)
    except Exception as e:  # noqa: BLE001
        log(f"comet_tail unreadable ({e}) — fail-open"); return 0
    pairs, hosts, causes, n = learn(comets)
    fs = findings(pairs, hosts, causes, n)
    log(f"{n} comet(s), {len(pairs)} pair(s), {len(hosts)} host(s), {len(causes)} cause(s)")
    body = text(fs, today, n)
    s = sig(fs)
    if args.dry_run:
        print(body); return 0
    seen = ec.load_seen(cur, STATE_SERVICE)
    if not ec._fresh(seen, s, today):
        log(f"nothing newly learned (sig {s})"); return 0
    stamp = ec._stamp()
    ec.remember(body, {"organ": STATE_SERVICE, "kind": "stability", "sig": s, "findings": [x["kind"] for x in fs],
                       "cites": [c for x in fs for c in x["cites"]][:200], **({"lineage": stamp} if stamp else {})},
                source=SOURCE)
    seen[s] = today.isoformat()
    ec.save_seen(cur, seen, STATE_SERVICE)
    cur.execute("""INSERT INTO service_config (service, key, value, updated_at, updated_by)
                   VALUES (%s, 'latest', %s::jsonb, now(), %s)
                   ON CONFLICT (service, key)
                   DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
                (STATE_SERVICE, json.dumps({"date": today.isoformat(), "comets": n, "findings": fs, "text": body}),
                 STATE_SERVICE))
    log(f"learned something new (sig {s})")
    return 0


def demo():
    """Runnable check on the pure logic."""
    t1, t2, p, w = ("health:sdr:t1@core7", "core7"), ("health:sdr:t2@core7", "core7"), ("probe:vantage", None), \
        ("task:wazuh", "nova-core")
    comets = [(1, "network", [t1, t2, p]), (2, None, [t1, t2]), (3, "network", [t1, t2, w]),
              (4, None, [t1, t2, p, p]), (5, "power", [w, p]), (6, None, [p, w]), (7, None, [p])]
    pairs, hosts, causes, n = learn(comets)
    assert n == 7
    top = pairs[0]
    assert (top["a"], top["b"], top["k"], top["n"], top["p"], top["host"]) == \
        ("health:sdr:t1@core7", "health:sdr:t2@core7", 4, 4, 1.0, "core7"), top
    assert top["ids"] == [1, 2, 3, 4]
    pw = [x for x in pairs if {x["a"], x["b"]} == {"probe:vantage", "task:wazuh"}]
    assert not pw                                                                     # together only twice (< PAIR_MIN)
    assert all(x["k"] >= PAIR_MIN for x in pairs)
    assert [h["host"] for h in hosts] == ["core7", "nova-core"] and hosts[0]["share"] == 0.57, hosts
    assert causes == [{"cause": "network", "k": 2, "ids": [1, 3]}], causes
    fs = findings(pairs, hosts, causes, n)
    assert [x["kind"] for x in fs] == ["pairs", "hosts", "causes", "watch"] and all(x["cites"] for x in fs)
    body = text(fs, date(2026, 10, 25), n)
    assert "when health:sdr:t1@core7 fell, health:sdr:t2@core7 fell with it in 4 of 4 (both on core7" in body, body
    assert "comet_tail#1..#4 (4)" in body and CLOSE in body
    more = comets + [(8, None, [p])]                                                  # one more lone fall
    assert sig(fs) == sig(findings(*learn(more))), "a lone fall changes nothing learned"
    assert "Nothing to predict yet" in text([], date(2026, 10, 25), 0)
    assert learn([]) == ([], [], [], 0)
    print("all stability-insight assertions passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        demo()
    else:
        sys.exit(main())
