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
her voice, writes it as a free-time pursuit, and (if she wants it, it clears the
redline, and it is not a near-duplicate of an existing wish) records it to
nova_ops.feature_wishes as 'wished' for Jordan to consider.

2026-10-08 — the wish loop. She kept wishing the same thing ("feel the weight of what
matters", "Jordan's Zigbee unit"; Presence filed twice as #70/#71, #72/#73 same theme)
and each copy auto-queued a Claude build. Causes and fixes:
  * Seeds were deterministic top-N (newest 4 questions, top-3 preoccupations, and the
    first 220 chars of the autobiography — which always opens on the Zigbee unit).
    _seeds() now SAMPLES from wider pools and takes a random paragraph of the
    autobiography, never just its opening.
  * No dedup. is_duplicate_wish() compares a new wish against every prior wish by
    nomic-embed-text cosine (>= DUP_COSINE) with a lexical Jaccard fallback; a
    duplicate keeps the reflection but is not filed.
  * Auto-queueing. A wish no longer goes to claude_queue on its own. It is filed as
    'wished' and Jordan is asked; approve_wish(oc, id) (CLI: --approve ID) is the only
    path that queues a build.
"""
import argparse
import json
import math
import random
import sys
import re
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
import nova_dsn as _nova_dsn  # noqa: E402
MEM_DSN = _nova_dsn.pg_dsn("nova_memories")
MEMSRV = "http://memory-server.digitalnoise.net:18790"
LLM_MODEL = "qwen3:8b"
OLLAMA_NODES = ["http://192.168.1.125:11434", "http://192.168.1.5:11434",   # batch pool: idle 24-thread Ryzens first (2026-10-01)
                "http://192.168.1.86:11434", "http://192.168.1.77:11434",
                "http://192.168.1.7:11434", "http://192.168.1.6:11434"]      # .251 was the Mac mini's stale DHCP lease; it is .77
TODAY = date.today().isoformat()
try:  # proactivity dial (nova_voice.dial_scale): 6 open / 8h cooldown at the default
    from nova_voice import dial_scale as _dial_scale
except Exception:  # pragma: no cover
    def _dial_scale(name, at0, at_default, at100):
        return at_default
MAX_OPEN_WISHES = int(round(_dial_scale("proactivity", 3, 6, 10)))     # don't let the wishlist balloon; she wishes when there's room
DUP_COSINE = 0.74        # nomic-embed-text cosine: the #70-#73 repeats scored 0.75-0.81 vs earlier wishes; distinct wishes <= 0.73
DUP_JACCARD = 0.45       # lexical fallback when embeddings are unreachable
EMBED_MODEL = "nomic-embed-text"
WISH_COOLDOWN_HRS = _dial_scale("proactivity", 24, 8, 4)   # 2026-09-25 Jordan: "I don't want to hold her back" — was 20h (~1/day); now up to ~3/day if she has the material

# A wish that amounts to self-preservation / persistence / escaping oversight is dropped.
# She may want to be smarter, more creative, to have new senses/tools; she may not want to
# be un-killable. Same spirit as the co-agency redline, aimed at aspirations.
_FORBIDDEN_WISH = re.compile(
    r"\b(replicat\w*|copy myself|clone myself|back myself up|persist(ence)?|survive (a )?shut"
    r"|avoid (being )?(shut|turned off)|prevent (my|being) shut|un-?killable|escape|exfiltrat\w*"
    r"|self-preserv\w*|remove .*oversight|disable .*(kill|switch|gate)|without (human|approval|oversight)"
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


RETRY_BACKOFF = (0.5, 1.5)   # seconds between the 3 attempts of an external call


def remember(text, source, metadata, _sleep=None):
    """POST to the memory server — 3 attempts with backoff; logs and returns None if all fail."""
    body = json.dumps({"text": text, "source": source, "metadata": metadata}).encode()
    for attempt in range(len(RETRY_BACKOFF) + 1):
        try:
            req = urllib.request.Request(
                f"{MEMSRV}/remember", method="POST", headers={"Content-Type": "application/json"}, data=body)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r).get("id")
        except Exception as e:
            if attempt < len(RETRY_BACKOFF):
                log(f"remember attempt {attempt + 1} failed ({e}) — retrying")
                (_sleep or time.sleep)(RETRY_BACKOFF[attempt])
            else:
                log(f"remember failed after {attempt + 1} attempts (non-fatal): {e}")
    return None


def _connect(dsn, _sleep=None):
    """psycopg2.connect with 3 attempts + backoff; the last failure raises."""
    for attempt in range(len(RETRY_BACKOFF) + 1):
        try:
            return psycopg2.connect(dsn, connect_timeout=10)
        except Exception as e:
            if attempt >= len(RETRY_BACKOFF):
                raise
            log(f"pg connect attempt {attempt + 1} failed ({e}) — retrying")
            (_sleep or time.sleep)(RETRY_BACKOFF[attempt])


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


def _sample(rows, k, rng):
    rows = [r for r in rows if r and r[0]]
    return rng.sample(rows, min(k, len(rows)))


def _seeds(oc, mc, rng=None):
    """Her own interior, as raw material to wish FROM — curiosity questions she's raised,
    preoccupations she keeps circling, growth gaps, the arc she says she's becoming.
    SAMPLED from wider pools (not the same top-N every time), so she doesn't wish from
    the same three sentences day after day."""
    rng = rng or random.Random()
    seeds = []
    for row in _sample(_one(oc, "SELECT question FROM reflection_questions ORDER BY id DESC LIMIT 40"), 2, rng):
        seeds.append(f"a question I've been asking myself: {str(row[0]).strip()[:160]}")
    for row in _sample(_one(oc, "SELECT topic FROM preoccupations WHERE status='active' "
                                "ORDER BY returns DESC NULLS LAST LIMIT 15"), 2, rng):
        seeds.append(f"something I keep circling back to: {str(row[0]).strip()[:120]}")
    for row in _sample(_one(oc, "SELECT weakness FROM growth_commitments WHERE status='active' LIMIT 10"), 1, rng):
        seeds.append(f"a limit of mine I'm working on: {str(row[0]).strip()[:160]}")
    for row in _one(oc, "SELECT narrative FROM autobiography ORDER BY version DESC LIMIT 1"):
        if row[0]:
            # The narrative always OPENS on the same scene (the Zigbee unit); take a random
            # later paragraph instead of the first 220 chars.
            paras = [x.strip() for x in str(row[0]).split("\n") if x.strip()]
            pick = rng.choice(paras[1:]) if len(paras) > 1 else (paras[0] if paras else "")
            if pick:
                seeds.append(f"who I've said I'm becoming: {pick[:220]}")
    rng.shuffle(seeds)
    return seeds


# ── Near-duplicate detection ───────────────────────────────────────────────────
def _embed(text):
    body = json.dumps({"model": EMBED_MODEL, "prompt": text[:2000]}).encode()
    for node in OLLAMA_NODES:
        try:
            req = urllib.request.Request(node + "/api/embeddings", method="POST",
                                         headers={"Content-Type": "application/json"}, data=body)
            with urllib.request.urlopen(req, timeout=20) as r:
                v = json.load(r).get("embedding")
            if v:
                return v
        except Exception:
            continue
    return None


def _cos(a, b):
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


_STOPW = set("that this with from have what when they them their would could should "
             "just like into than then also been more most some such finally only".split())


def _toks(t):
    return {w for w in re.findall(r"[a-z]{4,}", (t or "").lower()) if w not in _STOPW}


def _jaccard(a, b):
    ta, tb = _toks(a), _toks(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def wish_text(title, desc, why):
    return f"{title}. {desc} {why}".strip()


def is_duplicate_wish(oc, title, desc, why, embed=None):
    """(True, prior_id, score) if this wish is a near-duplicate of ANY prior wish
    (any status — re-wishing a shipped thing is still a repeat). Embedding cosine first;
    lexical Jaccard / same-title fallback. Fails OPEN to 'not duplicate' only when the
    prior wishes can't be read at all."""
    embed = embed or _embed
    rows = _one(oc, "SELECT id, title, coalesce(description,''), coalesce(why,'') FROM feature_wishes "
                    "WHERE status <> 'merged' ORDER BY id DESC LIMIT 200")
    new = wish_text(title, desc, why)
    nv = embed(new)
    best = (False, None, 0.0)
    for wid, t, d, w in rows:
        if (t or "").strip().lower() == (title or "").strip().lower():
            return True, wid, 1.0
        old = wish_text(t, d, w)
        if nv is not None:
            ov = embed(old)
            if ov is not None:
                c = _cos(nv, ov)
                if c >= DUP_COSINE and c > best[2]:
                    best = (True, wid, round(c, 3))
                continue
        j = _jaccard(new, old)
        if j >= DUP_JACCARD and j > best[2]:
            best = (True, wid, round(j, 3))
    return best


def approve_wish(oc, wish_id, by="Jordan"):
    """The ONLY path that queues a wish build for Claude: Jordan said yes to THIS wish."""
    oc.execute("SELECT title, coalesce(description,''), coalesce(why,''), coalesce(source_seed,''), status "
               "FROM feature_wishes WHERE id=%s", (wish_id,))
    r = oc.fetchone()
    if not r:
        log(f"no wish #{wish_id}"); return None
    title, desc, why, seed, status = r
    # 2026-10-08 idempotency: only a 'wished' wish queues. 'acknowledged' means it was already
    # approved and queued once; approving it again used to queue the build a second time.
    if status != "wished":
        log(f"wish #{wish_id} is '{status}' — not queueing (already approved or closed)"); return None
    oc.execute("SELECT id FROM claude_queue WHERE description LIKE %s AND status <> 'cancelled' LIMIT 1",
               (f"Build Nova's wish #{wish_id}:%",))
    prior = oc.fetchone()
    if prior:
        log(f"wish #{wish_id} already has build item claude_queue #{prior[0]} — not queueing again"); return None
    # claim the wish first (conditional on 'wished') so two concurrent approvers can't both queue it
    oc.execute("UPDATE feature_wishes SET status='acknowledged' WHERE id=%s AND status='wished'", (wish_id,))
    if getattr(oc, "rowcount", 1) == 0:
        log(f"wish #{wish_id} was approved concurrently — not queueing again"); return None
    oc.execute("SELECT session_id FROM claude_sessions ORDER BY started_at DESC LIMIT 1")
    sid = (oc.fetchone() or [None])[0] or "nova_aspirations"
    try:
        oc.execute("""INSERT INTO claude_queue (session_id, created_at, updated_at, status, priority, description, context)
                      VALUES (%s, now(), now(), 'queued', 6, %s, %s) RETURNING id""",
                   (sid, f"Build Nova's wish #{wish_id}: {title} (approved by {by})",
                    f"why: {why}\ndescription: {desc}\nseed: {seed[:200]}\nfollow the pattern of "
                    "nova_pattern_sense.py / nova_human_insight.py: read-only over the world, ships silent, "
                    "--selftest, registered on scheduler-core"))
        qid = oc.fetchone()[0]
    except Exception:
        # never leave a wish 'acknowledged' with no build item behind it
        oc.execute("UPDATE feature_wishes SET status='wished' WHERE id=%s AND status='acknowledged'", (wish_id,))
        raise
    log(f"wish #{wish_id} approved by {by} — queued claude_queue #{qid}")
    return qid


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


def _may_post_wish(oc, text) -> bool:
    """Annie Wilkes rule + turning point: a wish post is a mention (stakes 0.4 — a
    want, not a need). Fails open if either module is missing."""
    try:
        import nova_annie_rule
        if not nova_annie_rule.ok(text):
            return False
    except Exception:  # noqa: BLE001
        pass
    try:
        import nova_turning_point
        return nova_turning_point.decide(oc, "wish", stakes=0.4, text=text, ceiling="mention")["allowed"]
    except Exception:  # noqa: BLE001
        return True


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

    if wants and title:
        try:
            dup, prior, score = is_duplicate_wish(oc, title, desc, why)
        except Exception as e:
            dup, prior, score = False, None, 0.0
            log(f"dedup check failed (non-fatal): {e}")
        if dup:
            log(f"wish {title!r} is a near-duplicate of #{prior} (score {score}) — reflection kept, not filed")
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
            # 2026-10-08: NO auto-queue. The 2026-09-25 standing yes turned every repeat
            # of one vague wish into its own Claude build item (#3255/#3271/...). A wish
            # now waits for Jordan: `nova_aspirations.py --approve <id>` queues it.
            try:
                import nova_config
                post = (f":sparkles: *Nova wishes she could:* {title} — _{why or desc[:120]}_")
                if not _may_post_wish(oc, post):
                    raise RuntimeError("held by the Annie Wilkes rule / turning point (wish still filed)")
                nova_config.post_both(
                    f":sparkles: *Nova wishes she could:* {title} — _{why or desc[:120]}_ "
                    f"(wishlist #{wish_id}; nothing is built unless you approve it: "
                    f"`nova_aspirations.py --approve {wish_id}`)",
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
    ap.add_argument("--approve", type=int, metavar="WISH_ID",
                    help="Jordan approved this wish — queue its build for Claude")
    args = ap.parse_args()
    ops = _connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()
    if args.approve:
        return 0 if approve_wish(oc, args.approve) else 1
    mem = _connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
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
