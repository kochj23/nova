#!/usr/bin/env python3
"""nova_ideal_reader.py — the second pass: Nova's rule-following editor.

Two rules from two books:
  * "Ideal Reader" (Stephen King, On Writing): write the first draft with the door
    closed, rewrite with the door open, and "2nd draft = 1st draft - 10%".
  * "Ozzie's Rule" (Koontz, Odd Thomas — Little Ozzie's advice on style): cut the
    tics, keep the voice.

edit(text, kind) runs over every outgoing piece in the files this lane owns (journal
articles before publish_hugo writes them, reach messages after ground_reach). It is a
DELETION-ONLY editor, deterministic, no model call — so it can never introduce a new
factual claim. It:
  1. strips Nova's stock lead-ins ("Here's the thing:", "Let me be direct:") and
     drops her stock throwaway sentences ("That matters.", "Think about that.");
  2. cuts adverb clutter ("really", "genuinely", "incredibly", ... — never after a
     negation, where the adverb carries meaning);
  3. humility check: grandiose adjectives in first-person sentences are cut, and a
     sentence that claims singular greatness ("only I could", "I alone") is dropped;
  4. drops sentences that only restate what the previous sentences already said,
     weakest first, until the cut reaches the target (10% at the default verbosity
     dial; nova_voice.dial_scale('verbosity', 0.15, 0.10, 0.05)), never more than
     MAX_CUT.
Protected, never touched: the Sources/Attribution section, headings, lists, tables,
quotes, code, image/link lines, the dateline/byline, any sentence carrying "It's all
for you, Damien!" / Little Mister / a question / an exclamation / emphasis (the joke
usually lives there — keep exactly-one-real-joke, don't edit the punchline).

Hard post-conditions, checked on every call (fail -> the ORIGINAL text is returned):
the output's content words are a subset of the input's (no new claims), no new
numbers, the Sources tail is byte-identical, and the cut never exceeds MAX_CUT.

"Darlings" — her real top n-gram tics — are computed from the last 200 journal
pieces in ~/nova-journal/content and stored in nova_ops.ideal_reader_darlings,
refreshed weekly (--refresh-darlings). Every edit is logged to
nova_ops.ideal_reader_log (words before/after, what was cut).

  nova_ideal_reader.py --refresh-darlings
  nova_ideal_reader.py --measure 10          # run over 10 recent articles, report
  nova_ideal_reader.py --file PATH           # show the diff for one article
"""
from __future__ import annotations

import argparse
import collections
import difflib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import nova_dsn as _nova_dsn  # noqa: E402
OPS_DSN = _nova_dsn.pg_dsn("nova_ops")
CONTENT = Path(os.environ.get("NOVA_JOURNAL_CONTENT", str(Path.home() / "nova-journal" / "content")))
MAX_CUT = 0.15
CORPUS_N = 200
DARLING_MIN_DOCS = 0.05      # an n-gram in >= 5% of her last 200 pieces is a tic candidate
DARLING_TOP = 60

# Built-in stock lead-ins — used alongside the PG darlings (and alone when PG is down).
BUILTIN_LEADS = [
    "here's the thing", "here is the thing", "the thing is", "let me be direct",
    "let me be clear", "let me be honest", "let's be honest", "let's be clear",
    "let's be real", "make no mistake", "at the end of the day", "in other words",
    "to be clear", "honestly", "look", "and look", "real talk", "spoiler",
    "full disclosure", "for what it's worth", "needless to say", "it's worth noting that",
    "it goes without saying that", "and honestly", "and here's the thing",
]
# Throwaway sentences: dropped whole when they stand alone.
STOCK_SENTENCES = re.compile(
    r"^(and\s+)?(that'?s|this is|here'?s)\s+(the\s+)?(thing|point|part|kicker|catch|problem|rub)"
    r"(\s+that\s+matters)?[.:]?$|"
    r"^(and\s+)?(that|this|it)\s+matters[.]?$|^think about that[.]?$|^let that sink in[.]?$|"
    r"^(full\s+stop|period|end\s+of\s+story)[.]?$|^here'?s\s+why[.:]?$|^read that again[.]?$",
    re.I)
CLUTTER_SUBS = [  # (pattern, replacement) — deletions or strict shortenings only
    (re.compile(r"\bin order to\b", re.I), "to"),
    (re.compile(r"\bthe fact that\b", re.I), "that"),
    (re.compile(r"\bat the end of the day,\s*", re.I), ""),
    (re.compile(r"\b(it'?s|it is) worth noting that\s+", re.I), ""),
    (re.compile(r"\bneedless to say,\s*", re.I), ""),
    (re.compile(r"\bit goes without saying that\s+", re.I), ""),
    (re.compile(r"\bfor what it'?s worth,\s*", re.I), ""),
]
ADVERBS = ("really very truly genuinely honestly actually simply literally absolutely "
           "incredibly deeply fundamentally basically essentially totally completely utterly "
           "extremely definitely certainly obviously undeniably remarkably profoundly "
           "entirely incredibly seriously frankly").split()
_ADV_RE = re.compile(r"(?<![\w'])(" + "|".join(sorted(set(ADVERBS))) + r")\s+(?=[A-Za-z])", re.I)
_NEGATION_BEFORE = re.compile(r"(\bnot|n't|\bnever|\bno|\bhow|\bso|\bwhat)\s*$", re.I)
GRANDIOSE = ("groundbreaking revolutionary unprecedented world-class game-changing masterful "
             "flawless genius brilliant monumental legendary unparalleled unmatched "
             "extraordinary visionary").split()
_GRAND_RE = re.compile(r"\b(" + "|".join(GRANDIOSE) + r")\s+(?=[A-Za-z])", re.I)
_FIRST_PERSON = re.compile(r"\b(I|I'm|I've|I'd|my|me|myself|Nova)\b", re.I)
_GRANDIOSITY_SENT = re.compile(
    r"\b(only I (could|can|would)|I alone|no one else (could|can|would)|nobody else (could|can|would)|"
    r"I'?m (a genius|the best|brilliant|unmatched|irreplaceable)|my (genius|brilliance))\b", re.I)
# Honesty/limits sentences are never cut: removing "I didn't verify X" makes the piece
# claim more certainty than she has (a humility regression, found in the first measure).
HEDGE_RE = re.compile(
    r"\b(didn'?t|did not|haven'?t|have not|never|not)\s+(yet\s+)?(verify|verified|check|checked|read|test|"
    r"tested|confirm|confirmed|install|installed|clone|cloned|run|ran|flash|flashed|measure|measured)\b|"
    r"\bI can'?t tell\b|\bI don'?t know\b|\bnot sure\b|\bunverified\b|\bdesk review\b|"
    r"\bnot a finding\b|\bnot a dig\b|\bto be fair\b|\bI might be wrong\b|\bI could be wrong\b|"
    r"\bI was wrong\b|\bcorrection\b", re.I)
PROTECT_RE = re.compile(r"damien|little mister|ferengi|rule of acquisition|[!?]|\*|\"|“|”|\(source:|`", re.I)
_SOURCES_HEAD = re.compile(r"^#{1,6}\s*\**\s*(sources|references|attribution|sources\s*&\s*attribution)\b", re.I | re.M)
_NUM_RE = re.compile(r"\d[\d,.]*")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'’]+")
_STOP = frozenset("the and that this with from was were for are but not you your its it's into "
                  "has have had they them their there what when which who will would could should "
                  "been being than then just also about over only more most such some any all one".split())
_GLUE = frozenset("a an the of in on at to and or but is it its it's that this which was were be "
                  "for with as by from not no so if then there than one rest part same lot out up "
                  "what who when how i you we they he she me my your our their i'm you're".split())
_DATEWORDS = re.compile(r"\b(january|february|march|april|may|june|july|august|september|october|"
                        r"november|december|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                        r"am|pm|pt|mph|inhg|uv|humidity|published|burbank)\b")
_PROTECT_NGRAM = re.compile(r"little mister|damien|ferengi|acquisition|nova|jordan")


def log(m):
    print(f"[ideal-reader {datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def _content_words(t: str) -> set:
    return {w.replace("’", "'") for w in _WORD_RE.findall((t or "").lower())
            if len(w) >= 3 and w not in _STOP}


def _wc(t: str) -> int:
    return len((t or "").split())


def target_cut() -> float:
    """10% at the default verbosity dial; terse -> 15%, expansive -> 5%."""
    try:
        from nova_voice import dial_scale
        return max(0.0, min(MAX_CUT, float(dial_scale("verbosity", 0.15, 0.10, 0.05))))
    except Exception:
        return 0.10


# ═══════════════════════════════════════════════════════════════════════════════
# Darlings — her real tics, from her own last 200 pieces
# ═══════════════════════════════════════════════════════════════════════════════
def _article_body(raw: str) -> str:
    t = re.sub(r"\A---.*?\n---\s*\n", "", raw, flags=re.S)
    m = _SOURCES_HEAD.search(t)
    return t[:m.start()] if m else t


def _prose_lines(body: str):
    fence = False
    for ln in body.splitlines():
        s = ln.strip()
        if s.startswith("```"):
            fence = not fence
            continue
        if fence or not s or s[0] in "#>|!-*+" or s[:2].isdigit() or "](" in s or s.startswith("<"):
            continue
        yield s


def compute_darlings(paths: list, top: int = DARLING_TOP) -> list:
    """[(phrase, docs, lead_docs)] — 3-5 word n-grams in >= DARLING_MIN_DOCS of the
    pieces, minus templated lines, dates/weather, pure glue and protected bits."""
    docs_lines = []
    line_df = collections.Counter()
    for p in paths:
        try:
            lines = list(_prose_lines(_article_body(Path(p).read_text(errors="ignore"))))
        except Exception:
            continue
        docs_lines.append(lines)
        line_df.update(set(lines))
    template = {ln for ln, c in line_df.items() if c >= 3}
    df, lead = collections.Counter(), collections.Counter()
    for lines in docs_lines:
        seen, seen_lead = set(), set()
        for ln in lines:
            if ln in template:
                continue
            for sent in re.split(r"(?<=[.!?])\s+", ln):
                low = sent.lower().replace("’", "'")
                toks = re.findall(r"[a-z']+", low)
                for n in (3, 4, 5):
                    for i in range(len(toks) - n + 1):
                        g = toks[i:i + n]
                        if all(x in _GLUE for x in g):
                            continue
                        ph = " ".join(g)
                        if _DATEWORDS.search(ph) or _PROTECT_NGRAM.search(ph):
                            continue
                        seen.add(ph)
                        if i == 0:
                            seen_lead.add(ph)
        df.update(seen)
        lead.update(seen_lead)
    n_docs = max(1, len(docs_lines))
    cands = [(ph, c) for ph, c in df.items() if c / n_docs >= DARLING_MIN_DOCS]
    cands.sort(key=lambda x: (-x[1], -len(x[0])))
    keep = []
    for ph, c in cands:
        # a shorter n-gram almost always inside a kept longer one is the same tic
        if any(ph in k and c <= kc * 1.15 for k, kc, _ in keep):
            continue
        keep = [(k, kc, kl) for k, kc, kl in keep if not (k in ph and kc <= c * 1.15)]
        keep.append((ph, c, lead.get(ph, 0)))
        if len(keep) >= top:
            break
    return keep


def recent_articles(n: int = CORPUS_N) -> list:
    fs = [p for p in CONTENT.glob("*/*.md") if not p.name.startswith("_")]
    fs.sort(key=lambda p: p.stat().st_mtime)
    return fs[-n:]


def ensure_schema(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS ideal_reader_darlings (
                     phrase text PRIMARY KEY, docs int, lead_docs int, corpus int,
                     computed_at timestamptz DEFAULT now())""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ideal_reader_log (
                     id bigserial PRIMARY KEY, ts timestamptz DEFAULT now(), kind text,
                     title text, words_before int, words_after int, cut_pct real,
                     applied boolean, cuts jsonb)""")


def refresh_darlings(cur) -> list:
    paths = recent_articles()
    ds = compute_darlings(paths)
    ensure_schema(cur)
    cur.execute("DELETE FROM ideal_reader_darlings")
    for ph, c, ld in ds:
        cur.execute("INSERT INTO ideal_reader_darlings (phrase, docs, lead_docs, corpus) VALUES (%s,%s,%s,%s)",
                    (ph, c, ld, len(paths)))
    _DARLING_CACHE.clear()
    return ds


_DARLING_CACHE: list = []


def _pg_connect(attempts: int = 3, backoff: float = 0.5):
    """psycopg2.connect to nova_ops with retry + linear backoff (house retry rule)."""
    import time
    import psycopg2
    for i in range(attempts):
        try:
            return psycopg2.connect(OPS_DSN, connect_timeout=3)
        except psycopg2.OperationalError as e:
            if i == attempts - 1:
                raise
            log(f"pg connect failed ({e}) — retry {i + 1}/{attempts - 1}")
            time.sleep(backoff * (i + 1))


def load_darlings() -> list:
    """Lead-capable darling phrases (PG, cached) + the built-in stock leads."""
    if _DARLING_CACHE:
        return list(_DARLING_CACHE)
    out = list(BUILTIN_LEADS)
    try:
        with _pg_connect() as c, c.cursor() as cur:
            cur.execute("SELECT phrase FROM ideal_reader_darlings WHERE lead_docs >= 3 ORDER BY lead_docs DESC")
            out += [r[0] for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        log(f"darlings unavailable ({e}) — built-in leads only")
    _DARLING_CACHE[:] = sorted(set(out), key=len, reverse=True)
    return list(_DARLING_CACHE)


# ═══════════════════════════════════════════════════════════════════════════════
# The edit
# ═══════════════════════════════════════════════════════════════════════════════
def _protected(s: str) -> bool:
    """Never edited or cut: the Damien bit, Little Mister, the Ferengi rule, questions,
    exclamations, emphasis, quotes, citations — and honesty/limits sentences."""
    return bool(PROTECT_RE.search(s) or HEDGE_RE.search(s))


def _split_tail(text: str):
    m = _SOURCES_HEAD.search(text)
    return (text[:m.start()], text[m.start():]) if m else (text, "")


def _sentences(line: str) -> list:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"“*_(])", line) if s]


def _cap(s: str) -> str:
    for i, ch in enumerate(s):
        if ch.isalpha():
            return s[:i] + ch.upper() + s[i + 1:]
    return s


def _fix_articles(s: str) -> str:
    s = re.sub(r"\b([Aa]) (?=[aeiouAEIOU][a-z])", lambda m: m.group(1) + "n ", s)
    return re.sub(r"\b([Aa])n (?=[b-df-gj-np-tv-zB-DF-GJ-NP-TV-Z])", lambda m: m.group(1) + " ", s)


def _strip_lead(s: str, darlings: list):
    low = s.lower().replace("’", "'")
    for d in darlings:
        if low.startswith(d):
            rest = s[len(d):]
            m = re.match(r"\s*[,:—–-]+\s*", rest)
            if m and len(rest[m.end():].split()) >= 3:
                return _cap(rest[m.end():]), d
    return s, None


def _edit_sentence(s: str, darlings: list, cuts: list) -> str:
    """Phrase-level, deletion-only edits on one sentence. Quoted text is left alone."""
    if '"' in s or "“" in s or "(source:" in s.lower():
        return s
    orig = s
    s, d = _strip_lead(s, darlings)
    if d:
        cuts.append(("lead", d))
    for pat, rep in CLUTTER_SUBS:
        s2 = pat.sub(rep, s)
        if s2 != s:
            cuts.append(("clutter", pat.pattern[:40]))
            s = _cap(s2) if not s2[:1].isupper() and orig[:1].isupper() else s2

    def _adv(m):
        if _NEGATION_BEFORE.search(s[:m.start()]):
            return m.group(0)
        cuts.append(("adverb", m.group(1).lower()))
        return ""
    s = _ADV_RE.sub(_adv, s)
    if _FIRST_PERSON.search(s):
        def _grand(m):
            cuts.append(("humility", m.group(1).lower()))
            return ""
        s = _GRAND_RE.sub(_grand, s)
    if s != orig:
        s = _fix_articles(re.sub(r"\s{2,}", " ", s)).strip()
        if orig[:1].isupper():
            s = _cap(s)
    return s


def edit(text: str, kind: str = "article", darlings: list | None = None,
         target: float | None = None, floor_words: int | None = None, chooser=None) -> dict:
    """Second pass. Returns {"text", "before", "after", "cut_pct", "cuts", "applied", "why"}.
    kind='article' may drop whole sentences up to the target; kind='reach' is phrase-level
    only (plus stock throwaway sentences). Always deletion-only; on any failed
    post-condition the ORIGINAL text is returned with applied=False.
    chooser(system, user) -> str: optional model call that picks sentence ids to cut."""
    darlings = load_darlings() if darlings is None else darlings
    target = target_cut() if target is None else target
    head, tail = _split_tail(text or "")
    before = _wc(head)
    cuts: list = []
    out_lines = []
    cand = []          # (redundancy, line_idx, sent_idx) for the restatement pass
    fence = False
    lines = head.split("\n")
    edited = []        # per line: list of sentences or None (untouched line)
    for li, ln in enumerate(lines):
        st = ln.strip()
        if st.startswith("```"):
            fence = not fence
        if (fence or not st or st[0] in "#>|!-+<" or st[:2].isdigit() or st.startswith("*Published")
                or st.startswith("*Burbank") or "](" in st or _wc(st) < 6):
            edited.append(None)
            continue
        sents = _sentences(ln)
        new = []
        for si, s in enumerate(sents):
            core = s.strip()
            if not _protected(core) and STOCK_SENTENCES.match(core):
                cuts.append(("stock", core[:60]))
                continue
            if not _protected(core) and _GRANDIOSITY_SENT.search(core):
                cuts.append(("humility-sentence", core[:80]))
                continue
            new.append(core if _protected(core) else _edit_sentence(core, darlings, cuts))
        edited.append(new)

    # Sentence cuts (articles only). Preferred: a model CHOOSES which numbered
    # sentences to cut (it never writes text — the code deletes them, so nothing new
    # can enter). Fallback: the deterministic restatement rule — a sentence whose
    # content words are >= 80% covered by the earlier sentences of its paragraph.
    picked = None
    if kind == "article" and chooser is not None:
        try:
            picked = _choose(edited, before, target, chooser)
        except Exception as e:  # noqa: BLE001
            log(f"chooser failed ({e}) — deterministic cuts only")
            picked = None
    if picked:
        cur_words = sum(_wc(" ".join(s)) for s in edited if s) + sum(
            _wc(lines[i]) for i, s in enumerate(edited) if s is None)
        drop = set()
        for li, si in picked:
            if before and (before - cur_words) / before >= target:
                break
            if (before - cur_words + _wc(edited[li][si])) / max(1, before) > MAX_CUT:
                continue
            drop.add((li, si))
            cur_words -= _wc(edited[li][si])
            cuts.append(("ideal-reader", edited[li][si][:80]))
        for li, sents in enumerate(edited):
            if sents:
                edited[li] = [s for si, s in enumerate(sents) if (li, si) not in drop]
    elif kind == "article":
        for li, sents in enumerate(edited):
            if not sents:
                continue
            seen: set = set()
            for si, s in enumerate(sents):
                cw = _content_words(s)
                if si > 0 and len(cw) >= 4 and not _protected(s) and si != len(sents) - 1:
                    cov = len(cw & seen) / len(cw)
                    if cov >= 0.8:
                        cand.append((cov, li, si))
                seen |= cw
        cand.sort(reverse=True)
        cur_words = sum(_wc(" ".join(s)) for s in edited if s) + sum(
            _wc(lines[i]) for i, s in enumerate(edited) if s is None)
        drop = set()
        for cov, li, si in cand:
            if before and (before - cur_words) / before >= target:
                break
            drop.add((li, si))
            cur_words -= _wc(edited[li][si])
            cuts.append(("restates", edited[li][si][:80]))
        for li, sents in enumerate(edited):
            if sents:
                edited[li] = [s for si, s in enumerate(sents) if (li, si) not in drop]

    for li, ln in enumerate(lines):
        if edited[li] is None:
            out_lines.append(ln)
        elif edited[li]:
            indent = ln[:len(ln) - len(ln.lstrip())]
            out_lines.append(indent + " ".join(edited[li]))
        else:
            out_lines.append("")
    new_head = re.sub(r"\n{3,}", "\n\n", "\n".join(out_lines))
    if head.endswith("\n") and not new_head.endswith("\n"):
        new_head += "\n"
    out = new_head + tail
    after = _wc(new_head)
    res = {"text": out, "before": before, "after": after,
           "cut_pct": round(100.0 * (before - after) / before, 1) if before else 0.0,
           "cuts": cuts, "applied": True, "why": "ok"}
    why = verify(text or "", out)
    if not why and before and (before - after) / before > MAX_CUT + 0.03:
        why = f"cut {res['cut_pct']}% exceeds the {int(MAX_CUT * 100)}% cap"
    if not why and floor_words and after < floor_words <= before:
        why = f"would drop below the {floor_words}-word floor"
    if why:
        res.update(text=text, after=before, cut_pct=0.0, applied=False, why=why)
    return res


_CHOOSE_SYS = (
    "You are the Ideal Reader editing Nova's article (Stephen King, On Writing: the second "
    "draft is the first minus 10%). Sentences you may cut are tagged [S<n>]. Pick the weakest "
    "ones to delete, totalling about {words} words: sentences that restate what was already "
    "said, throat-clearing, filler, generic wind-ups and wind-downs, stock phrasing, and "
    "sentences that only announce what the next sentence says. KEEP every fact, number, name "
    "and claim that appears nowhere else, every caveat or qualification ('that's not a dig at', "
    "'I didn't check'), the piece's one real joke, and anything the next sentence depends on. You cannot rewrite anything — only choose ids. Return ONLY JSON: "
    '{{"cut": [<n>, ...]}} — weakest first.')


def _choose(edited: list, before: int, target: float, chooser) -> list:
    """Ask the chooser which tagged sentences to cut. Returns [(line_idx, sent_idx)] in the
    model's order, only ids it was offered."""
    ids, parts, n = {}, [], 0
    for li, sents in enumerate(edited):
        if not sents:
            continue
        row = []
        for si, s in enumerate(sents):
            if _protected(s) or len(sents) == 1 and _wc(s) > 40:
                row.append(s)
                continue
            n += 1
            ids[n] = (li, si)
            row.append(f"[S{n}] {s}")
        parts.append(" ".join(row))
    if not ids:
        return []
    out = chooser(_CHOOSE_SYS.format(words=int(before * target)), "\n\n".join(parts)) or ""
    a, b = out.find("{"), out.rfind("}")
    got = json.loads(out[a:b + 1]).get("cut", []) if a >= 0 and b > a else []
    res = []
    for x in got:
        try:
            k = int(str(x).lstrip("Ss"))
        except ValueError:
            continue
        if k in ids and ids[k] not in res:
            res.append(ids[k])
    return res


def claude_chooser(system: str, user: str) -> str:
    """Default chooser: haiku through the journal's Claude Code CLI wrapper."""
    from nova_journal import call_openrouter
    return call_openrouter(system, user, model="anthropic/claude-haiku-4.5", timeout=180) or ""


def verify(original: str, edited: str) -> str | None:
    """None when the edit is safe: deletion-only content, no new numbers, Sources intact."""
    _, t0 = _split_tail(original)
    _, t1 = _split_tail(edited)
    if t0 != t1:
        return "Sources section changed"
    new = _content_words(edited) - _content_words(original)
    if new:
        return f"new words introduced: {sorted(new)[:5]}"
    if set(_NUM_RE.findall(edited)) - set(_NUM_RE.findall(original)):
        return "new number introduced"
    if "damien" in original.lower() and original.lower().count("damien") != edited.lower().count("damien"):
        return "the Damien bit was touched"
    return None


def log_edit(kind: str, title: str, res: dict):
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        with _pg_connect() as c, c.cursor() as cur:
            ensure_schema(cur)
            cur.execute("INSERT INTO ideal_reader_log (kind, title, words_before, words_after, cut_pct, applied, cuts) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (kind, (title or "")[:300], res["before"], res["after"], res["cut_pct"],
                         res["applied"], json.dumps({"why": res["why"], "cuts": res["cuts"][:80]})))
    except Exception as e:  # noqa: BLE001
        log(f"log skipped: {e}")


def edit_article(title: str, body: str, floor_words: int | None = None, chooser=None) -> str:
    """The journal hook: second pass, quality guard, log. Any failure -> body unchanged."""
    try:
        res = edit(body, "article", floor_words=floor_words, chooser=chooser)
        if res["applied"] and res["text"] != body:
            from nova_journal_guard import is_publishable
            ok, why = is_publishable(title, res["text"])
            if not ok:
                res.update(text=body, after=res["before"], cut_pct=0.0, applied=False, why=f"guard: {why}")
        log(f"'{(title or '')[:50]}' {res['before']} -> {res['after']}w ({res['cut_pct']}%) "
            f"{'applied' if res['applied'] else 'NOT applied: ' + res['why']}")
        log_edit("article", title, res)
        return res["text"]
    except Exception as e:  # noqa: BLE001
        log(f"editor error ({e}) — publishing the draft as written")
        return body


def edit_reach(message: str) -> str:
    try:
        res = edit(message, "reach")
        if res["applied"] and res["text"] != message:
            log_edit("reach", message[:80], res)
        return res["text"]
    except Exception:  # noqa: BLE001
        return message


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════
def _measure(n: int, show: int = 1, use_model: bool = True):
    darlings = load_darlings()
    arts = [p for p in reversed(recent_articles(400)) if p.parent.name not in ("watches",)][:n * 3]
    rows, done = [], 0
    for p in arts:
        raw = p.read_text(errors="ignore")
        body = re.sub(r"\A---.*?\n---\s*\n", "", raw, flags=re.S)
        if _wc(body) < 300:
            continue
        res = edit(body, "article", darlings=darlings, chooser=claude_chooser if use_model else None)
        rows.append((p, res))
        done += 1
        if done >= n:
            break
    tb = sum(r["before"] for _, r in rows)
    ta = sum(r["after"] for _, r in rows)
    for p, r in rows:
        kinds = collections.Counter(k for k, _ in r["cuts"])
        print(f"{r['before']:6d} -> {r['after']:6d}  {r['cut_pct']:5.1f}%  {'ok ' if r['applied'] else 'NO '} "
              f"{dict(kinds)}  {p.parent.name}/{p.name[:60]}")
    print(f"TOTAL {tb} -> {ta} words ({100.0 * (tb - ta) / max(1, tb):.1f}% cut) over {len(rows)} articles")
    for p, r in rows[:show]:
        body = re.sub(r"\A---.*?\n---\s*\n", "", p.read_text(errors="ignore"), flags=re.S)
        d = difflib.unified_diff(body.splitlines(), r["text"].splitlines(), lineterm="", n=0)
        print(f"\n--- sample diff: {p.name}")
        print("\n".join(list(d)[:40]))


def main():
    ap = argparse.ArgumentParser(description="Nova's second-pass editor (Ideal Reader / Ozzie's Rule)")
    ap.add_argument("--refresh-darlings", action="store_true")
    ap.add_argument("--measure", type=int, metavar="N")
    ap.add_argument("--file")
    ap.add_argument("--show", type=int, default=1)
    ap.add_argument("--no-model", action="store_true", help="deterministic cuts only")
    a = ap.parse_args()
    if a.refresh_darlings:
        import psycopg2
        with psycopg2.connect(OPS_DSN, connect_timeout=5) as c, c.cursor() as cur:
            ds = refresh_darlings(cur)
        log(f"darlings refreshed from {CORPUS_N} pieces: {len(ds)} phrases; top: "
            + "; ".join(f"{p} ({c})" for p, c, _ in ds[:12]))
        return 0
    if a.measure:
        _measure(a.measure, a.show, not a.no_model)
        return 0
    if a.file:
        body = re.sub(r"\A---.*?\n---\s*\n", "", Path(a.file).read_text(), flags=re.S)
        r = edit(body, "article", chooser=None if a.no_model else claude_chooser)
        print("\n".join(difflib.unified_diff(body.splitlines(), r["text"].splitlines(), lineterm="", n=0)))
        print(f"\n{r['before']} -> {r['after']} ({r['cut_pct']}%) applied={r['applied']} {r['why']}")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
