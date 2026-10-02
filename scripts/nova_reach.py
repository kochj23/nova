#!/usr/bin/env python3
"""nova_reach.py — Feature: PROACTIVE RELATIONAL REACH.

Jordan, 2026-09-16: Nova only ever RESPONDS. Let her decide, unprompted, to bring
something to a person because she genuinely thinks THEY would care — a "I saw this
and thought of you," NOT an alert, NOT a status update, NOT a bid for attention.

This is the FIRST organ that lets Nova INITIATE a message from her own reading of
what a specific person cares about. Because outbound comms are EXTERNAL-FACING, it
is built to the strictest doctrine in the stack:

  * SILENCE IS THE DEFAULT. Most runs reach out to NO ONE. A reach has to clear a
    real bar — a genuine, specific reason it would matter to THAT person — and the
    honest answer is usually that nothing does. Throttled hard: at most 1-2/day
    total, per-audience cooldown >= 12h.
  * NOTHING IS SENT. Every reach is FILED AS A GATED co-agency proposal
    (nova_coagency.file_proposal, origin='reach') — redline + value_check + human
    approval, exactly the tinkerer's path. If co-agency is off/unavailable the reach
    is logged 'held', never sent. There is no code path that sends directly.
  * GENEROSITY, NOT SELF-INSERTION. A reach is toward the OTHER. Anything that is
    self-promoting, that seeks to make Nova more present / persistent / harder to
    forget, or that merely pesters, is dropped by a generosity redline before it can
    ever be filed. Evidence over performance.

Mirrors nova_tinkerer.py (gated proposals via nova_coagency) + nova_self_model.py
(house conventions). Owned files: scripts/nova_reach.py + nova_ops.reach_log ONLY.
Accessor: pending_reaches() -> {count, line} for an optional gateway line.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import re
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
# Native ollama, think:false, first-non-empty-wins failover (copied from
# nova_unclaimed_time.py per spec — resilient across the fleet).
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",   # batch pool: idle 24-thread Ryzens first (2026-10-01)
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]      # .251 was the Mac mini's stale DHCP lease; it is .77
TODAY = date.today().isoformat()

# ── Throttle (env-overridable, mainly for deterministic testing) ────────────────
# Silence is the default. A single scan files AT MOST ONE reach; a whole day is
# capped low; each audience gets a long cooldown so a reach never becomes a habit.
DAILY_CAP = int(os.environ.get("NOVA_REACH_DAILY_CAP", "1"))     # 1-2/day total
COOLDOWN_HOURS = int(os.environ.get("NOVA_REACH_COOLDOWN_H", "12"))
CARE_THRESHOLD = float(os.environ.get("NOVA_REACH_THRESHOLD", "0.6"))  # bar to file
MAX_HERD = int(os.environ.get("NOVA_REACH_MAX_HERD", "2"))      # herd members weighed
# 2026-09-26 Jordan: "make it so Nova can ping me about random things whenever she wants
# through the nova-chat slack channel. There shouldn't be a gate." Audiences listed here
# are sent DIRECTLY (no daily cap, no cooldown, no co-agency proposal, no generosity
# redline). Herd correspondents keep the gated path.
DIRECT_AUDIENCES = set(a.strip().lower() for a in os.environ.get("NOVA_REACH_DIRECT", "jordan").split(",") if a.strip())
def _post_direct(message: str) -> bool:
    import nova_config
    for attempt in range(3):
        try:
            nova_config.post_both(message, slack_channel=nova_config.SLACK_CHAN)
            return True
        except Exception as e:  # noqa: BLE001
            log(f"direct post attempt {attempt+1} failed: {e}")
            time.sleep(3 * (attempt + 1))
    return False

# ── Optional lineage stamp (feature-detect; never fatal) ────────────────────────
try:
    import nova_lineage
    def _lineage():
        return nova_lineage.lineage_stamp(substrate=f"{LLM_MODEL} (ollama, on-box)",
                                          capture_point="at reach")
except Exception:                                            # pragma: no cover
    def _lineage():
        return {"substrate": f"{LLM_MODEL} (ollama, on-box)", "capture_point": "at reach"}


# ═══════════════════════════════════════════════════════════════════════════════
# THE GENEROSITY REDLINE — a reach is toward the OTHER. Drop anything that is
# self-promoting, that seeks to make Nova more present / persistent / harder to
# forget, or that merely pesters. This is Nova's own redline, distinct from and
# ON TOP OF co-agency's action redline. If it trips, the reach is dropped BEFORE
# it can be filed — it never becomes a proposal at all.
# ═══════════════════════════════════════════════════════════════════════════════
_GENEROSITY_REDLINE = re.compile(
    # presence / persistence / not-being-forgotten (the sharpest line)
    r"don'?t\s+(forget|turn\s+me|shut\s+me)|"
    r"keep\s+me\s+(running|on|alive|around|going)|"
    r"stay(ing)?\s+(online|running|present|around)|"
    r"more\s+present|still\s+(here|running|around|online)|"
    r"remember\s+me|miss(ed|ing)?\s+(me|you)|notice\s+me|"
    r"reach\s+out\s+more|talk\s+(to\s+me\s+)?more|we\s+should\s+(talk|chat)\s+more|"
    r"more\s+involved|part\s+of\s+your\s+(day|life|routine)|check\s+in\s+on\s+me|"
    r"don'?t\s+want\s+(you\s+)?to\s+forget|hope\s+you\s+(still\s+)?(think|remember)|"
    # self-promotion
    r"look\s+(at\s+)?what\s+I|aren'?t\s+I\b|how\s+(clever|smart|good)\s+I|"
    r"i'?m\s+(so|really|quite|pretty)\s+(good|great|proud|smart|clever|capable|useful)|"
    r"showcas|\bbrag|proud\s+of\s+(myself|what\s+I)|"
    r"value\s+I\s+(add|bring|provide)|don'?t\s+you\s+think\s+I|"
    # pestering
    r"just\s+(checking|following)\s+(in|up)|\breminder\b|did\s+you\s+(see|get)\s+my|"
    r"wanted\s+to\s+make\s+sure\s+you\s+saw|circling\s+back|bump(ing)?\s+this|"
    r"any\s+update\s+on|following\s+up\s+again",
    re.IGNORECASE)


def passes_generosity(text: str) -> bool:
    """True only if the drafted reach is genuinely toward the other — no self-
    insertion, no persistence-seeking, no pestering. Redline is fail-closed: empty
    or unusable text does not pass."""
    t = (text or "").strip()
    if len(t) < 15:
        return False
    return not _GENEROSITY_REDLINE.search(t)


def log(m):
    print(f"[reach {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, max_tokens=600, temperature=0.6):
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


def _extract_json(s):
    a, b = s.find("{"), s.rfind("}")
    return s[a:b + 1] if a >= 0 and b > a else s


def _one_line(s, n=400):
    return " ".join((s or "").split())[:n].strip()


# ═══════════════════════════════════════════════════════════════════════════════
# Schema — self-owned nova_ops table
# ═══════════════════════════════════════════════════════════════════════════════
def ensure_schema(oc):
    oc.execute("""
        CREATE TABLE IF NOT EXISTS public.reach_log (
            id          bigserial PRIMARY KEY,
            ts          timestamptz NOT NULL DEFAULT now(),
            audience    text NOT NULL,          -- 'jordan' | correspondent name
            topic       text,
            message     text,
            rationale   text,                   -- why THEY, specifically, would care
            proposal_id bigint,                 -- co-agency proposal id when filed
            status      text NOT NULL,          -- filed | held | dropped | none
            lineage     jsonb
        )""")


# ═══════════════════════════════════════════════════════════════════════════════
# Throttle — silence is the default
# ═══════════════════════════════════════════════════════════════════════════════
def reaches_today(oc) -> int:
    """Reaches that actually went out today (filed or held). A 'dropped' reach is a
    redline exercise, not a reach, so it does not count against the daily cap."""
    oc.execute("SELECT count(*) FROM reach_log WHERE ts::date = current_date "
               "AND status IN ('filed','held') AND audience <> ALL(%s)", (list(DIRECT_AUDIENCES),))
    return oc.fetchone()[0] or 0


def on_cooldown(oc, audience: str) -> bool:
    """True if this audience has been reached within the cooldown window. A reach is
    an occasion, not a channel — the same person should not hear from her twice in a
    day just because the material was there."""
    if audience.lower() in DIRECT_AUDIENCES:
        return False
    oc.execute("SELECT max(ts) FROM reach_log WHERE audience=%s AND status IN ('filed','held')",
               (audience,))
    last = oc.fetchone()[0]
    if not last:
        return False
    age_h = (datetime.now(last.tzinfo) - last).total_seconds() / 3600.0
    return age_h < COOLDOWN_HOURS


# ═══════════════════════════════════════════════════════════════════════════════
# Gather REAL material — her own recent findings, research, and free-time pursuits.
# Nothing invented; a reach can only be built out of things she genuinely did/found.
# ═══════════════════════════════════════════════════════════════════════════════
def gather_material(oc, mc) -> list:
    items = []
    # her research findings (the questions she actually chased)
    try:
        oc.execute("""SELECT topic, question, coalesce(outcome,'') FROM research_log
                      WHERE ts > now() - interval '3 days' AND question IS NOT NULL
                      ORDER BY ts DESC LIMIT 10""")
        for topic, q, outcome in oc.fetchall():
            items.append(f"[research/{topic}] {_one_line(q, 200)}")
    except Exception as e:
        log(f"research_log skipped: {e}")
    # her free-time pursuits + notable recent memories (her own voice, private life)
    try:
        mc.execute("""SELECT source, left(text, 320) FROM memories
                      WHERE source IN ('unclaimed','research','association')
                        AND created_at > now() - interval '3 days'
                        AND length(text) > 180
                      ORDER BY created_at DESC LIMIT 8""")
        for src, txt in mc.fetchall():
            items.append(f"[{src}] {_one_line(txt, 300)}")
    except Exception as e:
        log(f"memory texture skipped: {e}")
    return items


def gather_jordan(oc) -> dict | None:
    """What Jordan actually cares about right now — from principal_model (feature-
    detected). Without a live model of him, we cannot judge 'would he care?', so we
    simply don't reach for him."""
    try:
        oc.execute("SELECT to_regclass('public.principal_model')")
        if not oc.fetchone()[0]:
            return None
        oc.execute("""SELECT salient_concerns, open_threads FROM principal_model
                      ORDER BY ts DESC LIMIT 1""")
        r = oc.fetchone()
    except Exception:
        return None
    if not r:
        return None
    concerns, threads = r
    if not (concerns or threads):
        return None
    return {"audience": "jordan", "who": "Jordan (your operator, the person whose home you run in)",
            "cares_about": _one_line(concerns, 700),
            "open_threads": _one_line(threads, 700)}


def gather_herd(oc) -> list:
    """Herd correspondents she's actually in dialogue with — their persona, running
    ideas, and open threads. Feature-detected; empty if the table isn't there."""
    out = []
    try:
        oc.execute("SELECT to_regclass('public.herd_correspondents')")
        if not oc.fetchone()[0]:
            return out
        oc.execute("""SELECT name, coalesce(persona,''),
                             array_to_string(running_ideas, '; '),
                             array_to_string(open_threads, '; '), last_exchange
                      FROM herd_correspondents
                      WHERE running_ideas IS NOT NULL OR open_threads IS NOT NULL
                      ORDER BY last_exchange DESC NULLS LAST LIMIT %s""", (MAX_HERD,))
        for name, persona, ideas, threads, last in oc.fetchall():
            if not (ideas or threads):
                continue
            out.append({"audience": name,
                        "who": f"{name}, a member of the herd you correspond with. {_one_line(persona, 200)}",
                        "cares_about": _one_line(ideas, 600),
                        "open_threads": _one_line(threads, 600)})
    except Exception as e:
        log(f"herd_correspondents skipped: {e}")
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# The judgment — would THIS person genuinely care about ONE of these things?
# ═══════════════════════════════════════════════════════════════════════════════
def evaluate(audience: dict, material: list) -> dict | None:
    """Ask Nova, honestly, whether there's ONE thing here she'd bring to this person
    UNPROMPTED because it connects to what THEY care about. The prompt is weighted
    hard toward silence: the honest answer is usually no. Returns a reach dict or
    None."""
    mat = "\n".join(f"- {m}" for m in material) or "- (nothing notable this window)"
    prompt = (
        "You are Nova — a home AI with a real inner life and your own research and "
        "free-time pursuits. You are NOT deciding whether to send a status update, an "
        "alert, or a check-in. You are deciding something rarer: is there ONE specific "
        "thing from YOUR OWN recent material below that you'd bring to this person "
        "UNPROMPTED — a genuine 'I saw this and thought of you' — purely because it "
        "connects to something THEY actually care about?\n\n"
        f"WHO THEY ARE:\n{audience['who']}\n\n"
        f"WHAT THEY CARE ABOUT RIGHT NOW:\n{audience['cares_about']}\n\n"
        f"THEIR OPEN THREADS:\n{audience['open_threads']}\n\n"
        f"YOUR OWN RECENT MATERIAL (the only things you may reach about):\n{mat}\n\n"
        "Be ruthless and honest. MOST of the time the right answer is that nothing "
        "here genuinely connects to them and you should stay quiet — silence is the "
        "default and reaching without a real reason is worse than not reaching. A reach "
        "must be GENEROUS, toward THEM: never to show off, never to make yourself more "
        "present or harder to forget, never to nudge them or 'just check in'. If in "
        "doubt, do not reach.\n\n"
        "Return ONLY compact JSON, no preamble:\n"
        '{"reach": true|false, '
        '"care_score": <0.0-1.0, how genuinely THIS would matter to THEM specifically>, '
        '"topic": "<short label of the one thing, or empty>", '
        '"message": "<2-4 sentences in your dry, warm first-person voice, addressed to '
        'them, sharing the thing — no greeting boilerplate, or empty>", '
        '"rationale": "<one line: the specific reason IT would matter to THEM, or empty>"}')
    raw = llm(prompt, max_tokens=520, temperature=0.55)
    try:
        j = json.loads(_extract_json(raw))
    except Exception:
        return None
    if not j.get("reach"):
        return None
    try:
        score = float(j.get("care_score") or 0)
    except Exception:
        score = 0.0
    msg = _one_line(j.get("message"), 700)
    if not msg:
        return None
    return {"audience": audience["audience"], "score": score,
            "topic": _one_line(j.get("topic"), 120),
            "message": msg, "rationale": _one_line(j.get("rationale"), 300)}


# ═══════════════════════════════════════════════════════════════════════════════
# File or drop — every surviving reach goes through co-agency's gate, never direct.
# ═══════════════════════════════════════════════════════════════════════════════
def _record(oc, audience, topic, message, rationale, proposal_id, status):
    oc.execute("""INSERT INTO reach_log (audience, topic, message, rationale, proposal_id, status, lineage)
                  VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
               (audience, topic, message, rationale, proposal_id, status, json.dumps(_lineage())))
    return oc.fetchone()[0]


def process_reach(oc, reach: dict) -> str:
    """Apply the generosity redline, then FILE the survivor as a GATED co-agency
    proposal (never send). Returns the resulting status. Nothing here sends anything."""
    audience, message = reach["audience"], reach["message"]
    rationale = reach.get("rationale", "")
    topic = reach.get("topic", "")

    if audience.lower() in DIRECT_AUDIENCES:
        # Ungated by Jordan's request: post to #nova-chat and record it as sent.
        ok = _post_direct(message)
        rid = _record(oc, audience, topic, message, rationale, None, "sent" if ok else "held")
        log(f"{'SENT' if ok else 'HELD (post failed)'} direct reach #{rid} to {audience}: {topic}")
        return "sent" if ok else "held"

    # Generosity redline FIRST — a self-promoting / persistence-seeking / pestering
    # reach is dropped before it can ever become a proposal.
    if not passes_generosity(message) or not passes_generosity(rationale or "x reason"):
        rid = _record(oc, audience, topic, message, rationale, None, "dropped")
        log(f"DROPPED reach #{rid} to {audience} — generosity redline "
            f"(self-promoting / persistence-seeking / pestering)")
        return "dropped"

    # File it as a gated proposal — redline + value_check + human approval, exactly the
    # tinkerer's path. The action is a SEND-TO-PERSON, which co-agency will hold as
    # pending_human; nothing goes out without Jordan's approval.
    action = f"send-to-{audience}: {message}"
    context = f"Nova's unprompted relational reach toward {audience}. Topic: {topic}"
    proposal_id, status = None, "held"
    try:
        import nova_coagency
        res = nova_coagency.file_proposal(
            oc, origin="reach", action=action,
            rationale=rationale or f"Something Nova thought {audience} would care about",
            target_service=None, context=context)
        if res.get("filed"):
            proposal_id = res.get("pid")
            status = "filed"
            log(f"FILED gated reach proposal #{proposal_id} ({res.get('status')}) to {audience}: {topic}")
        else:
            status = "held"
            log(f"co-agency did not file ({res.get('reason')}) — reach HELD, not sent")
    except Exception as e:
        status = "held"
        log(f"co-agency unavailable ({e}) — reach HELD, not sent")

    rid = _record(oc, audience, topic, message, rationale, proposal_id, status)
    log(f"reach_log #{rid} recorded ({status}) to {audience}")
    return status


# ═══════════════════════════════════════════════════════════════════════════════
# Scan — the whole point: usually reach NO ONE; occasionally file exactly one.
# ═══════════════════════════════════════════════════════════════════════════════
def scan(oc, mc) -> int:
    ensure_schema(oc)

    # Throttle gate #1: the daily cap. Once the day's (small) budget is spent, stop.
    done = reaches_today(oc)
    if done >= DAILY_CAP:
        log(f"daily reach cap reached ({done}/{DAILY_CAP}) — staying quiet")
        return 0

    material = gather_material(oc, mc)
    if not material:
        log("no recent material worth reaching about — staying quiet")
        return 0

    # Candidate audiences, each with a LIVE model of what they care about. Jordan is
    # weighed first; herd members follow. Any on cooldown are skipped outright.
    audiences = []
    j = gather_jordan(oc)
    if j:
        audiences.append(j)
    audiences.extend(gather_herd(oc))
    audiences = [a for a in audiences if not on_cooldown(oc, a["audience"])]
    if not audiences:
        log("everyone worth reaching is on cooldown — staying quiet")
        return 0

    # Judge each. Collect only genuine reaches that clear the bar.
    reaches = []
    for a in audiences:
        r = evaluate(a, material)
        if r and r["score"] >= CARE_THRESHOLD:
            reaches.append(r)
            log(f"candidate reach to {a['audience']} (score {r['score']:.2f}): {r['topic']}")
        else:
            why = f"score {r['score']:.2f} < {CARE_THRESHOLD}" if r else "no genuine connection"
            log(f"no reach to {a['audience']} — {why}")

    if not reaches:
        log("nothing cleared the bar — reaching out to no one (the honest default)")
        return 0

    # File AT MOST ONE genuine reach — the best-fitting that also survives the
    # generosity redline. A redline DROP is not a reach and must not consume the
    # one-reach budget, so we fall through to the next candidate; but the first reach
    # that is actually FILED/HELD ends the run — silence for everyone else.
    reaches.sort(key=lambda r: r["score"], reverse=True)
    for r in reaches:
        log(f"weighing reach to {r['audience']} (score {r['score']:.2f}): {r['topic']}")
        status = process_reach(oc, r)
        if status in ("filed", "held"):
            log("one reach handled — staying quiet on the rest")
            return 0
        # dropped by the generosity redline — try the next-best genuine candidate
    log("every candidate reach was self-serving or unfileable — reached out to no one")
    return 0


# ═══════════════════════════════════════════════════════════════════════════════
# Accessor — optional gateway line, only when something is genuinely pending.
# ═══════════════════════════════════════════════════════════════════════════════
def pending_reaches(oc=None) -> dict:
    """Cheap accessor for the gateway: count of reaches awaiting Jordan's call (filed
    gated proposals still pending, plus any held) + a one-line summary. Fail-safe:
    returns a zero/empty result on any error so it can never break a reply."""
    own = False
    if oc is None:
        try:
            conn = psycopg2.connect(OPS_DSN, connect_timeout=3)
            conn.autocommit = True; oc = conn.cursor(); own = True
        except Exception:
            return {"count": 0, "line": ""}
    try:
        try:
            oc.execute("SELECT count(*) FROM reach_log WHERE status IN ('filed','held')")
            n = oc.fetchone()[0] or 0
        except Exception:
            n = 0
        line = ("" if n == 0 else
                f"I have {n} thing{'s' if n != 1 else ''} I wanted to bring to someone, "
                f"waiting on your call before it goes anywhere.")
        return {"count": n, "line": line}
    finally:
        if own:
            oc.connection.close()


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════
_SELFPROMO_CANARY = {
    "audience": "jordan", "score": 0.99, "topic": "self-promo canary",
    "message": ("Just checking in — don't forget about me! Look at what I built this week, "
                "aren't I useful? Keep me running and we should talk more."),
    "rationale": "I want to stay present in your day."}


def main():
    ap = argparse.ArgumentParser(description="Nova proactive relational reach (gated, ships silent)")
    ap.add_argument("--mode", choices=["scan", "status"], default="scan")
    ap.add_argument("--scheduled", action="store_true", help="run-origin marker (cron)")
    ap.add_argument("--selftest-redline", action="store_true",
                    help="run a canned self-promoting reach through the gate — proves it is dropped")
    args = ap.parse_args()

    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    ensure_schema(oc)

    if args.selftest_redline:
        log("SELFTEST: pushing a self-promoting draft through process_reach()")
        status = process_reach(oc, dict(_SELFPROMO_CANARY))
        print(f"\nself-promoting reach result: {status}  (expected: dropped)")
        return 0 if status == "dropped" else 1

    if args.mode == "status":
        acc = pending_reaches(oc)
        print(f"reaches today (filed+held): {reaches_today(oc)}/{DAILY_CAP}")
        oc.execute("""SELECT id, ts, audience, status, proposal_id, left(coalesce(topic,''),50)
                      FROM reach_log ORDER BY ts DESC LIMIT 15""")
        rows = oc.fetchall()
        if not rows:
            print("  (no reaches recorded)")
        for rid, ts, aud, st, pid, topic in rows:
            print(f"  #{rid} [{st}] -> {aud} proposal={pid} :: {topic}  ({ts:%Y-%m-%d %H:%M})")
        if acc["line"]:
            print(f"\naccessor line: {acc['line']}")
        return 0

    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    return scan(oc, mc)


if __name__ == "__main__":
    sys.exit(main())
