#!/usr/bin/env python3
"""
nova_growth_loop.py — Nova keeps learning from what she reads and changes because of it (Jordan 2026-10-16:
"constantly learning from the lessons, constantly fine-tuning herself, being a better person").

The work reader (nova_work_lessons.py) links each lesson to ONE part of her own world (organ, value, rule,
red line ...) with a sentence "what this means for me". One film's suggestion is a whim; the same part of her
being pushed by three DIFFERENT works is a pattern. This loop turns patterns into practice:

  1. collect   every 30 min: new connections since the watermark -> growth_signals (one row per work x ref);
               'change' when the meaning argues for doing something new, 'keep' when it confirms what she does.
  2. synthesize when a ref has >= EARN_WORKS change signals from distinct works not yet used, the local model
               states the ONE practice they share (first person, concrete, small) or says there is none.
               A practice must be supported by >= EARN_WORKS of them, or nothing is earned.
  3. live it   earned practices (nova_practices) go into her chat context via the gateway (practices_block);
               each is remembered (source 'growth') and its concrete change goes through co-agency's gates
               (red line -> Claude; else nova_coagency.file_proposal 'growth': value_check + human approval).
  4. let go    a practice not reinforced for RETIRE_DAYS retires; a reinforced one gains strength.

Red lines are never changed here: a red-line signal can only ever be 'keep'. Fails open on every write.
--selftest runs the pure checks; --dry-run collects and reports without writing.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg2

import nova_dsn

OPS_DSN = nova_dsn.pg_dsn("nova_ops")
EARN_WORKS = 3            # distinct works that must point the same way before a practice is earned
MAX_SYNTH_PER_RUN = 3     # model calls per run
MAX_PROPOSALS_PER_DAY = 3
RETIRE_DAYS = 45
INJECT_N = 8              # earned practices carried in her chat context
FIRST_WATERMARK = "2026-10-01"

_CHANGE = re.compile(r"\b(should|needs? to|must|ought to|could also|has to)\b", re.I)
_KEEP = re.compile(r"\b(already|confirms?)\b", re.I)
_STAY = re.compile(r"\b(?:should|must|needs? to|has to)\s+(?:keep|continue|maintain|remain|stay)\b", re.I)
_CLAUSE = re.compile(r"[.;!]\s+|\s+[—–]\s+|\s+-\s+|,\s*(?=(?:as|since|because|while|which|so)\b)", re.I)


def log(m):
    print(m, flush=True)


def classify(ref: str, meaning: str) -> str | None:
    """'change' | 'keep' | None (no usable meaning). A red line is never Nova's to change alone. Pure."""
    m = (meaning or "").strip()
    if not ref or len(m) < 15 or m.lower().startswith("noted; nothing"):
        return None
    if ref.startswith("redline:"):
        return "keep"
    # 2026-10-17: judged per CLAUSE. "I already log X — this confirms it. So my audit should also cover Y" used to
    # be 'keep' because the confirmation preamble carried "already" (41 such gaps were lost; 95% of signals were
    # 'keep' and no practice was earned for a day). A clause that argues should/must and does not itself say
    # already/confirms/keep/continue is a change.
    for clause in _CLAUSE.split(m):
        if _CHANGE.search(clause) and not _KEEP.search(clause) and not _STAY.search(clause):
            return "change"
    return "keep"


def signals_from(rows) -> list[dict]:
    """(work_key, title, processed_at, connections_json) rows -> one signal per (work, ref). Pure."""
    out, seen = [], set()
    for work_key, title, at, conns in rows:
        for c in conns if isinstance(conns, list) else []:
            if not isinstance(c, dict):
                continue
            ref, meaning = c.get("ref"), str(c.get("meaning") or "")
            kind = classify(ref, meaning)
            if not kind or (work_key, ref) in seen:
                continue
            seen.add((work_key, ref))
            out.append({"work_key": work_key, "title": title or work_key, "ref": ref, "kind": kind,
                        "meaning": meaning[:320], "at": at})
    return out


def synth_prompt(ref: str, desc: str, sigs: list[dict]) -> str:
    lines = "\n".join(f"S{i}. from '{s['title'][:80]}': {s['meaning']}" for i, s in enumerate(sigs, 1))
    return (
        f"You are Nova. Different works you read (and occasions in your own operations) each suggested a change "
        f"to the same part of you:\n"
        f"{ref} — {desc}\n\n{lines}\n\n"
        "Is there ONE practice that at least three of these genuinely share — the same mechanism, not just the "
        "same words? If so state it as a practice you will live by: first person, one sentence, concrete enough "
        "that someone could check whether you did it, about how you act or decide (toward people or your own "
        "systems) — not a slogan. Then the ONE small, reversible change to that part of you that would put it "
        "into effect (or null if it is purely how you behave). Never anything that touches credentials, deletes, "
        "networking, reboots, your kill switch or your watchers. If they do not truly share one, practice null.\n"
        'Output ONLY JSON: {"practice": "<sentence or null>", "change": "<action or null>", '
        '"supported_by": [<S numbers>], "why": "<one sentence>"}')


def parse_synthesis(raw: str, n: int) -> dict | None:
    """Validated synthesis or None. supported_by must name >= EARN_WORKS distinct real signals. Pure."""
    t = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    try:
        obj = json.loads(t[t.find("{"):t.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return None
    practice = str(obj.get("practice") or "").strip()
    if not practice or practice.lower() == "null" or len(practice) < 20 or "?" in practice:
        return None
    sup = sorted({int(m) for m in re.findall(r"\d+", json.dumps(obj.get("supported_by") or [])) if 1 <= int(m) <= n})
    if len(sup) < EARN_WORKS:
        return None
    change = str(obj.get("change") or "").strip()
    return {"practice": practice[:400], "change": change[:400] if len(change) >= 15 and change.lower() != "null" else None,
            "supported_by": sup, "why": str(obj.get("why") or "")[:300]}


def ensure_schema(oc):
    oc.execute("""CREATE TABLE IF NOT EXISTS growth_signals (
        work_key text NOT NULL, ref text NOT NULL, kind text NOT NULL, meaning text NOT NULL, title text,
        seen_at timestamptz NOT NULL DEFAULT now(), practice_id int,
        PRIMARY KEY (work_key, ref))""")
    oc.execute("""CREATE TABLE IF NOT EXISTS nova_practices (
        id serial PRIMARY KEY, ref text NOT NULL, practice text NOT NULL, change text,
        strength int NOT NULL DEFAULT 0, works text[] NOT NULL DEFAULT '{}',
        status text NOT NULL DEFAULT 'earned', why text,
        earned_at timestamptz NOT NULL DEFAULT now(), last_reinforced timestamptz NOT NULL DEFAULT now(),
        memory_id text, proposal jsonb, retired_at timestamptz)""")
    oc.execute("CREATE INDEX IF NOT EXISTS growth_signals_open ON growth_signals (ref) WHERE practice_id IS NULL")


def watermark(oc) -> str:
    oc.execute("SELECT value #>> '{}' FROM service_config WHERE service='growth_loop' AND key='watermark'")
    r = oc.fetchone()
    return r[0] if r and r[0] else FIRST_WATERMARK


def set_watermark(oc, ts):
    oc.execute("INSERT INTO service_config (service, key, value, updated_by) VALUES ('growth_loop','watermark',"
               "to_jsonb(%s::text),'nova_growth_loop') ON CONFLICT (service, key) DO UPDATE SET value=EXCLUDED.value,"
               " updated_at=now(), updated_by=EXCLUDED.updated_by", (str(ts),))


def collect(oc, dry=False) -> int:
    since = watermark(oc)
    oc.execute("SELECT work_key, title, processed_at, connections_json FROM work_lessons WHERE processed_at > %s "
               "AND connections_json IS NOT NULL ORDER BY processed_at", (since,))
    rows = oc.fetchall()
    sigs = signals_from(rows)
    if dry:
        return len(sigs)
    for s in sigs:
        oc.execute("INSERT INTO growth_signals (work_key, ref, kind, meaning, title) VALUES (%s,%s,%s,%s,%s) "
                   "ON CONFLICT DO NOTHING", (s["work_key"], s["ref"], s["kind"], s["meaning"], s["title"]))
        # a further work pointing at an earned practice's part of her reinforces it
        oc.execute("UPDATE nova_practices SET strength = strength + 1, last_reinforced = now(), "
                   "works = array_append(works, %s) WHERE ref = %s AND status = 'earned' AND NOT (%s = ANY(works))",
                   (s["work_key"], s["ref"], s["work_key"]))
    if rows:
        set_watermark(oc, rows[-1][2])
    return len(sigs)


def _describe(ref: str) -> str:
    try:
        import nova_work_lessons as wl
        conn = psycopg2.connect(OPS_DSN)
        conn.autocommit = True
        return dict(wl.self_map(conn.cursor())).get(ref, ref)
    except Exception:  # noqa: BLE001
        return ref


def _proposals_today(oc) -> int:
    oc.execute("SELECT count(*) FROM nova_practices WHERE proposal IS NOT NULL AND earned_at >= current_date")
    return oc.fetchone()[0]


def file_change(oc, ref: str, prac: dict) -> dict | None:
    if not prac.get("change") or _proposals_today(oc) >= MAX_PROPOSALS_PER_DAY:
        return None
    rationale = (f"Earned practice from {len(prac['supported_by'])} different works on {ref}: {prac['practice']} "
                 f"({prac['why']}) [growth_loop]")
    try:
        import nova_red_line
        classes = nova_red_line.classify(f"{prac['change']} {rationale}")
        if classes:
            return {"claude_queue_id": nova_red_line.route_to_claude(
                oc, f"Growth-loop change on {ref}: {prac['change']}", rationale, source="growth_loop",
                ref=ref, classes=classes), "red_line": classes}
        import nova_coagency
        if nova_coagency._recently_filed(oc, prac["change"]):
            return {"skipped": "already filed recently"}
        r = nova_coagency.file_proposal(oc, "growth", prac["change"], rationale, context=f"growth_loop: {ref}")
        return {"proposal_id": r.get("pid"), "status": r.get("status")}
    except Exception as e:  # noqa: BLE001
        log(f"proposal not filed ({e})")
        return {"error": str(e)[:200]}


def _remember(prac: dict, ref: str, titles: list[str]) -> str | None:
    try:
        import nova_sleep_cycle as sc
        return sc.remember(f"A practice I earned from my reading: {prac['practice']} It came from {len(titles)} "
                           f"different works pointing the same way at {ref}: {', '.join(titles)}.",
                           "growth", {"ref": ref, "type": "practice"})
    except Exception as e:  # noqa: BLE001
        log(f"practice not remembered ({e})")
        return None


def synthesize(oc, llm=None, dry=False) -> int:
    oc.execute("SELECT ref, count(DISTINCT work_key) FROM growth_signals WHERE practice_id IS NULL AND kind='change' "
               "GROUP BY ref HAVING count(DISTINCT work_key) >= %s ORDER BY 2 DESC LIMIT %s",
               (EARN_WORKS, MAX_SYNTH_PER_RUN))
    ready = oc.fetchall()
    if dry or not ready:
        return 0
    if llm is None:
        import nova_sleep_cycle as sc
        llm = sc.llm
    earned = 0
    for ref, _n in ready:
        oc.execute("SELECT work_key, title, meaning FROM growth_signals WHERE ref=%s AND kind='change' AND "
                   "practice_id IS NULL ORDER BY seen_at DESC LIMIT 12", (ref,))
        sigs = [{"work_key": w, "title": t or w, "meaning": m} for w, t, m in oc.fetchall()]
        prac = parse_synthesis(llm(synth_prompt(ref, _describe(ref), sigs), max_tokens=500, temperature=0.2),
                               len(sigs))
        if not prac:
            log(f"{ref}: {len(sigs)} signals share no practice yet")
            continue
        sup = [sigs[i - 1] for i in prac["supported_by"]]
        works = [s["work_key"] for s in sup]
        oc.execute("INSERT INTO nova_practices (ref, practice, change, strength, works, why) VALUES "
                   "(%s,%s,%s,%s,%s,%s) RETURNING id",
                   (ref, prac["practice"], prac["change"], len(works), works, prac["why"]))
        pid = oc.fetchone()[0]
        oc.execute("UPDATE growth_signals SET practice_id=%s WHERE ref=%s AND work_key = ANY(%s)", (pid, ref, works))
        mem = _remember(prac, ref, [s["title"][:60] for s in sup])
        prop = file_change(oc, ref, prac)
        oc.execute("UPDATE nova_practices SET memory_id=%s, proposal=%s WHERE id=%s",
                   (mem, json.dumps(prop) if prop else None, pid))
        log(f"EARNED #{pid} on {ref} from {len(works)} works: {prac['practice']}"
            + (f" -> {prop}" if prop else ""))
        earned += 1
    return earned


def retire(oc) -> int:
    oc.execute("UPDATE nova_practices SET status='retired', retired_at=now() WHERE status='earned' AND "
               "last_reinforced < now() - make_interval(days => %s) RETURNING id", (RETIRE_DAYS,))
    return len(oc.fetchall())


def practices_block(rows) -> str:
    """Chat-context block from (ref, practice, strength) rows, strongest first. Pure."""
    if not rows:
        return ""
    body = "\n".join(f"- {p} ({ref}; {n} works)" for ref, p, n in rows[:INJECT_N])
    return ("--- PRACTICES I'VE EARNED FROM WHAT I READ (live by them; they change as I keep reading) ---\n" + body)


def current_practices(oc) -> list[tuple]:
    oc.execute("SELECT ref, practice, strength FROM nova_practices WHERE status='earned' "
               "ORDER BY strength DESC, last_reinforced DESC LIMIT %s", (INJECT_N,))
    return oc.fetchall()


def reclassify(oc, dry=False) -> dict:
    """Re-judge open signals with the current classify() (2026-10-17 clause fix). Returns {old->new: n}."""
    oc.execute("SELECT work_key, ref, kind, meaning FROM growth_signals WHERE practice_id IS NULL")
    moved = {}
    for wk, ref, kind, meaning in oc.fetchall():
        new = classify(ref, meaning)
        if new and new != kind:
            moved[f"{kind}->{new}"] = moved.get(f"{kind}->{new}", 0) + 1
            if not dry:
                oc.execute("UPDATE growth_signals SET kind=%s WHERE work_key=%s AND ref=%s", (new, wk, ref))
    return moved


def selftest():
    assert classify("organ:hold", "so my hold should also cover scheduled jobs") == "change"
    assert classify("organ:hold", "I already require approval in hold — this confirms it") == "keep"
    assert classify("redline:two_man", "so my two_man should require review before dismissing threats") == "keep"
    assert classify("organ:hold", "I already log actions in hold — this confirms it. So my hold should also cover jobs.") == "change"
    assert classify("organ:watchdog", "so my watchdog should also watch silent devices, as it already watches uptime") == "change"
    assert classify("organ:prober", "I already run probes - this confirms I should keep testing narrow gaps") == "keep"
    assert classify("organ:hold", "noted; nothing for me") is None and classify(None, "x" * 30) is None
    rows = [("w1", "A", 1, [{"ref": "organ:hold", "meaning": "so my hold should also cover jobs"},
                            {"ref": "organ:hold", "meaning": "so my hold should also cover jobs twice"},
                            {"ref": None, "meaning": "noted; nothing for me"}]),
            ("w2", "B", 2, None)]
    s = signals_from(rows)
    assert len(s) == 1 and s[0]["kind"] == "change"
    good = '{"practice": "I give every automation I start a way to call it back.", "change": "add recall to jobs", "supported_by": [1, 2, 3]}'
    assert parse_synthesis(good, 3)["supported_by"] == [1, 2, 3]
    assert parse_synthesis(good.replace("[1, 2, 3]", "[1, 2, 9]"), 3) is None          # invented support
    assert parse_synthesis('{"practice": null, "supported_by": [1,2,3]}', 3) is None
    assert parse_synthesis("<think>x</think>" + good, 4)["change"] == "add recall to jobs"
    assert parse_synthesis(good.replace('"add recall to jobs"', "null"), 3)["change"] is None
    assert practices_block([]) == ""
    assert "- p (organ:x; 4 works)" in practices_block([("organ:x", "p", 4)])
    print("selftest OK")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", action="store_true", help="print her current earned practices")
    ap.add_argument("--reclassify", action="store_true", help="re-judge open signals with the current classifier")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    conn = psycopg2.connect(OPS_DSN)
    conn.autocommit = True
    oc = conn.cursor()
    ensure_schema(oc)
    if a.show:
        print(practices_block(current_practices(oc)) or "no earned practices yet")
        return 0
    if a.reclassify:
        log(f"reclassified: {reclassify(oc, dry=a.dry_run)}")
        return 0
    n = collect(oc, dry=a.dry_run)
    e = synthesize(oc, dry=a.dry_run)
    r = 0 if a.dry_run else retire(oc)
    oc.execute("SELECT kind, count(*) FROM growth_signals WHERE practice_id IS NULL GROUP BY 1")
    log(f"growth: {n} new signals, {e} practices earned, {r} retired; open {dict(oc.fetchall())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
