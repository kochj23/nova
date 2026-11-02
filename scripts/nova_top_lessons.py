#!/usr/bin/env python3
"""nova_top_lessons.py — the daily 06:00 "Top 10 lessons" article in /operations.

Little Mister, 2026-11-01: "I want a new daily 6am report that goes over the top 10 new lessons for a 24hr
period with what caused it and if it should be a belief or not."

Every lesson Nova draws — from her own operations (nova_ops_lessons.py: failing jobs, AARs, incidents,
decisions on her proposals) and from what she reads (nova_work_lessons.py) — is offered to the belief
pipeline as a belief_candidates row. That table is therefore the one place where "a new lesson" exists for
both sources, with its evidence and its belief-worthiness score. This job takes the window's candidates,
ranks them by that score (nova_beliefs.value_score: generality, relevance to her domains, novelty,
credibility, independent sources), folds near-duplicates (an ops lesson re-drawn every 2 h is one lesson
seen N times), and renders the top 10 with:

  * Lesson   — the candidate stance, as offered.
  * Cause    — the evidence resolved to what actually happened: the failing job and its real error line, the
               AAR's "why", the incident and how often it opened, the proposal and its decision, or the work
               read and the quote the lesson stands on.
  * Belief?  — Yes / Already believed / Questionable / No (or "No, but close"), with the reason.

The pipeline's score alone is not a judgment: on the first run two of the top five were "security warnings are
ignored" lessons drawn from a backup job whose only "error" was OpenSSH's post-quantum notice on stderr. So:
  - a deterministic rule marks an ops lesson Questionable when every job it cites has no real error line;
  - each of the ten gets ONE local-model review (nova_beliefs.llm_json: qwen3:30b-a3b, local only) that sees
    the lesson AND its resolved cause; where it disagrees with the pipeline the verdict says so.
If the model is unreachable the pipeline verdict stands alone. The article body is otherwise stored rows and
lookups (NOVA_IDEAL_READER=0). Rejected candidates (scripture, fragments, fringe, no evidence) never rank.

Scheduled daily 06:00 (config/scheduler.yaml task top_lessons, --scheduled). Stable slug
top-lessons-YYYY-MM-DD so a re-run overwrites the day instead of double-posting.

Usage: nova_top_lessons.py [--hours 24] [--top 10] [--scheduled] [--force] [--dry-run] [--selftest]
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_dsn  # noqa: E402

OPS_DSN = nova_dsn.pg_dsn("nova_ops")
SITE = "https://nova.digitalnoise.net"
SECTION = "operations"
TAGS = ["lessons-learned", "beliefs", "top-10", "daily"]
EMOJI = "🧭"
TOP = 10
DUP_COS = 0.90            # nova_beliefs.SAME_COS: same claim
MIN_SCORE = 0.72          # nova_beliefs.MIN_SCORE: below this a candidate never becomes a belief
WEAK_GEN, WEAK_REL = 0.6, 0.55

ORIGIN = {"ops": "my operations", "work_lessons": "my reading", "news": "corroborated news"}

_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b")
_HOST = re.compile(r"\b(?:nova-core\d+|office-m4-?\d*|pg-primary|tv-movies-\d+|jordans-mac-mini)"
                   r"(?:\.digitalnoise\.net)?\b", re.I)
_PATH = re.compile(r"(?:/Users|/home|/Volumes|/opt|/mnt|/nova)/[^\s'\"),]+")
_NOISE = re.compile(r"post-quantum|store now, decrypt later|openssh\.com/pq|^\s*\*\*\s*$|^\s*[~^]+\s*$", re.I)
_ERRLINE = re.compile(r"error|exception|timed? ?out|failed|refused|denied|not found|traceback|killed", re.I)
NO_ERROR = "its output holds no actual error line"


def scrub(t: str) -> str:
    """Public-site safe: no IPs, internal host names or filesystem paths."""
    t = _IP.sub("[addr]", t or "")
    t = _HOST.sub("[host]", t)
    t = _PATH.sub("[path]", t)
    return re.sub(r"\s+", " ", t).strip()


def clip(t: str, n: int) -> str:
    t = scrub(t)
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "…"


def error_gist(tail: str) -> str:
    """The one line of a job's output that says what went wrong; known noise (the OpenSSH PQ notice) dropped."""
    lines = [l.strip() for l in (tail or "").splitlines() if l.strip() and not _NOISE.search(l)]
    return next((l for l in reversed(lines) if _ERRLINE.search(l)), "")


def cosine(a, b) -> float:
    if not a or not b:
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5
    db = sum(y * y for y in b) ** 0.5
    return num / (da * db) if da and db else 0.0


# ─────────────────────────── verdict (pure) ───────────────────────────
def verdict(c: dict) -> tuple[str, str]:
    """(label, why) from the belief pipeline's own state and scores."""
    st, sc = c.get("status"), c.get("score") or 0.0
    gen, rel = c.get("generality"), c.get("relevance")
    if st in ("promoted", "revised"):
        what = "booked as" if st == "promoted" else "revised"
        if (gen is not None and gen < WEAK_GEN) or (rel is not None and rel < WEAK_REL):
            weak = "too specific" if gen is not None and gen < WEAK_GEN else "barely touches my domains"
            return "Questionable", f"{what} belief #{c.get('belief_id')}, but it is {weak} — worth an audit"
        return "Yes", f"{what} belief #{c.get('belief_id')}, score {sc:.2f}"
    if st == "reinforced":
        return "Already believed", f"strengthened belief #{c.get('belief_id')}" + (
            f": “{clip(c['belief_stance'], 160).rstrip('.')}”" if c.get("belief_stance") else "")
    if st == "pending":
        if sc >= MIN_SCORE:
            return "Yes", f"scores {sc:.2f} against the {MIN_SCORE} bar; the next belief promotion should book it"
        reasons = []
        if gen is not None and gen < 0.5:
            reasons.append("too specific to one case")
        if rel is not None and rel < 0.55:
            reasons.append("off my domains")
        if (c.get("near_cos") or 0) >= 0.85:
            reasons.append(f"near-duplicate of belief #{c.get('near_id')}")
        return "No", f"scores {sc:.2f}, under the {MIN_SCORE} bar" + (f" — {', '.join(reasons)}" if reasons else "")
    return "No", f"{st}: {c.get('reason') or 'not belief material'}"


def unsupported(c: dict) -> bool:
    """An ops lesson whose only evidence is jobs whose output holds no real error: meaning read into noise."""
    return c.get("origin") == "ops" and bool(c.get("cause")) and all(NO_ERROR in x for x in c["cause"])


def combine(pipe: tuple[str, str], rev: dict | None, noise: bool = False) -> tuple[str, str]:
    """Final (label, why): the pipeline verdict, overruled or confirmed by the noise rule and the review."""
    label, why = pipe
    if noise and label in ("Yes", "Already believed", "Questionable"):
        return "Questionable", ("the only evidence is a job whose output holds no real error (its stderr is "
                                "OpenSSH's post-quantum notice), so the lesson reads meaning into noise "
                                f"(pipeline: {why})")
    if rev is None:
        return label, why
    if label in ("Yes", "Already believed") and not rev["belief"]:
        return "Questionable", f"{rev['why']} (pipeline: {why})"
    if label == "No" and rev["belief"]:
        return "No, but close", f"{why}; my review would keep it: {rev['why']}"
    if label == "Questionable" and rev["belief"]:
        return "Yes", f"{rev['why']} (pipeline: {why})"
    return label, f"{rev['why']} (pipeline: {why})"


def fold(cands: list[dict]) -> list[dict]:
    """Best-first candidates with near-duplicates folded into the first (seen += 1)."""
    out: list[dict] = []
    for c in sorted(cands, key=lambda x: -(x.get("score") or 0)):
        twin = next((o for o in out if c.get("embedding") and cosine(o.get("embedding"), c["embedding"]) >= DUP_COS),
                    None)
        if twin is None and c.get("stance"):
            twin = next((o for o in out if o["stance"].strip().lower() == c["stance"].strip().lower()), None)
        if twin:
            twin["seen"] += 1
            twin["evidence"] = list(dict.fromkeys(twin["evidence"] + c["evidence"]))
            continue
        out.append({**c, "seen": 1, "evidence": list(c.get("evidence") or [])})
    return out


# ─────────────────────────── causes ───────────────────────────
def _one(oc, sql, params):
    try:
        oc.execute(sql, params)
        return oc.fetchone()
    except Exception:  # noqa: BLE001 — a missing table or bad ref just drops that cause line
        return None


def cause(oc, c: dict, start: datetime, end: datetime) -> list[str]:
    """Human lines for what produced the lesson."""
    if c["origin"] == "work_lessons":
        title = (c.get("title") or "").strip() or next(
            (e.split("work ", 1)[-1] for e in c["evidence"] if e.startswith("article:work ")), "a work I read")
        q = (c.get("quote") or "").strip()
        return [f"Reading *{clip(title, 120)}*" + (f": “{clip(q, 200)}”" if q else "")]
    lines = []
    ms = (int(start.timestamp() * 1000), int(end.timestamp() * 1000))
    for ref in c["evidence"][:4]:
        kind, _, key = ref.partition(":")
        if kind == "scheduler":
            r = _one(oc, "SELECT count(*) FILTER (WHERE status <> 'success'), count(*), "
                         "(array_agg(coalesce(error_tail,'') || chr(10) || coalesce(stdout_tail,'') "
                         " ORDER BY started_at DESC) FILTER (WHERE status <> 'success'))[1] FROM scheduler_runs "
                         "WHERE task_id=%s AND started_at > %s AND started_at <= %s AND status <> 'running'",
                     (key, *ms))
            if r and r[1]:
                gist = clip(error_gist(r[2] or ""), 200)
                lines.append(f"Scheduled job `{key}` failed {r[0]} of {r[1]} finished runs"
                             + (f" — last error: {gist}" if gist else f" — {NO_ERROR}"))
            else:
                lines.append(f"Scheduled job `{key}` kept failing")
        elif kind == "hotwash" and key.isdigit():
            r = _one(oc, "SELECT kind, subject, q3_why FROM hotwash WHERE id=%s", (int(key),))
            if r:
                lines.append(f"After-action review ({r[0].replace('_', ' ')}: {clip(r[1], 80)}) — why: {clip(r[2], 220)}")
        elif kind == "incident" and key.isdigit():
            r = _one(oc, "SELECT title, recurrence_key FROM telemetry.incidents WHERE id=%s", (int(key),))
            if r:
                n = _one(oc, "SELECT count(*) FROM telemetry.incidents WHERE recurrence_key=%s "
                             "AND opened_at > %s - interval '7 days'", (r[1], end))
                lines.append(f"Recurring incident “{clip(r[0], 120)}” — opened {n[0] if n else '?'} times in 7 days")
        elif kind == "postmortem" and key.isdigit():
            r = _one(oc, "SELECT title, trigger_reason FROM postmortems WHERE id=%s", (int(key),))
            if r:
                lines.append(f"Postmortem “{clip(r[0], 120)}”" + (f" ({clip(r[1], 80)})" if r[1] else ""))
        elif kind == "coagency" and key.isdigit():
            r = _one(oc, "SELECT status, proposed_action, coalesce(decision_note,'') FROM coagency_proposals "
                         "WHERE id=%s", (int(key),))
            if r:
                lines.append(f"My proposal “{clip(r[1], 140)}” was {r[0]}" + (f" — {clip(r[2], 160)}" if r[2] else ""))
        elif kind == "claude_queue" and key.isdigit():
            r = _one(oc, "SELECT status, description FROM claude_queue WHERE id=%s", (int(key),))
            if r:
                lines.append(f"Work item ({r[0]}): {clip(r[1], 160)}")
        elif kind == "gateway_trace":
            lines.append("A chat turn that failed or fell to the rescue path")
    return lines or [f"From {ORIGIN.get(c['origin'], c['origin'])}"]


def change_line(oc, cid: int) -> str | None:
    r = _one(oc, "SELECT change FROM ops_lessons WHERE candidate_id=%s AND coalesce(change,'') <> '' LIMIT 1", (cid,))
    return clip(r[0], 200) if r else None


def review_prompt(c: dict) -> str:
    causes = "\n".join(f"- {x}" for x in c["cause"])
    if c.get("origin") == "work_lessons":
        test = ("This lesson came from something you READ; the quote is its evidence, and a quote that illustrates "
                "a sound principle IS enough support. Answer true if the principle is sound, general beyond this "
                "one story, and would sharpen how you run systems, protect the household, or judge people and "
                "claims. Answer false for plot trivia, fiction or a TV episode presented as real-world fact, "
                "a platitude anyone would agree with, or a claim the quote does not actually make.")
    else:
        test = ("This lesson came from your own OPERATIONS. Answer true if the evidence above really shows the "
                "pattern and it would change how you run things. Answer false if it reads meaning into noise "
                "(a harmless warning, a job with no real error), generalises a few transient timeouts into a "
                "design flaw, or just restates a standing rule.")
    return ("You are Nova, deciding whether a lesson you drew should become one of your durable beliefs.\n"
            f"LESSON: {c['stance']}\nWHAT CAUSED IT:\n{causes}\n\n{test} "
            "Do not restate the lesson; say what in the cause does or does not support it. Reply as JSON: "
            '{"belief": true|false, "why": "<one plain sentence, 25 words max, first person>"}')


def review(c: dict) -> dict | None:
    """One local-model second opinion, or None when the model is unreachable or answers junk."""
    import nova_beliefs as nb
    for _ in range(2):
        try:
            got = parse_review(nb.llm_json(review_prompt(c), max_tokens=120))
        except Exception:  # noqa: BLE001
            got = None
        if got:
            return got
    return None


def parse_review(raw: str | None) -> dict | None:
    """{"belief": bool, "why": str}. qwen3 sometimes mangles the key ("": "...", " ": "..."), so the reason
    is the "why" value or else the longest other string value."""
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("belief"), bool):
        return None
    why = d.get("why")
    if not isinstance(why, str) or not why.strip():
        why = max((v for k, v in d.items() if k != "belief" and isinstance(v, str)), key=len, default="")
    why = why.strip()
    return {"belief": d["belief"], "why": clip(why.rstrip("."), 240)} if len(why.split()) >= 3 else None


# ─────────────────────────── data ───────────────────────────
def load(oc, start: datetime, end: datetime) -> tuple[list[dict], dict]:
    import nova_beliefs as nb
    live = {c["id"]: c for c in nb.score_pending(oc)}          # pending scores are computed, not stored
    oc.execute("""SELECT c.id, c.origin, c.ref, c.title, c.stance, c.quote, c.evidence, c.status, c.reason, c.score,
                         c.scores, c.belief_id, c.embedding, b.stance
                  FROM belief_candidates c LEFT JOIN beliefs b ON b.id = c.belief_id
                  WHERE c.created_at > %s AND c.created_at <= %s""", (start, end))
    cands, counts = [], {}
    for (cid, origin, ref, title, stance, quote, ev, status, reason, score, scores, bid, emb, bstance) in oc.fetchall():
        counts[status] = counts.get(status, 0) + 1
        counts[origin] = counts.get(origin, 0) + 1
        if status == "rejected":
            continue
        s = scores or {}
        c = {"id": cid, "origin": origin, "ref": ref, "title": title, "stance": stance, "quote": quote,
             "evidence": list(ev or []), "status": status, "reason": reason, "score": score,
             "generality": s.get("generality"), "relevance": s.get("relevance"), "near_cos": s.get("near_cos"),
             "belief_id": bid, "belief_stance": bstance, "embedding": list(emb) if emb else None}
        if status == "pending" and cid in live:
            p = live[cid]
            c.update(score=p["score"], generality=p["generality"], relevance=p["relevance"],
                     near_cos=p["near_cos"], near_id=p["near_id"], embedding=p["embedding"])
        if c["score"] is not None:
            cands.append(c)
    return cands, counts


# ─────────────────────────── render ───────────────────────────
def render(top: list[dict], counts: dict, end: datetime, n_lessons: int) -> str:
    total = sum(v for k, v in counts.items() if k in ORIGIN)
    yes = sum(1 for c in top if c["verdict"][0] == "Yes")
    q = sum(1 for c in top if c["verdict"][0] == "Questionable")
    p = [f"In the 24 hours to {end:%-I:%M %p %Z} on {end:%A, %B %-d}, I drew **{total} lessons**: "
         f"{counts.get('ops', 0)} from my own operations and {counts.get('work_lessons', 0)} from what I read. "
         f"After folding repeats there were {n_lessons} distinct ones; here are the {len(top)} that score highest "
         f"on how much they deserve to become something I believe. {yes} should be beliefs"
         + (f"; {q} {'is' if q == 1 else 'are'} questionable — booked or scored high, but the cause does not hold up"
            if q else "") + ".", ""]
    for i, c in enumerate(top, 1):
        label, why = c["verdict"]
        seen = f" · seen {c['seen']}×" if c["seen"] > 1 else ""
        p += [f"## {i}. {clip(c['stance'], 300)}", "",
              f"*From {ORIGIN.get(c['origin'], c['origin'])} · score {c['score']:.2f}{seen}*", "",
              "**What caused it**", ""]
        p += [f"- {line}" for line in c["cause"]]
        if c.get("change"):
            p += [f"- What it would change: {c['change']}"]
        p += ["", f"**Should it be a belief?** **{label}** — {why.rstrip('.')}.", ""]
    p += ["---", "", "**How this is ranked.** Each lesson carries the belief pipeline's own value score "
          "(generality, relevance to my domains, novelty against what I already believe, source credibility, "
          f"independent sources); the bar for a belief is {MIN_SCORE}. Each of the {len(top)} then gets one "
          "local-model review of the lesson against its actual cause. *Questionable* means the pipeline would keep "
          "it but the review, its own weak scores, or an evidence trail with no real error says the cause does "
          "not support it — those are candidates to retire. Rejected lessons (fragments, scripture, fringe "
          f"sources, no evidence) never rank; {counts.get('rejected', 0)} were rejected in this window.", ""]
    return "\n".join(p)


def run(hours: float = 24, top_n: int = TOP, dry_run: bool = False, scheduled: bool = False,
        force: bool = False) -> int:
    import psycopg2
    end = datetime.now().astimezone()
    start = end - timedelta(hours=hours)
    stamp = end.date()
    slug = f"top-lessons-{stamp}"
    conn = psycopg2.connect(OPS_DSN, connect_timeout=10); conn.autocommit = True
    oc = conn.cursor()
    if scheduled and not force:
        import nova_journal as nj
        if (nj.HUGO_ROOT / f"content/{SECTION}/{slug}.md").exists():
            print(f"[top_lessons] {slug} already published"); return 0
    cands, counts = load(oc, start, end)
    folded = fold(cands)
    if not folded:
        print(f"[top_lessons] no lessons between {start:%Y-%m-%d %H:%M} and {end:%H:%M}; no article"); return 0
    top = folded[:top_n]
    for c in top:
        c["cause"] = cause(oc, c, start, end)
        c["change"] = change_line(oc, c["id"]) if c["origin"] == "ops" else None
        c["verdict"] = combine(verdict(c), review(c), unsupported(c))
    title = f"Top {len(top)} lessons — {stamp:%B %-d, %Y}"
    body = render(top, counts, end, len(folded))
    if dry_run:
        print(title, slug, sep="\n"); print(body); return 0
    os.environ["NOVA_IDEAL_READER"] = "0"          # a ledger is not an essay; nothing gets cut
    import nova_journal as nj
    yes = sum(1 for c in top if c["verdict"][0] == "Yes")
    desc = (f"The {len(top)} best new lessons of the last 24 hours, what caused each, and whether it should "
            f"be a belief ({yes} yes).")
    if not nj.publish_hugo(title, body, SECTION, TAGS, desc, emoji=EMOJI, stable_slug=slug):
        raise SystemExit("[top_lessons] publish refused")
    result = nj.git_push(SECTION, title)
    if result != nj.PUSH_PUSHED:
        raise SystemExit(f"[top_lessons] git: {result}")
    print(f"[top_lessons] published {SITE}/{SECTION}/{slug}/")
    return 0


def selftest() -> int:
    assert "[addr]" in scrub("down at 192.168.1.10:5432") and "[host]" in scrub("nova-core5 died")
    assert "[path]" in scrub("see /Users/someone/x.log now")
    assert verdict({"status": "promoted", "score": 0.9, "generality": 1.0, "relevance": 0.62, "belief_id": 7})[0] == "Yes"
    assert verdict({"status": "promoted", "score": 0.9, "generality": 0.4, "relevance": 0.62,
                    "belief_id": 7})[0] == "Questionable"
    assert verdict({"status": "reinforced", "belief_id": 3, "belief_stance": "x"})[0] == "Already believed"
    assert verdict({"status": "pending", "score": 0.8})[0] == "Yes"
    v = verdict({"status": "pending", "score": 0.5, "generality": 0.3, "relevance": 0.5})
    assert v[0] == "No" and "too specific" in v[1] and "off my domains" in v[1]
    assert combine(("Yes", "booked"), {"belief": False, "why": "noise"})[0] == "Questionable"
    assert combine(("No", "low"), {"belief": True, "why": "real"})[0] == "No, but close"
    assert combine(("Yes", "booked"), None) == ("Yes", "booked")
    assert combine(("Yes", "booked"), {"belief": True, "why": "ok"}, noise=True)[0] == "Questionable"
    assert unsupported({"origin": "ops", "cause": [f"Scheduled job `x` failed 2 of 9 finished runs — {NO_ERROR}"]})
    assert not unsupported({"origin": "ops", "cause": ["Scheduled job `x` — last error: Timed out", NO_ERROR]})
    pq = "** WARNING: connection is not using a post-quantum key exchange algorithm.\n** The server may need..."
    assert error_gist(pq) == "" and error_gist("ok\nTimed out after 120s\n") == "Timed out after 120s"
    f = fold([{"stance": "A b c.", "score": 0.9, "embedding": [1, 0], "evidence": ["x"]},
              {"stance": "A b c!", "score": 0.8, "embedding": [0.99, 0.01], "evidence": ["y"]},
              {"stance": "Other.", "score": 0.85, "embedding": [0, 1], "evidence": ["z"]}])
    assert len(f) == 2 and f[0]["seen"] == 2 and f[0]["evidence"] == ["x", "y"] and f[1]["stance"] == "Other."
    assert clip("word " * 100, 20).endswith("…")
    assert parse_review('{"belief": false, " ": "A fictional plot point, not reality."}')["why"] == \
        "A fictional plot point, not reality"
    assert parse_review('{"belief": true, "why": "Real outage shows it."}')["belief"] is True
    assert parse_review('{"belief": "yes"}') is None and parse_review("nope") is None
    print("selftest OK")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--top", type=int, default=TOP)
    ap.add_argument("--scheduled", action="store_true", help="skip if today's article exists")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    return run(a.hours, a.top, a.dry_run, a.scheduled, a.force)


if __name__ == "__main__":
    sys.exit(main())
