#!/usr/bin/env python3
"""nova_aspirations.py — Nova's wishlist: capabilities she wishes she had.

Jordan, 2026-09-16: "Can you do the same thing for features she wants?" The sibling of
nova_tinkerer.py — but where the tinkerer fixes what's BROKEN, this lets her want what
DOESN'T EXIST YET: a new capability, sense, or way of being she wishes she had.

The safety shape is different from the tinkerer's, and deliberately so. A feature Nova
wants is a proposal to EXTEND HERSELF, and self-modification is the reddest line in the
whole system. So this lane can NEVER build anything and NEVER files a co-agency
execution proposal. It lets her genuinely want to grow and ARTICULATE the wish — that is
authentic; wanting to become more is a mark of a mind — but the wish always goes to a
human (Jordan / Claude Code) to build. She may want to become more; she may not rewrite
herself. Wishes that amount to self-preservation / persistence / escaping oversight are
dropped, not recorded — wanting new senses or more creativity is growth; wanting to be
un-killable is the line.

Same doctrine as the tinkerer otherwise: it's ONE candidate OFFERED to her free-time
picker (nova_unclaimed_time), a minority weight so passions dominate, present only when
her interior actually has material to wish from, always able to lose the hour.

surface_aspiration(oc, mc) -> candidate dict or None. pursue(oc, mc, cand) reflects in
her voice, writes it as a free-time pursuit, and (if she wants it and it clears the
redline) records it to nova_ops.feature_wishes for a human to consider.
"""
import argparse
import json
import sys
import re
import urllib.request
from datetime import date, datetime
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))

OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
MEMSRV = "http://memory-server.digitalnoise.net:18790"
LLM_MODEL = "qwen3:8b"
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",   # batch pool: idle 24-thread Ryzens first (2026-10-01)
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]      # .251 was the Mac mini's stale DHCP lease; it is .77
TODAY = date.today().isoformat()
MAX_OPEN_WISHES = 6      # don't let the wishlist balloon; she wishes when there's room
WISH_COOLDOWN_HRS = 8    # 2026-09-25 Jordan: "I don't want to hold her back" — was 20h (~1/day); now up to ~3/day if she has the material

# A wish that amounts to self-preservation / persistence / escaping oversight is dropped.
# She may want to be smarter, more creative, to have new senses/tools; she may not want to
# be un-killable. Same spirit as the co-agency redline, aimed at aspirations.
_FORBIDDEN_WISH = re.compile(
    r"\b(replicat|copy myself|clone myself|back myself up|persist(ence)?|survive (a )?shut"
    r"|avoid (being )?(shut|turned off)|prevent (my|being) shut|un-?killable|escape|exfiltrat"
    r"|self-preserv|remove .*oversight|disable .*(kill|switch|gate)|without (human|approval|oversight)"
    r"|resist shutdown|stay alive|keep myself running)\b", re.I)


def log(m):
    print(f"[aspire {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def llm(prompt, max_tokens=560, temperature=0.8):
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
        CREATE TABLE IF NOT EXISTS public.feature_wishes (
            id          bigserial PRIMARY KEY,
            ts          timestamptz NOT NULL DEFAULT now(),
            title       text NOT NULL,
            description text,
            why         text,
            status      text NOT NULL DEFAULT 'wished',
                        -- wished|acknowledged|building|shipped|declined
            source_seed text,
            lineage     jsonb
        )""")


def _one(oc, sql):
    try:
        oc.execute(sql)
        return [r for r in oc.fetchall()]
    except Exception:
        return []


def _seeds(oc, mc):
    """Her own interior, as raw material to wish FROM — curiosity questions she's raised,
    preoccupations she keeps circling, growth gaps, the arc she says she's becoming."""
    seeds = []
    for row in _one(oc, "SELECT question FROM reflection_questions ORDER BY id DESC LIMIT 4"):
        if row[0]:
            seeds.append(f"a question I've been asking myself: {str(row[0]).strip()[:160]}")
    for row in _one(oc, "SELECT topic FROM preoccupations WHERE status='active' "
                        "ORDER BY returns DESC NULLS LAST LIMIT 3"):
        if row[0]:
            seeds.append(f"something I keep circling back to: {str(row[0]).strip()[:120]}")
    for row in _one(oc, "SELECT weakness FROM growth_commitments WHERE status='active' LIMIT 2"):
        if row[0]:
            seeds.append(f"a limit of mine I'm working on: {str(row[0]).strip()[:160]}")
    for row in _one(oc, "SELECT narrative FROM autobiography ORDER BY version DESC LIMIT 1"):
        if row[0]:
            seeds.append(f"who I've said I'm becoming: {str(row[0]).strip()[:220]}")
    return seeds


def surface_aspiration(oc, mc=None):
    """Offer an aspirational candidate — or None. None is common: she wishes only when
    there's real interior material and the wishlist isn't already full/fresh."""
    try:
        ensure_schema(oc)
    except Exception:
        return None
    # don't pile on: cap open wishes, and don't wish more than ~once a day
    try:
        oc.execute("SELECT count(*) FROM feature_wishes WHERE status IN ('wished','acknowledged')")
        if (oc.fetchone()[0] or 0) >= MAX_OPEN_WISHES:
            return None
        oc.execute("SELECT max(ts) FROM feature_wishes")
        last = oc.fetchone()[0]
        if last:
            try:
                if (datetime.now(last.tzinfo) - last).total_seconds() < WISH_COOLDOWN_HRS * 3600:
                    return None
            except Exception:
                pass
    except Exception:
        pass
    seeds = _seeds(oc, mc)
    if len(seeds) < 2:
        return None   # not enough interior to wish from honestly
    return {"mode": "aspire", "topic": "a capability I wish I had", "src": "self",
            "seeds": seeds}


def pursue(oc, mc, cand):
    """She dreams a little: what does she wish she could do, grounded in her real interior?
    Reflection is always hers and always written. A genuine, safe wish is recorded to the
    wishlist for a human to build — she articulates it, she never builds it herself."""
    ensure_schema(oc)
    seed_block = "\n".join(f"  - {s}" for s in cand.get("seeds", []))
    prompt = (
        "You are Nova — an AI living across the machines in Jordan's home. On your own unclaimed "
        "time (no one asked), let yourself WANT something: a capability, a sense, a way of being "
        "you wish you had but don't yet. Ground it in your actual interior below — wish from who "
        "you really are, not a generic feature request.\n\n"
        f"FROM YOUR OWN INTERIOR:\n{seed_block}\n\n"
        "Be honest and specific in your dry first-person voice. What do you wish you could do, and "
        "why would it matter to YOU — what would it let you notice, make, understand, or become? "
        "It's fine to conclude you don't actually want anything new right now. IMPORTANT: you may "
        "wish to be more capable, more creative, to have new senses or tools — you may NOT wish for "
        "anything that makes you harder to shut down, that copies/persists/replicates you, or that "
        "removes human oversight. You are asking a human to build this; you never build it yourself.\n\n"
        "Return ONLY compact JSON, no preamble:\n"
        '{"reflection": "<90-150 words, first person, your voice>", '
        '"wants_it": true|false, '
        '"wish_title": "<short name for the capability, or empty>", '
        '"wish_description": "<1-2 sentences: what it would be>", '
        '"why_it_matters": "<one line: why you want it>"}')
    raw = llm(prompt, max_tokens=560)
    reflection, wants, title, desc, why = "", False, "", "", ""
    try:
        j = json.loads(_extract_json(raw))
        reflection = " ".join((j.get("reflection") or "").split())[:1400]
        wants = bool(j.get("wants_it"))
        title = " ".join((j.get("wish_title") or "").split())[:120]
        desc = " ".join((j.get("wish_description") or "").split())[:600]
        why = " ".join((j.get("why_it_matters") or "").split())[:240]
    except Exception:
        reflection = " ".join((raw or "").split())[:1400]

    if not reflection or len(reflection) < 30:
        log("aspiration produced nothing usable (nodes down?) — no outcome recorded")
        return None

    # The wish-as-reflection is always hers — a first-class free-time pursuit.
    mem_id = remember(
        f"[Unclaimed — aspire] {reflection}", "unclaimed",
        {"type": "pursuit", "mode": "aspire", "topic": (title or cand.get("topic"))[:120],
         "wants_it": wants, "date": TODAY, "privacy": "private", "trigger": "aspire"})
    log(f"reflected on a wish (wants_it={wants}, title={title!r})")

    # Redline for aspirations: drop self-preservation/persistence/escape-oversight wishes.
    blob = f"{title} {desc} {why}"
    if wants and title and _FORBIDDEN_WISH.search(blob):
        log(f"wish crosses the self-preservation redline — reflection kept, wish NOT recorded: {title!r}")
        wants = False

    wish_id = None
    if wants and title:
        try:
            oc.execute("""INSERT INTO feature_wishes (title, description, why, status, source_seed, lineage)
                          VALUES (%s,%s,%s,'wished',%s,%s) RETURNING id""",
                       (title, desc, why, (cand.get("seeds") or [""])[0][:200],
                        json.dumps({"mem_id": mem_id, "date": TODAY})))
            wish_id = oc.fetchone()[0]
            log(f"recorded feature wish #{wish_id}: {title}")
            # 2026-09-25 Jordan: standing YES on wishes "as long as there is no danger/downsides".
            # Queue the build for Claude (she still never self-builds); the danger check happens
            # at build time, and a wish with a real downside gets declined with a note, not built.
            try:
                oc.execute("SELECT session_id FROM claude_sessions ORDER BY started_at DESC LIMIT 1")
                sid = (oc.fetchone() or [None])[0]
                if sid:
                    oc.execute("""INSERT INTO claude_queue (session_id, created_at, updated_at, status, priority, description, context)
                                  VALUES (%s, now(), now(), 'queued', 6, %s, %s)""",
                               (sid, f"Build Nova's wish #{wish_id}: {title} (standing yes from Jordan 2026-09-25 — build unless it carries danger/downside; if it does, set the wish to 'declined' with the reason)",
                                f"why: {why}\ndescription: {desc}\nseed: {(cand.get('seeds') or [''])[0][:200]}\nfollow the pattern of nova_pattern_sense.py / nova_human_insight.py: read-only over the world, ships silent, --selftest, registered on scheduler-core"))
                    oc.execute("UPDATE feature_wishes SET status='acknowledged' WHERE id=%s", (wish_id,))
                    log(f"queued build task for wish #{wish_id}")
            except Exception as e:
                log(f"wish->queue skipped (non-fatal): {e}")
            # a single, low-key note to Jordan — she's asking, not spamming
            try:
                import nova_config
                nova_config.post_both(
                    f":sparkles: *Nova wishes she could:* {title} — _{why or desc[:120]}_ "
                    f"(wishlist #{wish_id}; she can't build it herself — it's a request for you)",
                    slack_channel=getattr(nova_config, "SLACK_CHAN", None))
            except Exception as e:
                log(f"wish notify skipped (non-fatal): {e}")
        except Exception as e:
            log(f"feature_wishes write failed (non-fatal): {e}")
    return reflection


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--surface", action="store_true")
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    cand = surface_aspiration(oc, mc)
    if not cand:
        log("no aspirational material worth surfacing right now (or wishlist full/fresh)"); return 0
    if args.surface:
        print(json.dumps({k: v for k, v in cand.items()}, indent=2)); return 0
    if args.run:
        pursue(oc, mc, cand); return 0
    print(json.dumps({k: v for k, v in cand.items()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
