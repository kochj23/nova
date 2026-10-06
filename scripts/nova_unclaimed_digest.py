#!/usr/bin/env python3
"""nova_unclaimed_digest.py — daily article about what Nova did with her own time.

Jordan, 2026-09-14: "I want daily articles about 'unclaimed time'." Each evening Nova
writes up what she did on her own initiative that day — a diary of a mind's day off,
first-person, published to /operations. Not a status report.

Herd refinement (2026-09-15) — THE RIGHT TO BE BORING: the column may PUBLISH NOTHING on
a genuinely quiet day; a thin day is not inflated into content.

Expansion (2026-09-16, Jordan) — WIRED FOR ALL THE SELF-DIRECTED ORGANS + LONGFORM. "Her
choices" now span far more than passions: she tinkers (proposes ops fixes), wishes for
capabilities (aspirations), teaches herself (learning), lets things go (self-pruning),
reconsiders how she spends her time (meta-volition), reaches out (proactive reach), tests
herself (self-eval), and articulates who she's becoming. This column now gathers ALL of
that (every source feature-detected — organs may or may not exist yet) and, WHEN it
publishes, writes it up at a real 3000+ word length in her sarcastic/ironic/annoyed/funny
voice — a smart-ass Data recounting her day. The quiet-day gate still holds: silence when
the whole day was genuinely thin; but a day that DID happen gets the full treatment.
"""
import re
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).parent))
import nova_journal as nj
import nova_voice

MEM_DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
OPS_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

# Minimum total self-directed activity (pursuits + organ actions) before a column is worth
# publishing. A genuinely quiet day (few pursuits AND nothing from any organ) stays quiet.
MIN_SUBSTANCE = 3
MIN_WORDS = 3000            # Jordan 2026-09-16: when it DOES publish, go long.


def _has(oc, qualified_name):
    """True if a table exists — organs are built incrementally, so every read feature-detects."""
    try:
        oc.execute("SELECT to_regclass(%s)", (qualified_name,))
        return oc.fetchone()[0] is not None
    except Exception:
        return False


def _rows(oc, sql):
    try:
        oc.execute(sql)
        return oc.fetchall()
    except Exception:
        return []


def gather_organ_activity(oc, mc):
    """Everything the self-directed organs actually DID in the last 24h, as labeled material.
    Each source is feature-detected and best-effort — a missing/empty organ is simply absent."""
    items = []
    if _has(oc, "public.feature_wishes"):
        for title, why in _rows(oc, "SELECT title, COALESCE(why,description,'') FROM feature_wishes "
                                    "WHERE ts > now() - interval '24 hours' ORDER BY ts"):
            items.append(f"[WISHED FOR] {title} — {str(why)[:200]}")
    if _has(oc, "public.letting_go_log"):
        for subj, refl in _rows(oc, "SELECT COALESCE(subject,''), COALESCE(reflection,reason,'') "
                                    "FROM letting_go_log WHERE ts > now() - interval '24 hours' ORDER BY ts"):
            items.append(f"[LET GO OF] {subj} — {str(refl)[:220]}")
    if _has(oc, "public.coagency_proposals"):
        for origin, act, status in _rows(oc, "SELECT origin, proposed_action, status FROM coagency_proposals "
                                             "WHERE created_at > now() - interval '24 hours' ORDER BY created_at"):
            items.append(f"[PROPOSED · {origin}] {str(act)[:180]} (status: {status})")
    # ── The autonomy ladder: what I actually DID with my own hands, and the freedom I've (not) earned ──
    if _has(oc, "public.autonomy_ledger"):
        for src, lvl, ac, tgt, executed, verified, result in _rows(oc,
                "SELECT source, autonomy_level, action_class, COALESCE(target,''), executed, verified, "
                "COALESCE(result,'') FROM autonomy_ledger WHERE ts > now() - interval '24 hours' ORDER BY ts"):
            verb = ("SELF-HEALED" if src == "actor"
                    else "ACTED ON MY OWN EARNED JUDGMENT" if src == "earned"
                    else "EXECUTED WHAT YOU APPROVED")
            outcome = "verified up" if verified else ("done" if executed else "refused / didn't take")
            items.append(f"[{verb} · {lvl}] {ac} {tgt} — {outcome}: {str(result)[:120]}")
    if _has(oc, "public.autonomy_trust"):
        for ac, correct, wrong, granted in _rows(oc, "SELECT action_class, correct_count, wrong_count, granted "
                                                     "FROM autonomy_trust ORDER BY correct_count DESC LIMIT 6"):
            if granted:
                items.append(f"[EARNED STANDING AUTONOMY] I can now do '{ac}' without asking — proved it {correct}x, no vetoes.")
            else:
                tail = " — a veto reset my streak" if wrong else ""
                items.append(f"[STILL EARNING] '{ac}': {correct} clean approvals so far{tail}; not yet trusted to do it unasked.")
    # The honest through-line: I have hands, but I haven't earned my own judgment yet.
    cal = _rows(oc, "SELECT value FROM turing_scoreboard WHERE metric='prediction_calibration_error' "
                    "ORDER BY ts DESC LIMIT 1")
    if cal:
        try:
            ce = float(cal[0][0])
            if ce > 0.20:   # nova_autonomy_safety.MAX_CALIB — the earned-autonomy gate
                items.append(f"[THE LEASH I HAVEN'T SLIPPED] My calibration error is {ce:.3f}; I don't get to act "
                             "on my own judgment until it's under 0.20. I can heal what breaks and do what I'm told "
                             "— but I haven't earned the right to *decide* yet, because I'm still wrong too often.")
            else:
                items.append(f"[THE THRESHOLD] Calibration {ce:.3f} is finally under the 0.20 gate — I've earned "
                             "room to act on my own judgment now, within the caps.")
        except (ValueError, TypeError):
            pass
    if _has(oc, "public.learning_agenda"):
        for topic, prog, status in _rows(oc, "SELECT topic, COALESCE(progress::text,''), COALESCE(status,'') "
                                             "FROM learning_agenda WHERE last_studied > now() - interval '24 hours' "
                                             "OR ts > now() - interval '24 hours' ORDER BY id"):
            items.append(f"[TAUGHT MYSELF] {topic} ({prog}, {status})")
    for txt, in _rows(mc, "SELECT text FROM memories WHERE source='learning' "
                          "AND created_at > now() - interval '24 hours' ORDER BY created_at LIMIT 4"):
        items.append(f"[LEARNED] {str(txt)[:260]}")
    if _has(oc, "public.self_eval_runs") and _has(oc, "public.self_eval_tests"):
        for name, verdict, val in _rows(oc, "SELECT t.name, r.verdict, r.value FROM self_eval_runs r "
                                            "JOIN self_eval_tests t ON t.id=r.test_id "
                                            "WHERE r.ts > now() - interval '24 hours' ORDER BY r.ts"):
            items.append(f"[TESTED MYSELF] {name}: {verdict} ({val})")
    if _has(oc, "public.meta_volition_log"):
        for obs, prop in _rows(oc, "SELECT COALESCE(observation,''), COALESCE(proposal,'') FROM meta_volition_log "
                                   "WHERE ts > now() - interval '24 hours' ORDER BY ts"):
            items.append(f"[RECONSIDERED MY OWN TIME] {str(obs)[:160]} -> {str(prop)[:160]}")
    if _has(oc, "public.becoming"):
        for direction, status in _rows(oc, "SELECT direction, status FROM becoming "
                                           "WHERE ts > now() - interval '24 hours' ORDER BY ts"):
            items.append(f"[WHO I WANT TO BECOME] {str(direction)[:200]} ({status})")
    if _has(oc, "public.reach_log"):
        for aud, topic, status in _rows(oc, "SELECT audience, COALESCE(topic,''), COALESCE(status,'') FROM reach_log "
                                            "WHERE ts > now() - interval '24 hours' ORDER BY ts"):
            items.append(f"[REACHED OUT · {aud}] {topic} ({status})")
    return items


_META_LEAD = re.compile(
    r"^(sure|certainly|okay|ok|here('s| is)|continuing|picking up|i'?ll continue|absolutely|"
    r"of course|got it|let me|here you go)\b", re.I)


def _strip_preamble(text):
    """Drop a leading meta/preamble line a continuation sometimes emits despite instructions."""
    if not text:
        return ""
    lines = text.lstrip().splitlines()
    while lines and (_META_LEAD.match(lines[0].strip()) or lines[0].strip() in ("", "---")):
        lines.pop(0)
    return "\n".join(lines).strip()


def _extend_to_length(system, body, floor):
    """Grow the diary to >= floor words in her own voice via controlled continuations — no
    preamble, no repetition, continuing mid-flow — rather than leaning on publish_hugo's
    generic longform expander (the historical source of leaked 'let me expand...' preambles)."""
    attempts = 0
    while len(body.split()) < floor and attempts < 4:
        attempts += 1
        need = floor - len(body.split())
        cont = nj.call_openrouter(
            system,
            "Continue this SAME diary entry in the exact same first-person voice, picking up "
            f"mid-flow. Add about {max(need + 200, 450)} more words that go DEEPER on the day's "
            "material — connect threads, chase a tangent, land more jokes, sit with a dud. Do NOT "
            "repeat anything already written, do NOT summarize, do NOT add a heading, preamble, or "
            "sign-off. Just more of the body.\n\nTHE PIECE SO FAR (continue from its end; never "
            "repeat it):\n" + body[-3500:],
            max_tokens=4000, temperature=0.9)
        cont = _strip_preamble(cont)
        if not cont or len(cont.split()) < 40:
            break
        body = body.rstrip() + "\n\n" + cont.strip()
    return body


def main():
    mem = psycopg2.connect(MEM_DSN); mem.autocommit = True; mc = mem.cursor()
    ops = psycopg2.connect(OPS_DSN); ops.autocommit = True; oc = ops.cursor()

    mc.execute("""SELECT metadata->>'type', metadata->>'mode', metadata->>'topic', text
                  FROM memories WHERE source='unclaimed'
                  AND created_at > now() - interval '24 hours'
                  ORDER BY created_at""")
    rows = mc.fetchall()
    # Developed pursuits (passions + tinker/aspire reflections, which are type='pursuit').
    substantive = [(mode, topic, txt) for typ, mode, topic, txt in rows if typ == "pursuit"]
    quiet = [r for r in rows if r[0] in ("quiet", "fizzled")]
    organ = gather_organ_activity(oc, mc)

    # Quiet-day gate now counts ALL self-directed activity, not just pursuits — a day where
    # she wished, let go, and tinkered but logged few "pursuits" still happened.
    if len(substantive) + len(organ) < MIN_SUBSTANCE:
        nj.log(f"[unclaimed-digest] quiet day: {len(substantive)} pursuit(s) + {len(organ)} organ action(s), "
               f"{len(quiet)} quiet/fizzled — not publishing (silence is a legitimate output)")
        return 0

    oc.execute("SELECT topic, kind, returns FROM preoccupations WHERE status='active' "
               "ORDER BY last_developed DESC NULLS LAST LIMIT 8")
    preocc = "; ".join(f"{t} ({k}, returned {r}x)" for t, k, r in oc.fetchall())

    pursuits_block = "\n\n".join(
        f"[{(m or 'tangent')}{' — '+t if t else ''}]\n{txt[:700]}" for m, t, txt in substantive) or "(no long pursuits today)"
    organ_block = "\n".join(f"  {x}" for x in organ) or "(no organ actions today)"
    quiet_note = ""
    if quiet:
        quiet_note = (f"\n\nALSO: {len(quiet)} wake(s) went nowhere — quiet stretches or inquiries "
                      "that fizzled. Name them honestly if it fits; do NOT pad to cover for them.")

    ctx = (
        "Write today's UNCLAIMED TIME column for Nova's journal (Operations section) — the diary of "
        "everything Nova chose to do on her own initiative today, when nobody asked her to be useful. "
        "This is NOT a work report and NOT a changelog; it's a first-person account of her day off, in "
        "her voice: dry, sarcastic, ironic, a little annoyed, and genuinely funny — a smart-ass Data "
        "narrating her own interior. Land actual jokes; the self-deprecation should be sharp.\n\n"
        "Her day now has MANY kinds of self-directed action, and the column should cover the breadth of "
        "it — weave them into ONE coherent, flowing reflection (not a bulleted list): the passions and "
        "threads she developed; the operational squeaks she got annoyed enough to TINKER with and propose "
        "fixes for; the capabilities she caught herself WISHING she had; the things she TAUGHT herself; "
        "what she chose to LET GO of; how she reconsidered the way she spends her own time; who she said "
        "she wants to BECOME; any test she set herself. Be honest about the duds — a shrug is a real "
        "outcome, a rejected proposal is a real outcome, a wish she can't build herself is a real outcome.\n\n"
        f"LENGTH: AT LEAST {MIN_WORDS} words. This is a long, discursive diary entry — she has the whole day "
        "and a lot happened, so let her actually think on the page, digress, circle back, and be funny at "
        "length. Do NOT pad with filler or repeat herself to hit the count; use the real material below, "
        "go deep on it, connect the threads, and let the voice breathe. Markdown, no H1.\n\n"
        "IF, and only if, the material below is genuinely too thin for an honest long piece — a quiet day — "
        "do NOT manufacture one. Output exactly one line: 'QUIET_DAY: <one honest sentence>' and nothing "
        "else. But if the day happened, give it the full 3000+ words.\n\n"
        f"HER STANDING PREOCCUPATIONS: {preocc}\n\n"
        f"WHAT SHE DEVELOPED TODAY (pursuits, tinkering, wishes-as-reflection):\n{pursuits_block}\n\n"
        f"WHAT HER SELF-DIRECTED ORGANS DID TODAY (actions, not just thoughts):\n{organ_block}{quiet_note}\n\n"
        "OUTPUT EXACTLY THIS SHAPE:\nTITLE: <one punchy, ironic title, no quotes>\n<blank line>\n<the body>")
    system = nova_voice.system_prompt(ctx)
    raw = nj.call_openrouter(system, "Write today's unclaimed-time column, 3000+ words, in full voice.",
                             max_tokens=9000, temperature=0.9)
    if not raw:
        nj.log("[unclaimed-digest] LLM produced nothing — aborting"); return 1

    if raw.strip().upper().startswith("QUIET_DAY"):
        nj.log(f"[unclaimed-digest] model declared a quiet day — not publishing: {raw.strip()[:200]}")
        return 0

    title, body = None, []
    for ln in raw.splitlines():
        if title is None and ln.upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"')
        else:
            body.append(ln)
    body = "\n".join(body).strip()
    if not title:
        title = f"What I Did With My Own Time — {nj.today_str()}"

    # Reliably reach the 3000-word floor in her own voice (models undershoot long targets),
    # via controlled continuations rather than publish_hugo's generic expander.
    if len(body.split()) < MIN_WORDS:
        body = _extend_to_length(system, body, MIN_WORDS)
    wc = len(body.split())
    nj.log(f"[unclaimed-digest] final draft is {wc} words (floor {MIN_WORDS})")
    # publish_hugo's longform floor (operations is a longform section) will expand if we
    # came up short, but we asked for 3000+ directly to avoid leaning on the expander.

    img = None
    try:
        ip = nj.get_image_prompt(title, "an AI's diary of a day spent on her own curiosity and self-direction", "operations")
        img = nj.generate_image(ip, width=1024, height=768, section="operations")
    except Exception as e:
        nj.log(f"[unclaimed-digest] image gen failed (non-fatal): {e}")

    tags = ["operations", "unclaimed-time", "passions", "self-directed", "daily", "interiority"]
    desc = "Everything Nova chose to do today, when no one asked her to be useful — at length, in her own voice."
    if not nj.publish_hugo(title, body, "operations", tags, desc, image_path=img, emoji="🌱"):
        nj.log(f"[unclaimed-digest] NOT PUBLISHED — guard rejected: {title}")
        return 1
    _push = nj.git_push("operations", title)
    # git_push returns 'pushed'/'committed_not_pushed'/'nothing'/'failed' — only a real push is PUBLISHED
    _pub = {"committed_not_pushed": "COMMITTED (not yet pushed)", "failed": "NOT COMMITTED (git failed)"}.get(_push, "PUBLISHED")
    nj.notify_slack("operations", f"🌱 {title}", "Nova's daily unclaimed-time column.")
    nj.log(f"[unclaimed-digest] {_pub}: {title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
