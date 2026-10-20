#!/usr/bin/env python3
"""nova_corrections.py — "Nova's Corrections": a weekly column where Nova publishes what she got wrong.

Jordan (2026-10-19). Saturday 11:00 (scheduler.yaml corrections_weekly on .6), section Nova and Friends (was Opinions) — a
newspaper runs its corrections beside its opinion pages, and nearly everything Nova revises is a judgment
(what an alert meant, whether a fix held, what a show or a policy is), i.e. opinion-shaped.

Where corrections come from (the existing ledger + article history, nothing invented):
  1. explicit revisions in the belief ledger (nova_ops.beliefs old.superseded_by = new) booked this week
     from a DIFFERENT article than the earlier stance;
  2. near-duplicate stances (embedding cosine >= SIM_MIN) where a belief formed this week sits next to
     one held earlier from another article — the belief engine's own record of what she said, when.
  A model screens the candidate pairs for genuine contradiction (a reversal or a fact the later piece
  says was wrong — not a restatement, not the world changing between two dates). Each surviving pair is
  then GROUNDED in the two articles' text: the model must return a verbatim quote from each, and both
  quotes are checked against the published files. Only verified pairs are published; both articles are
  linked. A pair is published once (nova_ops.journal_corrections).

No genuine correction this week -> nothing is published, exit 0 with the reason logged.
CLI  nova_corrections.py [--days 7] [--dry-run]
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SECTION = "nova"          # Nova and Friends (Research + Opinions merged 2026-10-19)
SIM_MIN = 0.72
SIM_MAX = 0.985           # above this the two stances are the same sentence: a restatement
MAX_SCREEN = 160
MAX_CORRECTIONS = 6
ARTICLE_CHARS = 9000
MAX_ARTICLE_PAIRS = 10
ARTICLE_DIST_MAX = 0.2
ARTICLE_SECTIONS = ("nova", "opinions", "research", "tech", "cars", "watches", "food", "security",
                    "music", "military", "screen", "science", "mystery", "crime", "workshop", "travel",
                    "history", "belief")
MODEL = os.environ.get("NOVA_CORRECTIONS_MODEL", "anthropic/claude-sonnet-4.5")
HUGO_ROOT = Path.home() / "nova-journal"
SKIP_PREFIX = re.compile(r"^(work_lessons|ops|pursuit|chat|dream)s?:", re.I)
_IP_RX = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
SCHEMA = """CREATE TABLE IF NOT EXISTS journal_corrections (
    pair_key text PRIMARY KEY, earlier_url text NOT NULL, later_url text NOT NULL, earlier_quote text,
    later_quote text, published_in text, created_at timestamptz NOT NULL DEFAULT now())"""


def log(m):
    if not os.environ.get("NOVA_TEST_QUIET"):
        print(f"[corrections] {m}", flush=True)


# ─────────────────────────── pure ───────────────────────────
def norm(s: str) -> str:
    s = (s or "").lower().replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", s)).strip()


def quote_in(quote: str, text: str) -> bool:
    q = norm(quote)
    return len(q.split()) >= 5 and q in norm(text)


def clean_title(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`#]", "", t or "")).strip()


def pair_key(a_url: str, b_url: str, topic: str = "") -> str:
    return hashlib.sha1(f"{a_url}|{b_url}|{norm(topic)}".encode()).hexdigest()[:20]


def strip_front_matter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            text = text[end + 4:]
    return re.sub(r'<div class="nova-grounding".*?</div>', "", text, flags=re.S).strip()


def cosine_pairs(recent: list[dict], older: list[dict], vr, vo, lo=SIM_MIN, hi=SIM_MAX, cap=MAX_SCREEN) -> list[tuple]:
    """(sim, recent, older) for pairs from different articles in [lo, hi), best first, one per stance pair."""
    import numpy as np
    if not recent or not older:
        return []
    A = np.asarray(vr, float)
    B = np.asarray(vo, float)
    A /= np.linalg.norm(A, axis=1, keepdims=True) + 1e-9
    B /= np.linalg.norm(B, axis=1, keepdims=True) + 1e-9
    S = A @ B.T
    order = np.argsort(-S, axis=None)
    out, seen = [], set()
    for flat in order:
        i, j = divmod(int(flat), S.shape[1])
        s = float(S[i, j])
        if s < lo:
            break
        if s >= hi:
            continue
        a, b = recent[i], older[j]
        if clean_title(a["article"]) == clean_title(b["article"]):
            continue
        key = (norm(a["stance"]), norm(b["stance"]))
        if key in seen:
            continue
        seen.add(key)
        out.append((s, a, b))
        if len(out) >= cap:
            break
    return out


def parse_json(raw: str):
    if not raw:
        return None
    m = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    body = m.group(1) if m else raw
    start = min([i for i in (body.find("["), body.find("{")) if i >= 0], default=-1)
    if start < 0:
        return None
    for end in range(len(body), start, -1):
        try:
            return json.loads(body[start:end])
        except Exception:  # noqa: BLE001
            continue
    return None


def verify(judged: dict, earlier_text: str, later_text: str) -> dict | None:
    """A judged pair stands only when it is genuine and BOTH quotes are verbatim in their articles."""
    if not isinstance(judged, dict) or not judged.get("genuine"):
        return None
    eq, lq = str(judged.get("earlier_quote") or "").strip(), str(judged.get("later_quote") or "").strip()
    if not quote_in(eq, earlier_text) or not quote_in(lq, later_text):
        return None
    if _IP_RX.search(eq + lq):
        return None
    return {"earlier_quote": eq, "later_quote": lq, "what_changed": str(judged.get("what_changed") or "").strip(),
            "which_stands": str(judged.get("which_stands") or "unclear").strip(), "topic": str(judged.get("topic") or "").strip()}


def render_record(items: list[dict]) -> str:
    lines = ["\n\n## The record\n", "*Both sides of every correction, quoted exactly as published.*\n"]
    for i, c in enumerate(items, 1):
        lines.append(f"**{i}. {c['topic'] or 'Correction'}**\n")
        lines.append(f"- Earlier — [{c['earlier']['title']}]({c['earlier']['url']}) ({c['earlier']['date']}): "
                     f"“{c['earlier_quote']}”")
        lines.append(f"- Later — [{c['later']['title']}]({c['later']['url']}) ({c['later']['date']}): "
                     f"“{c['later_quote']}”")
        lines.append(f"- What stands: {c['which_stands']}\n")
    return "\n".join(lines) + "\n"


# ─────────────────────────── I/O ───────────────────────────
def _pg(db: str):
    import psycopg2
    import nova_dsn
    c = psycopg2.connect(nova_dsn.pg_dsn(db, "connect_timeout=10"))
    c.autocommit = True
    return c


def load_beliefs(days: int) -> tuple[list[dict], list[dict], list[tuple]]:
    """(recent, older, explicit revisions). Ledger rows from articles only (not work-lessons/ops)."""
    c = _pg("nova_ops")
    try:
        with c.cursor() as cur:
            cur.execute("SELECT id, stance, article_slug, first_held, first_held > now() - make_interval(days => %s) "
                        "FROM beliefs WHERE article_slug IS NOT NULL AND first_held > now() - interval '180 days'",
                        (int(days),))
            rows = [{"id": r[0], "stance": r[1], "article": r[2], "when": r[3], "recent": r[4]}
                    for r in cur.fetchall() if not SKIP_PREFIX.match(r[2] or "")]
            cur.execute("SELECT old.id, new.id FROM beliefs old JOIN beliefs new ON old.superseded_by = new.id "
                        "WHERE new.first_held > now() - make_interval(days => %s) AND old.article_slug <> new.article_slug",
                        (int(days),))
            revs = cur.fetchall()
    finally:
        c.close()
    return [r for r in rows if r["recent"]], [r for r in rows if not r["recent"]], revs


def _title_of(text: str) -> str:
    m = re.search(r'^title:\s*"?(.*?)"?\s*$', text[:1500], re.M)
    return m.group(1) if m else ""


def article_url(path: Path) -> str:
    """Hugo permalink: /<section>/<filename stem>/ (the date prefix stays in the URL)."""
    return f"https://nova.digitalnoise.net/{path.parent.name}/{path.stem}/"


def article_index(root: Path | None = None) -> dict:
    """clean title -> {id, title, url, path, date} for every published article on disk (the article's
    nova_articles memory id when it was re-ingested, else its URL)."""
    root = root or HUGO_ROOT
    ids = {}
    try:
        c = _pg("nova_memories")
        try:
            with c.cursor() as cur:
                cur.execute("SELECT id, metadata->>'path' FROM memories WHERE source='nova_articles' "
                            "AND metadata->>'type'='article'")
                ids = {p: i for i, p in cur.fetchall() if p}
        finally:
            c.close()
    except Exception as e:  # noqa: BLE001
        log(f"article memory ids unavailable ({e}); citing by URL only")
    idx = {}
    for f in sorted((root / "content").glob("*/*.md")):
        if f.name.startswith("_"):
            continue
        try:
            head = f.read_text(errors="ignore")[:1500]
        except OSError:
            continue
        raw = _title_of(head)
        t = clean_title(re.sub(r"^[^\w]+", "", raw)).lower()
        if not t:
            continue
        dm = re.match(r"(\d{4}-\d{2}-\d{2})", f.stem) or re.search(r"^date:\s*(\d{4}-\d{2}-\d{2})", head, re.M)
        rel = f"{f.parent.name}/{f.name}"
        idx[t] = {"id": ids.get(rel), "title": clean_title(raw), "url": article_url(f), "path": rel,
                  "date": dm.group(1) if dm else ""}
    return idx


def resolve(idx: dict, article: str) -> dict | None:
    a = idx.get(clean_title(re.sub(r"^[^\w]+", "", article or "")).lower())
    if not a:
        return None
    p = HUGO_ROOT / "content" / a["path"]
    if not p.exists():
        return None
    return dict(a, text=strip_front_matter(p.read_text())[:ARTICLE_CHARS])


def already(keys: list[str]) -> set:
    c = _pg("nova_ops")
    try:
        with c.cursor() as cur:
            cur.execute(SCHEMA)
            cur.execute("SELECT pair_key FROM journal_corrections WHERE pair_key = ANY(%s)", (keys,))
            return {r[0] for r in cur.fetchall()}
    finally:
        c.close()


def record(items: list[dict], published_in: str):
    c = _pg("nova_ops")
    try:
        with c.cursor() as cur:
            cur.execute(SCHEMA)
            for it in items:
                cur.execute("INSERT INTO journal_corrections (pair_key, earlier_url, later_url, earlier_quote, "
                            "later_quote, published_in) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (it["key"], it["earlier"]["url"], it["later"]["url"], it["earlier_quote"],
                             it["later_quote"], published_in))
    finally:
        c.close()


def _llm(system: str, user: str) -> str | None:
    import nova_journal as nj
    return nj.call_openrouter(system, user, model=MODEL, max_tokens=6000, timeout=600)


# ─────────────────────────── passes ───────────────────────────
def candidates(days: int) -> list[tuple]:
    recent, older, revs = load_beliefs(days)
    log(f"ledger: {len(recent)} stance(s) this window, {len(older)} earlier, {len(revs)} cross-article revision(s)")
    by_id = {r["id"]: r for r in recent + older}
    out = [(1.0, by_id[n], by_id[o]) for o, n in revs if o in by_id and n in by_id]
    if recent and older:
        import nova_beliefs
        vecs = []
        texts = [r["stance"] for r in recent + older]
        for k in range(0, len(texts), 64):
            v = nova_beliefs.embed(texts[k:k + 64])
            if not v:
                log("embedding failed — screening explicit revisions only")
                vecs = None
                break
            vecs += v
        if vecs:
            out += cosine_pairs(recent, older, vecs[:len(recent)], vecs[len(recent):])
    return out[:MAX_SCREEN]


def screen(pairs: list[tuple], llm) -> list[tuple]:
    if not pairs:
        return []
    listing = "\n".join(f"{i}. LATER: {a['stance']}\n   EARLIER: {b['stance']}" for i, (_s, a, b) in enumerate(pairs))
    raw = llm("You judge whether two statements by the same author CONTRADICT. Output JSON only.",
              "Each numbered pair is a LATER stance and an EARLIER stance by Nova. A pair CONTRADICTS when the "
              "later one reverses, denies, or corrects the earlier one about the SAME thing (a fact, a cause, a "
              "verdict, a prediction). It does NOT contradict when it restates, narrows, extends or adds detail, "
              "or when the two describe different days/events (the world changing is not a correction).\n\n"
              f"{listing}\n\nReturn a JSON array of the numbers of pairs that genuinely contradict ([] if none).")
    got = parse_json(raw)
    keep = []
    for k in got if isinstance(got, list) else []:
        try:
            keep.append(pairs[int(k)])
        except (ValueError, TypeError, IndexError):
            continue
    return keep


def ground_articles(ea: dict, la: dict, earlier_hint: str, later_hint: str, llm) -> dict | None:
    """Ask for a verbatim quote from each article; keep the pair only when both quotes check out."""
    raw = llm("You check a possible self-correction between two published articles. Output one JSON object only.",
              f"EARLIER ARTICLE ({ea['date']}): {ea['title']}\n{ea['text']}\n\n"
              f"LATER ARTICLE ({la['date']}): {la['title']}\n{la['text']}\n\n"
              + (f"Ledger says EARLIER held: {earlier_hint}\nand LATER holds: {later_hint}\n\n" if earlier_hint else "")
              + "Does the later article genuinely contradict or correct the earlier one on the same point — a "
              "reversed verdict, a fact or cause it now says was wrong, a prediction it now says failed (not the "
              "world changing between dates, not a restatement, not a different event)? Return JSON: "
              '{"genuine": true|false, "topic": "<4-8 words>", "earlier_quote": "<verbatim 8-40 words from the '
              'EARLIER article>", "later_quote": "<verbatim 8-40 words from the LATER article>", '
              '"what_changed": "<one sentence>", "which_stands": "<later|earlier|partly both|unclear> — one clause why"}. '
              "Quotes must be copied EXACTLY from the article text.")
    j = parse_json(raw)
    v = verify(j, ea["text"], la["text"])
    if not v:
        log(f"  not a verified correction: {ea['title'][:50]!r} -> {la['title'][:50]!r} "
            f"(genuine={isinstance(j, dict) and j.get('genuine')})")
        return None
    v.update(earlier={k: ea[k] for k in ("id", "title", "url", "date")},
             later={k: la[k] for k in ("id", "title", "url", "date")},
             key=pair_key(ea["url"], la["url"], v["topic"] or earlier_hint))
    return v


def ground(later_belief: dict, earlier_belief: dict, idx: dict, llm) -> dict | None:
    la, ea = resolve(idx, later_belief["article"]), resolve(idx, earlier_belief["article"])
    if la and ea and ea["date"] > la["date"]:        # order by when the ARTICLES ran, not when the ledger booked them
        la, ea = ea, la
        later_belief, earlier_belief = earlier_belief, later_belief
    if not la or not ea or la["url"] == ea["url"] or ea["date"] == la["date"]:
        log(f"  unresolvable pair: {later_belief['article'][:60]!r} vs {earlier_belief['article'][:60]!r}")
        return None
    return ground_articles(ea, la, earlier_belief["stance"], later_belief["stance"], llm)


def article_pairs(days: int, idx: dict, limit: int = MAX_ARTICLE_PAIRS) -> list[tuple[dict, dict]]:
    """This window's opinion-bearing articles x their nearest earlier article (pgvector over Nova's
    re-ingested articles). Weekly roundups are summaries of other posts, so they are skipped."""
    c = _pg("nova_memories")
    try:
        with c.cursor() as cur:
            cur.execute("""
                WITH a AS (SELECT metadata->>'title' t, metadata->>'path' p, metadata->>'section' s,
                                  (metadata->>'date')::timestamptz d, embedding
                           FROM memories WHERE source='nova_articles' AND metadata->>'type'='article'
                           AND embedding IS NOT NULL AND metadata->>'title' !~* 'this week in')
                SELECT l.t, e.t, e.dist FROM a l CROSS JOIN LATERAL (
                    SELECT b.t, (b.embedding <=> l.embedding) dist FROM a b
                    WHERE b.d < l.d - interval '1 day' AND b.d > l.d - interval '180 days' AND b.p <> l.p
                    ORDER BY b.embedding <=> l.embedding LIMIT 1) e
                WHERE l.d > now() - make_interval(days => %s) AND l.s = ANY(%s) AND e.dist <= %s
                ORDER BY e.dist LIMIT %s""", (int(days), list(ARTICLE_SECTIONS), ARTICLE_DIST_MAX, int(limit)))
            rows = cur.fetchall()
    finally:
        c.close()
    out = []
    for lt, et, _d in rows:
        la, ea = resolve(idx, lt), resolve(idx, et)
        if la and ea and la["url"] != ea["url"] and ea["date"] < la["date"]:
            out.append((ea, la))
    return out


def write(items: list[dict], llm) -> tuple[str, str] | None:
    import nova_voice
    ctx = ("Write this week's NOVA'S CORRECTIONS column for her journal (Opinions). Nova publishes where her own "
           "later writing contradicted her earlier writing. Voice: hers — dry, honest, a little rueful, never "
           "groveling; owning an error is a point of pride. For each correction: what she said before (quote it "
           "exactly as given), what she said later (quote exactly), which one stands and why, and what she "
           "changes in how she works. Link both articles using the markdown links given. Never add a "
           "correction, quote, date or number that is not in the material. Open with one short paragraph on "
           "why she runs corrections at all; close with one line. 500-1100 words, markdown, ### per "
           "correction, no H1.\n\nOUTPUT EXACTLY THIS SHAPE:\nTITLE: <a punchy title, no quotes>\n<blank line>\n<the body>")
    system = nova_voice.system_prompt(ctx, section=SECTION, topic="corrections error honesty changed mind")
    mat = "\n\n".join(
        f"CORRECTION {i}: {c['topic']}\n  earlier: [{c['earlier']['title']}]({c['earlier']['url']}) {c['earlier']['date']}\n"
        f"  earlier quote: \"{c['earlier_quote']}\"\n  later: [{c['later']['title']}]({c['later']['url']}) {c['later']['date']}\n"
        f"  later quote: \"{c['later_quote']}\"\n  what changed: {c['what_changed']}\n  which stands: {c['which_stands']}"
        for i, c in enumerate(items, 1))
    raw = llm(system, f"THIS WEEK'S VERIFIED CORRECTIONS:\n\n{mat}\n\nWrite the column.")
    if not raw or len(raw) < 400:
        return None
    title, body = None, []
    for ln in raw.splitlines():
        if title is None and ln.upper().startswith("TITLE:"):
            title = ln.split(":", 1)[1].strip().strip('"')
        else:
            body.append(ln)
    return (title or "Nova's Corrections"), "\n".join(body).strip()


def run(days: int = 7, dry_run: bool = False, llm=None) -> dict:
    llm = llm or _llm
    pairs = candidates(days)
    flagged = screen(pairs, llm) if pairs else []
    log(f"{len(pairs)} candidate pair(s) -> {len(flagged)} flagged as contradictions")
    idx = article_index() if flagged else None
    items, keys = [], set()
    for _s, later_b, earlier_b in flagged:
        log(f"  flagged: LATER {later_b['stance'][:90]!r} / EARLIER {earlier_b['stance'][:90]!r}")
        if len(items) >= MAX_CORRECTIONS:
            break
        g = ground(later_b, earlier_b, idx, llm)
        if g and g["key"] not in keys:
            keys.add(g["key"])
            items.append(g)
    if len(items) < MAX_CORRECTIONS:
        try:
            idx = idx or article_index()
            apairs = article_pairs(days, idx)
            log(f"{len(apairs)} near-duplicate article pair(s) from this window to check")
            for ea, la in apairs:
                if len(items) >= MAX_CORRECTIONS:
                    break
                g = ground_articles(ea, la, "", "", llm)
                if g and g["key"] not in keys:
                    keys.add(g["key"])
                    items.append(g)
        except Exception as e:  # noqa: BLE001
            log(f"article-history pass skipped: {e}")
    if items:
        done = already([i["key"] for i in items])
        items = [i for i in items if i["key"] not in done]
    if not items:
        reason = "no genuine, quote-verified correction this week"
        log(f"{reason} — not publishing")
        return {"status": "skipped", "reason": reason, "candidates": len(pairs), "flagged": len(flagged)}
    art = write(items, llm)
    if not art:
        return {"status": "failed", "reason": "column pass produced nothing"}
    title, body = art
    if not title.lower().startswith("nova's corrections"):
        title = f"Nova's Corrections: {title}"
    body = _IP_RX.sub("[address]", body) + render_record(items)
    res = {"status": "dry-run" if dry_run else "published", "title": title, "corrections": len(items)}
    if dry_run:
        Path(os.environ.get("NOVA_CORRECTIONS_PREVIEW", "/tmp/corrections_preview.md")).write_text(f"# {title}\n\n{body}")
        return res
    import nova_journal as nj
    sources = "\n\n".join(f"[{i}] (nova_articles, {c['earlier']['url']}) {c['earlier_quote']}\n\n"
                          f"[{i}b] (nova_articles, {c['later']['url']}) {c['later_quote']}" for i, c in enumerate(items, 1))
    cited = [i for i in dict.fromkeys([c["earlier"]["id"] for c in items] + [c["later"]["id"] for c in items]) if i]
    if not nj.publish_hugo(title, body, SECTION, ["corrections", "weekly", "beliefs", "honesty"],
                           "Nova's weekly corrections: where her later writing contradicted her earlier writing, both quoted.",
                           emoji="✏️", cited_memory_ids=cited, sources=sources, profile="corrections"):
        return {"status": "failed", "reason": "publish guard rejected"}
    res["push"] = nj.git_push(SECTION, title)
    record(items, title)
    try:
        nj.notify_slack(SECTION, f"✏️ {title}", f"{len(items)} correction(s) this week.")
    except Exception:  # noqa: BLE001
        pass
    log(f"published: {title} ({len(items)} corrections)")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    res = run(a.days, a.dry_run)
    print(json.dumps(res, default=str))
    return 0 if res["status"] in ("published", "skipped", "dry-run") else 1


if __name__ == "__main__":
    sys.exit(main())
