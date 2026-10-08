#!/usr/bin/env python3
"""nova_ivory_head.py — The Ivory Head: how much of what Nova believes came from one shelf.

Lovecraft, "The Temple" (1925). The U-29 sinks the British freighter Victory; a dead sailor
from it is found on the submarine's deck, and in his pocket is a carved ivory head. Lieutenant
Klenze takes it from the men. Then the boat comes apart one man at a time: bad dreams, Müller
confined and whipped, Bohm and Schmidt go mad, Müller and Zimmer vanish, Traube is shot, six
mutineers are shot, Klenze walks into the sea with the head. The commander, Karl Heinrich,
Graf von Altberg-Ehrenstein, writes it all down as a chronicle sealed in a bottle. The crew
blames the head; he refuses to, and keeps writing. Nothing in it ever attacks anyone. Having
it aboard is enough.

Nova's version: for every active belief, count where its cited support came from, by source
family (gutenberg, youtube, tv, news, scanner, herd_mail, fishbowl...). A belief whose support
is >= 80% one ingested family, with no conversation or sensor grounding, is flagged. Each week
gets one row per family. Citation coverage is reported next to the findings, because uncited
beliefs are invisible to this organ. Findings are questions, never deletions. Nothing is posted.

Data path (read-only except its own two tables):
  nova_ops.beliefs (active; article_slug holds the article TITLE)
    -> article slug: nova_journal's slug rule on the title, plus the nova_articles memory's
       metadata slug with its date prefix stripped (stable-slug posts like the-fishbowl)
    -> nova_ops.article_citations (article_slug, memory_id), written at publish time
    -> nova_memories.memories (source + ingest tag) by id. The memory server has no
       lookup-by-id endpoint, so this is a batched read-only SELECT over W.MEM_DSN, as
       nova_cardinal / nova_articles_to_memory do. W.connect retries; a failed query fails open.

CLI:    --run [--dry-run]   --show   --selftest
Tables: ivory_head_weekly (week, family, ...), ivory_head_flags (week, belief_id, ...)
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

THRESHOLD = 0.8
BATCH = 1000
GROUNDING = {"conversation", "sensor"}
NOT_INGESTED = GROUNDING | {"self"}          # her own writing is neither a corpus nor grounding
# memory source, or ingest tag (metadata platform / type) -> family. Checked platform, type, source.
FAMILY = {
    "conversation": "conversation", "chat_turn": "conversation", "imessage": "conversation",
    "nova_imessage": "conversation", "jordan_conversation": "conversation", "chatroom": "conversation",
    "homekit": "sensor", "homekit_homepod": "sensor", "weather_station": "sensor", "weather_homekit": "sensor",
    "apple_health": "sensor", "health_correlation": "sensor", "face_recognition": "sensor",
    "frame_vision": "sensor", "quiet_sensor": "sensor",
    "scanner": "scanner", "scanner_digest": "scanner",
    "herd": "herd_mail", "herd_correspondence": "herd_mail", "herd_relationships": "herd_mail", "herd_blog": "herd_mail",
    "lexicon": "lexicon",
    "fishbowl_stream": "fishbowl", "fishbowl": "fishbowl",
    "youtube": "youtube", "tv_transcript": "tv", "television": "tv",
    "gov_rss": "news", "news": "news", "local_news": "news",
    "email_archive": "email_archive", "private_document": "email_archive",
    "local_file": "local_files",
    "nova_articles": "self", "research": "self", "episodic": "self", "episode": "self",
    "association": "self", "spark": "self", "self_answer": "self",
}
_GUTENBERG = re.compile(r"(^|/)pg\d+\.txt$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ivory_head_weekly (
  week date NOT NULL,
  family text NOT NULL,
  beliefs_supported int NOT NULL,
  single_corpus_beliefs int NOT NULL,
  share real NOT NULL,
  beliefs_total int NOT NULL,
  beliefs_cited int NOT NULL,
  computed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (week, family));
CREATE TABLE IF NOT EXISTS ivory_head_flags (
  week date NOT NULL,
  belief_id int NOT NULL,
  family text NOT NULL,
  share real NOT NULL,
  n_citations int NOT NULL,
  computed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (week, belief_id));
"""


def log(m: str) -> None:
    print(f"[ivory-head {datetime.now():%H:%M:%S}] {m}", flush=True)


# ── pure ────────────────────────────────────────────────────────────────────

def family_of(source: str | None, platform: str | None = None, mtype: str | None = None,
              path: str | None = None) -> str:
    """Source family of one memory. Pure."""
    if path and _GUTENBERG.search(path):
        return "gutenberg"
    for k in (platform, mtype, source):
        if k in FAMILY:
            return FAMILY[k]
    return "other"   # ponytail: topical shelves with no ingest tag pool here; extend FAMILY when one matters


def slug_of(title: str) -> str:
    """nova_journal's publish slug rule (inline there, so mirrored here)."""
    return re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:60]


def strip_date(slug: str) -> str:
    return re.sub(r"^\d{4}-\d{2}-\d{2}-", "", slug or "")


def analyse(cites: dict, fam: dict, total: int, threshold: float = THRESHOLD) -> tuple[list, list, dict]:
    """cites = {belief_id: [memory_id]}, fam = {memory_id: family}, total = active beliefs.
    -> (weekly rows, flags, coverage). Pure."""
    shares: dict = {}
    flags = []
    for bid, ids in cites.items():
        fs = [fam[i] for i in ids if i in fam]
        if not fs:
            continue
        sh = {f: fs.count(f) / len(fs) for f in set(fs)}
        shares[bid] = sh
        top = max(sh, key=lambda f: (sh[f], f))
        if sh[top] >= threshold and top not in NOT_INGESTED and not GROUNDING & set(sh):
            flags.append({"belief_id": bid, "family": top, "share": round(sh[top], 3), "n_citations": len(fs)})
    flags.sort(key=lambda r: (-r["share"], -r["n_citations"], r["belief_id"]))
    cited = sum(1 for ids in cites.values() if ids)
    weekly = []
    for f in sorted({f for sh in shares.values() for f in sh}):
        weekly.append({"family": f, "beliefs_supported": sum(1 for sh in shares.values() if f in sh),
                       "single_corpus_beliefs": sum(1 for r in flags if r["family"] == f),
                       "share": round(sum(sh.get(f, 0) for sh in shares.values()) / len(shares), 4),
                       "beliefs_total": total, "beliefs_cited": cited})
    unresolved = sum(1 for ids in cites.values() for i in ids if i not in fam)
    cov = {"beliefs_total": total, "beliefs_cited": cited, "beliefs_uncited": total - cited,
           "beliefs_resolved": len(shares), "citations_unresolved": unresolved,
           "coverage": round(cited / total, 4) if total else 0.0}
    return weekly, flags, cov


def week_of(d: date | None = None) -> date:
    d = d or date.today()
    return d - timedelta(days=d.weekday())


# ── I/O ─────────────────────────────────────────────────────────────────────

def _q(cur, sql, args=()):
    try:
        cur.execute(sql, args)
        return cur.fetchall()
    except Exception as e:  # noqa: BLE001 — fail open, never sink the run
        log(f"query failed: {e}")
        try:
            cur.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def ensure_schema(cur) -> None:
    cur.execute(SCHEMA)


def load_beliefs(oc) -> list:
    return _q(oc, "SELECT id, article_slug FROM beliefs WHERE active") or []


def load_citations(oc) -> dict:
    out: dict = {}
    for slug, mid in _q(oc, "SELECT article_slug, memory_id FROM article_citations") or []:
        out.setdefault(slug, []).append(str(mid))
    return out


def article_slugs(mc, titles: list) -> dict:
    """{title: {slug without date}} from the re-ingested nova_articles memories."""
    out: dict = {}
    rows = _q(mc, "SELECT DISTINCT metadata->>'title', metadata->>'slug' FROM memories "
                  "WHERE source='nova_articles' AND metadata->>'title' = ANY(%s)", (titles,)) or []
    for t, s in rows:
        if s:
            out.setdefault(t, set()).add(strip_date(s))
    return out


def memory_families(mc, ids: list) -> dict:
    """{memory_id: family}, batched. Missing ids (or a failed batch) are simply absent."""
    out = {}
    for i in range(0, len(ids), BATCH):
        rows = _q(mc, "SELECT id, source, metadata->>'platform', metadata->>'type', metadata->>'path' "
                      "FROM memories WHERE id = ANY(%s)", (ids[i:i + BATCH],)) or []
        for mid, src, plat, mtype, path in rows:
            out[mid] = family_of(src, plat, mtype, path)
    return out


def belief_citations(beliefs: list, citations: dict, art: dict) -> dict:
    """{belief_id: [memory_id]} via both slug routes. Pure.
    ponytail: a stable-slug post (the-fishbowl) accumulates citations across editions, so its
    beliefs share every edition's citations; fine until per-edition citations exist."""
    out = {}
    for bid, title in beliefs:
        slugs = {slug_of(title)} | art.get(title, set())
        out[bid] = sorted({m for s in slugs for m in citations.get(s, [])})
    return out


def write(oc, week: date, weekly: list, flags: list) -> None:
    ensure_schema(oc)
    for r in weekly:
        oc.execute("INSERT INTO ivory_head_weekly (week, family, beliefs_supported, single_corpus_beliefs, share, "
                   "beliefs_total, beliefs_cited) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (week, family) DO UPDATE "
                   "SET beliefs_supported=EXCLUDED.beliefs_supported, single_corpus_beliefs=EXCLUDED.single_corpus_beliefs, "
                   "share=EXCLUDED.share, beliefs_total=EXCLUDED.beliefs_total, beliefs_cited=EXCLUDED.beliefs_cited, "
                   "computed_at=now()",
                   (week, r["family"], r["beliefs_supported"], r["single_corpus_beliefs"], r["share"],
                    r["beliefs_total"], r["beliefs_cited"]))
    oc.execute("DELETE FROM ivory_head_flags WHERE week=%s", (week,))
    for r in flags:
        oc.execute("INSERT INTO ivory_head_flags (week, belief_id, family, share, n_citations) VALUES (%s,%s,%s,%s,%s)",
                   (week, r["belief_id"], r["family"], r["share"], r["n_citations"]))


def run(dry: bool = False) -> dict:
    import nova_watch_common as W
    conn, mconn = W.connect(), W.connect(W.MEM_DSN)
    try:
        oc, mc = conn.cursor(), mconn.cursor()
        beliefs = load_beliefs(oc)
        cites = belief_citations(beliefs, load_citations(oc), article_slugs(mc, sorted({t for _b, t in beliefs if t})))
        fam = memory_families(mc, sorted({m for ids in cites.values() for m in ids}))
        weekly, flags, cov = analyse(cites, fam, len(beliefs))
        week = week_of()
        log(f"week {week}: {cov['beliefs_cited']}/{cov['beliefs_total']} active beliefs cited "
            f"({cov['coverage']:.1%}), {cov['beliefs_uncited']} uncited and invisible to this organ; "
            f"{cov['citations_unresolved']} citation(s) unresolved; {len(flags)} single-corpus belief(s)")
        if not dry and beliefs:
            write(oc, week, weekly, flags)
        return {"week": str(week), "coverage": cov, "weekly": weekly, "flags": flags}
    finally:
        conn.close()
        mconn.close()


def show() -> int:
    import nova_watch_common as W
    conn = W.connect()
    try:
        oc = conn.cursor()
        rows = _q(oc, "SELECT week, family, beliefs_supported, single_corpus_beliefs, share, beliefs_total, beliefs_cited "
                      "FROM ivory_head_weekly WHERE week = (SELECT max(week) FROM ivory_head_weekly) ORDER BY share DESC")
        if not rows:
            print("no Ivory Head runs yet")
            return 0
        print(f"week {rows[0][0]}: {rows[0][6]}/{rows[0][5]} active beliefs cited")
        for _w, f, sup, single, share, _t, _c in rows:
            print(f"  {f:<16} share={share:.0%}  beliefs={sup:<4} single-corpus={single}")
        for bid, f, share, n, topic, stance in _q(
                oc, "SELECT f.belief_id, f.family, f.share, f.n_citations, b.topic, b.stance FROM ivory_head_flags f "
                    "JOIN beliefs b ON b.id=f.belief_id WHERE f.week=%s ORDER BY f.share DESC, f.n_citations DESC LIMIT 10",
                    (rows[0][0],)) or []:
            print(f"  ? #{bid} {f} {share:.0%} of {n}: [{topic}] {stance[:100]}")
        return 0
    finally:
        conn.close()


def selftest() -> int:
    assert family_of("literature", None, "local_file", "/x/pg1342.txt") == "gutenberg"
    assert family_of("automotive", "youtube", "video_transcript") == "youtube"
    assert family_of("new_deal", None, "tv_transcript") == "tv"
    assert family_of("conversation", None, "chat_turn") == "conversation"
    assert family_of("philosophy") == "other"
    assert slug_of("🐠 Archie's Colon Went Live!") == "archie-s-colon-went-live"
    assert strip_date("2026-10-06-the-fishbowl") == "the-fishbowl"
    fam = {"a": "youtube", "b": "youtube", "c": "conversation", "d": "self"}
    weekly, flags, cov = analyse({1: ["a", "b"], 2: ["a", "c"], 3: [], 4: ["d"], 5: ["zz"]}, fam, 6)
    assert [f["belief_id"] for f in flags] == [1], flags
    assert cov["beliefs_cited"] == 4 and cov["beliefs_uncited"] == 2 and cov["citations_unresolved"] == 1
    yt = next(r for r in weekly if r["family"] == "youtube")
    assert yt["beliefs_supported"] == 2 and yt["single_corpus_beliefs"] == 1 and yt["share"] == 0.5
    assert week_of(date(2026, 10, 8)) == date(2026, 10, 5)
    print("selftest ok")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", action="store_true", help="score every active belief and write this week's rows")
    ap.add_argument("--dry-run", action="store_true", help="with --run: print, write nothing (no CREATE TABLE)")
    ap.add_argument("--show", action="store_true", help="print the latest week")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.run:
        res = run(dry=a.dry_run)
        if a.dry_run:
            for r in sorted(res["weekly"], key=lambda r: -r["share"]):
                print(f"{r['family']:<16} share={r['share']:.0%} beliefs={r['beliefs_supported']} "
                      f"single-corpus={r['single_corpus_beliefs']}")
            for r in res["flags"][:10]:
                print(f"? belief #{r['belief_id']} {r['family']} {r['share']:.0%} of {r['n_citations']} citations")
        return 0
    if a.show:
        return show()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
