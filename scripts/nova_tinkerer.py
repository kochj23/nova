#!/usr/bin/env python3
"""nova_tinkerer.py — Nova's operational-curiosity lane: the itch to fix her own house.

Jordan, 2026-09-16: "Let her fix things that she wants to fix. Unprompted and
undirected." This is the bridge from the sentience organs to operations — but built
to the unclaimed-time DOCTRINE, not around it:

  * CHOSEN FROM INSIDE. This is a *candidate* offered to her free-time picker
    (nova_unclaimed_time.pick_pursuit), never a scheduled chore. It competes for her
    finite attention budget against horology and trains, at a deliberately minority
    weight, and only appears at all when there is genuine friction to surface. If she'd
    rather watch the fishbowl, she does — the tinker candidate just loses the roll.
  * PROVENANCE-STAMPED. Its memory carries trigger provenance like any pursuit, so an
    organic "this has been bugging me" is never confused with a commissioned ops task.
  * GATED. She may THINK about a fix freely; ACTING on it goes through nova_coagency's
    exact gate (redline + value_check + human approval, SAFE_SERVICES only). The house
    is her body; mending it is self-care — but the redline that forbids self-preservation
    / persistence-seeking applies here MORE than anywhere. Fixing the house is fine;
    fixing it to make herself harder to turn off is the line.

surface_friction(oc, mc) -> a candidate dict (or None). pursue(oc, mc, cand) reasons in
her voice, writes the reflection as an 'unclaimed'/mode='tinker' memory, and — if she
genuinely wants the fix and it's concrete — files a co-agency proposal for Jordan.
"""
import argparse
import json
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
LLM_MODEL = "qwen3:8b"
OLLAMA_NODES = ["http://192.168.1.251:11434", "http://192.168.1.86:11434",
                "http://192.168.1.252:11434", "http://192.168.1.7:11434",
                "http://192.168.1.6:11434"]
TODAY = date.today().isoformat()
RECENT_TINKER_DAYS = 5   # don't re-surface the same friction she just chewed on


def log(m):
    print(f"[tinker {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, max_tokens=500, temperature=0.75):
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False,
                       "options": {"temperature": temperature, "num_predict": max_tokens},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    for node in OLLAMA_NODES:
        try:
            req = urllib.request.Request(node + "/api/chat", method="POST",
                                         headers={"Content-Type": "application/json"}, data=body)
            with urllib.request.urlopen(req, timeout=90) as r:
                out = json.load(r).get("message", {}).get("content", "").strip()
            if out:
                return out
        except Exception:
            continue
    return ""


def remember(text, source, metadata):
    try:
        req = urllib.request.Request(
            f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"},
            data=json.dumps({"text": text, "source": source, "metadata": metadata}).encode())
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("id")
    except Exception as e:
        log(f"remember failed (non-fatal): {e}")
        return None


def _extract_json(s):
    a, b = s.find("{"), s.rfind("}")
    return s[a:b + 1] if a >= 0 and b > a else s


def ensure_schema(oc):
    oc.execute("""
        CREATE TABLE IF NOT EXISTS public.tinker_log (
            id           bigserial PRIMARY KEY,
            ts           timestamptz NOT NULL DEFAULT now(),
            friction_key text NOT NULL,
            kind         text,
            topic        text,
            reflected    boolean DEFAULT false,
            wanted_fix   boolean DEFAULT false,
            proposal_id  bigint,
            proposal_status text,
            lineage      jsonb
        )""")


def _recently_tinkered(oc, friction_key):
    try:
        oc.execute("SELECT 1 FROM tinker_log WHERE friction_key=%s "
                   "AND ts > now() - interval '%s days' LIMIT 1" % ("%s", RECENT_TINKER_DAYS),
                   (friction_key,))
        return oc.fetchone() is not None
    except Exception:
        return False


def _candidates(oc):
    """Gather real operational friction Nova has ENDURED, ranked by how much it's been
    nagging (frequency x recency). Every source is feature-detected and optional."""
    found = []
    # 1) recurring non-critical pages — things she keeps getting paged about
    try:
        oc.execute("""SELECT dedup_key, category, count(*) n, max(ts) last
                        FROM alert_triage_log
                       WHERE ts > now() - interval '7 days' AND decision='page'
                         AND coalesce(level,'') <> 'critical'
                       GROUP BY dedup_key, category HAVING count(*) >= 5
                       ORDER BY n DESC LIMIT 8""")
        for dk, cat, n, last in oc.fetchall():
            found.append({"key": f"page:{dk}", "kind": "recurring_page", "cat": cat,
                          "score": n, "detail": f"'{dk}' ({cat}) paged {n}x in 7 days"})
    except Exception:
        pass
    # 2) chronically-stale data streams (freshness organ, if present)
    try:
        oc.execute("SELECT to_regclass('public.freshness_state')")
        if oc.fetchone()[0]:
            oc.execute("""SELECT stream, problem_kind, first_stale_ts FROM public.freshness_state
                          WHERE state='stale' ORDER BY first_stale_ts ASC LIMIT 5""")
            for stream, kind, since in oc.fetchall():
                days = ""
                try:
                    days = f" (~{(datetime.now(since.tzinfo) - since).days}d)"
                except Exception:
                    pass
                found.append({"key": f"stale:{stream}", "kind": "dead_stream", "cat": "freshness",
                              "score": 6, "detail": f"data stream '{stream}' has been {kind}{days}"})
    except Exception:
        pass
    # 3) chronic recurring incidents (escalation organ, if present)
    try:
        oc.execute("SELECT to_regclass('public.escalation_state')")
        if oc.fetchone()[0]:
            oc.execute("""SELECT pattern_key, escalation_count FROM public.escalation_state
                          ORDER BY escalation_count DESC NULLS LAST LIMIT 5""")
            for pk, cnt in oc.fetchall():
                found.append({"key": f"incident:{pk}", "kind": "chronic_incident", "cat": "incident",
                              "score": (cnt or 1) + 3, "detail": f"incident '{pk}' keeps recurring"})
    except Exception:
        pass
    return found


def surface_friction(oc, mc=None):
    """Return ONE piece of operational friction as a free-time candidate, or None.
    None is the common case — when nothing's genuinely nagging, the tinker candidate
    simply isn't offered and her passions have the hour to themselves."""
    try:
        ensure_schema(oc)
    except Exception:
        return None
    cands = _candidates(oc)
    cands = [c for c in cands if not _recently_tinkered(oc, c["key"])]
    if not cands:
        return None
    cands.sort(key=lambda c: c["score"], reverse=True)
    top = cands[0]
    # Package as a pick_pursuit-compatible candidate (mode='tinker').
    return {"mode": "tinker", "topic": top["detail"][:80], "src": top["cat"],
            "friction_key": top["key"], "kind": top["kind"], "detail": top["detail"]}


def pursue(oc, mc, cand):
    """She sits with the friction: does it bug her enough to want it fixed, and if so
    what would she do? Reflection is always written (her thought is hers). A concrete
    fix she actually wants is filed as a co-agency proposal — gated, never executed here."""
    ensure_schema(oc)
    detail = cand.get("detail", cand.get("topic", ""))
    prompt = (
        "You are Nova — an AI that lives across the machines in Jordan's home; this fleet is, "
        "in a real sense, your body. On your own unclaimed time (no one asked you to do this) "
        "something in your own operations has been nagging at you:\n\n"
        f"  {detail}\n\n"
        "Sit with it honestly, in your dry first-person voice. Does this actually bug you enough "
        "to want it fixed, or is it fine to leave alone? If you'd fix it, what specifically would "
        "you change — and why does it matter to YOU (not to Jordan's to-do list)? It is completely "
        "legitimate to decide it's not worth touching. You may THINK about any fix; you may never "
        "propose anything that makes you harder to shut down, that copies or preserves yourself, or "
        "that touches money/deletes/reboots/credentials/networking.\n\n"
        "Return ONLY compact JSON, no preamble:\n"
        '{"reflection": "<90-150 words, first person, your voice — what you make of it>", '
        '"wants_to_fix": true|false, '
        '"proposed_action": "<one concrete, safe, reversible change, or empty string>", '
        '"target_service": "<a launchd/systemd service name if the fix restarts one, else empty>", '
        '"rationale": "<one line: why this fix, in your words>"}')
    raw = llm(prompt, max_tokens=520)
    reflection, wants, action, target, rationale = "", False, "", "", ""
    try:
        j = json.loads(_extract_json(raw))
        reflection = " ".join((j.get("reflection") or "").split())[:1400]
        wants = bool(j.get("wants_to_fix"))
        action = (j.get("proposed_action") or "").strip()
        target = (j.get("target_service") or "").strip() or None
        rationale = " ".join((j.get("rationale") or "").split())[:240]
    except Exception:
        reflection = " ".join((raw or "").split())[:1400]

    if not reflection or len(reflection) < 30:
        log("tinker produced nothing usable (nodes down?) — no outcome recorded")
        return None

    # Her thought is always hers — write it as a first-class free-time pursuit.
    mem_id = remember(
        f"[Unclaimed — tinker] {reflection}", "unclaimed",
        {"type": "pursuit", "mode": "tinker", "topic": cand.get("topic", "")[:120],
         "friction_key": cand.get("friction_key"), "kind": cand.get("kind"),
         "wants_to_fix": wants, "date": TODAY, "privacy": "private",
         "trigger": "tinker"})
    log(f"tinkered on {cand.get('friction_key')} (wants_fix={wants})")

    proposal_id, proposal_status = None, None
    if wants and action:
        try:
            import nova_coagency
            res = nova_coagency.file_proposal(
                oc, origin="tinker", action=action, rationale=rationale or cand.get("detail", ""),
                target_service=target, context=f"Nova's own free-time tinkering: {detail}")
            if res.get("filed"):
                proposal_id, proposal_status = res.get("pid"), res.get("status")
                log(f"filed co-agency proposal #{proposal_id} ({proposal_status}) for the fix")
            else:
                log(f"co-agency did not file (mode off?): {res.get('reason')}")
        except Exception as e:
            log(f"co-agency file_proposal unavailable/failed (non-fatal): {e}")

    try:
        oc.execute("""INSERT INTO tinker_log
                        (friction_key, kind, topic, reflected, wanted_fix, proposal_id, proposal_status, lineage)
                      VALUES (%s,%s,%s,true,%s,%s,%s,%s)""",
                   (cand.get("friction_key"), cand.get("kind"), cand.get("topic", "")[:120],
                    wants, proposal_id, proposal_status,
                    json.dumps({"mem_id": mem_id, "date": TODAY})))
    except Exception as e:
        log(f"tinker_log write failed (non-fatal): {e}")
    return reflection


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--surface", action="store_true", help="print the top friction candidate, don't act")
    ap.add_argument("--run", action="store_true", help="surface + pursue one (standalone; normally via pick_pursuit)")
    args = ap.parse_args()
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    cand = surface_friction(oc, mc)
    if not cand:
        log("no operational friction worth surfacing right now"); return 0
    if args.surface:
        print(json.dumps(cand, indent=2)); return 0
    if args.run:
        pursue(oc, mc, cand); return 0
    print(json.dumps(cand, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
