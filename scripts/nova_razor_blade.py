#!/usr/bin/env python3
"""nova_razor_blade.py — THE RAZOR BLADE: how much did the pipeline change what she first wrote?

Little Mister, 2026-10-15 (after Nova read Modulations, 1998): "Add all 9."

In Modulations, a pioneer of tape music holds up the razor blade he spliced with: "When you cut and you
edit, you can do it in such a way that no one will ever know." That is the point of a good edit, and
the danger of one. Between the model's first draft and the published post, Nova's journal pipeline
expands (longform_expand: grounded expansion, tighten, strip-and-recheck), appends (the Crystal Ball,
the dateline) and cuts (the Ideal Reader). Nobody reading the post can tell how much of it changed.
This organ keeps the count, so at least she knows.

How it measures:
  * publish_hugo (nova_journal) passes the first draft and the final body to record_publish() after
    the file is written: one razor_blade row with draft/published word counts, words added and
    removed (difflib opcodes over words), similarity (SequenceMatcher ratio) and pct_changed =
    100 x (1 - similarity); the Ideal Reader's cut is joined from ideal_reader_log, and the
    longform stage's delta is what remains; the most-edited ## headings go into headings.
  * Posts from before the hook: --backfill-log PATH reconstructs word counts from the journal log
    ([longform] draft/expanded/tightened/stripped lines + "Published:") plus ideal_reader_log and
    the Hugo file on disk. Those rows are origin='log', similarity NULL, and pct_changed is the
    insert/delete estimate (added + removed) / (draft + published), which equals 1 - ratio when
    the edit is pure insertion and deletion.
  * The dateline the publisher prepends is not counted as an edit.

Read-only on the journal: it never changes a post. The public "edited N%" footer is NOT enabled.
Measured for the monthly continuity review (--report): median % changed per month, posts changed
more than 30%, and the sections (Hugo section and ## heading) edited most.

CLI:   --scheduled            daily; on the 1st, notify last month's numbers once
       --dry-run [PATH]       parse the journal log (default ~/.openclaw/logs/nova_journal.log), print
                              the reconstruction and the monthly numbers, write nothing
       --backfill-log PATH    write the log reconstruction (origin='log'; idempotent)
       --report               monthly numbers
       --selftest             offline check of the pure functions
Table: razor_blade (nova_ops)
Schedule: scheduler-core, daily 07:20, timeout 120.
Written by Jordan Koch (via Claude).
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nova_dsn  # noqa: E402

SERVICE = "nova_razor_blade"
HEAVY_PCT = 30.0                 # "posts changed > 30%"
STATEMENT_TIMEOUT_MS = 15000
JOURNAL_LOG = Path.home() / ".openclaw/logs/nova_journal.log"
HUGO_CONTENT = Path.home() / "nova-journal/content"
READER_WINDOW_MIN = 30           # an ideal_reader_log row belongs to a post published within this

SCHEMA = """
CREATE TABLE IF NOT EXISTS razor_blade (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz NOT NULL,
  slug text NOT NULL,
  section text,
  title text,
  origin text NOT NULL DEFAULT 'publish',
  draft_words integer NOT NULL,
  expanded_words integer,
  reader_cut_words integer,
  published_words integer NOT NULL,
  words_added integer NOT NULL,
  words_removed integer NOT NULL,
  similarity real,
  pct_changed real NOT NULL,
  headings jsonb NOT NULL DEFAULT '[]',
  UNIQUE (slug, published_at));
CREATE INDEX IF NOT EXISTS razor_blade_published_at ON razor_blade (published_at);
"""

# the italic "*<place> · Weekday, Month D, YYYY · H:MM PM ...*" line the publisher prepends
DATELINE_RE = re.compile(r"\A\s*\*[^*\n]{1,60} · \w+day, \w+ \d{1,2}, \d{4}[^\n]*\*\s*\n+")
HEADING_RE = re.compile(r"^#{1,4}\s+(.+?)\s*#*\s*$", re.M)


def log(m: str) -> None:
    print(f"[razor-blade {datetime.now():%H:%M:%S}] {m}", flush=True)


# ---------------------------------------------------------------- pure functions

def strip_dateline(text: str) -> str:
    return DATELINE_RE.sub("", text or "", count=1)


def words(text: str) -> list[str]:
    return (text or "").split()


def diff_counts(a: list[str], b: list[str]) -> tuple[int, int, float]:
    """(words added, words removed, similarity 0..1) from a to b. Pure."""
    if not a and not b:
        return 0, 0, 1.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    added = removed = 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op in ("replace", "delete"):
            removed += i2 - i1
        if op in ("replace", "insert"):
            added += j2 - j1
    return added, removed, sm.ratio()


def sections(text: str) -> dict[str, str]:
    """{heading: body} by markdown heading; text before the first heading is '(lead)'. Pure."""
    out, last, pos = {}, "(lead)", 0
    for m in HEADING_RE.finditer(text or ""):
        out[last] = out.get(last, "") + text[pos:m.start()]
        last, pos = m.group(1).strip()[:80], m.end()
    out[last] = out.get(last, "") + (text or "")[pos:]
    return {k: v for k, v in out.items() if v.strip() or k != "(lead)"}


def heading_changes(draft: str, published: str, top: int = 8) -> list[dict]:
    """The most-edited sections: [{heading, pct, added, removed}] sorted by words touched. A heading only
    in the published text is all added; one only in the draft is all removed. Pure."""
    d, p = sections(draft), sections(published)
    out = []
    for h in list(dict.fromkeys(list(p) + list(d))):
        a, b = words(d.get(h, "")), words(p.get(h, ""))
        add, rem, sim = diff_counts(a, b)
        if add or rem:
            out.append({"heading": h, "pct": round(100 * (1 - sim), 1), "added": add, "removed": rem})
    out.sort(key=lambda x: -(x["added"] + x["removed"]))
    return out[:top]


def measure(draft: str, published: str) -> dict:
    """The razor_blade numbers for one post. Pure."""
    published = strip_dateline(published)
    a, b = words(draft), words(published)
    added, removed, sim = diff_counts(a, b)
    return {"draft_words": len(a), "published_words": len(b), "words_added": added, "words_removed": removed,
            "similarity": round(sim, 4), "pct_changed": round(100 * (1 - sim), 1),
            "headings": heading_changes(draft, published)}


def estimate_pct(draft_w: int, published_w: int, added: int, removed: int) -> float:
    """Insert/delete estimate of 1 - ratio: (added + removed) / (draft + published). Pure."""
    tot = draft_w + published_w
    return round(100.0 * (added + removed) / tot, 1) if tot else 0.0


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:60]


def monthly_stats(rows: list[tuple]) -> list[dict]:
    """rows: (month 'YYYY-MM', section, pct_changed, headings list) -> per-month numbers. Pure."""
    by: dict = {}
    for month, section, pct, heads in rows:
        by.setdefault(month, []).append((section or "-", float(pct), heads or []))
    out = []
    for month in sorted(by, reverse=True):
        posts = by[month]
        pcts = [p for _, p, _ in posts]
        sec: dict = {}
        for s, p, _ in posts:
            sec.setdefault(s, []).append(p)
        sec_rank = sorted(((s, round(statistics.median(v), 1), len(v)) for s, v in sec.items()),
                          key=lambda x: -x[1])
        heads: dict = {}
        for _, _, hs in posts:
            for h in hs:
                heads[h["heading"]] = heads.get(h["heading"], 0) + h["added"] + h["removed"]
        out.append({"month": month, "posts": len(posts), "median_pct": round(statistics.median(pcts), 1),
                    "heavy": sum(1 for p in pcts if p > HEAVY_PCT),
                    "sections": sec_rank[:5],
                    "headings": sorted(heads.items(), key=lambda x: -x[1])[:5]})
    return out


# ---------------------------------------------------------------- the publish hook

def _under_test() -> bool:
    """True under pytest or any tests/test_*.py run: hooks must never write from a test."""
    return bool(os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules
                or Path(sys.argv[0] if sys.argv else "").name.startswith("test_"))


def record_publish(title: str, section: str, slug: str, draft: str, published: str) -> bool:
    """Called by nova_journal.publish_hugo after the post is written. Never raises (the caller also
    guards it); skipped under pytest. -> True when a row was written."""
    if _under_test():
        return False
    try:
        m = measure(draft, published)
        conn = connect(timeout_s=5)
        try:
            cur = conn.cursor()
            ensure_schema(cur)
            cur.execute("SELECT words_before, words_after, applied FROM ideal_reader_log WHERE title = %s "
                        "AND ts > now() - make_interval(mins => %s) ORDER BY id DESC LIMIT 1",
                        ((title or "")[:300], READER_WINDOW_MIN))
            r = cur.fetchone()
            cut = (r[0] - r[1]) if r and r[2] else 0
            expanded = r[0] if r else None
            insert_row(cur, {**m, "published_at": datetime.now().astimezone(), "slug": slug, "section": section,
                             "title": title, "origin": "publish", "expanded_words": expanded,
                             "reader_cut_words": cut})
        finally:
            conn.close()
        return True
    except Exception as e:  # noqa: BLE001
        log(f"record skipped: {e}")
        return False


# ---------------------------------------------------------------- log reconstruction

_TS = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] \[journal\] (.*)$")
_TAG_NEW = re.compile(r"^\[longform\] '(.*)' \[[^\]]*/([\w-]+)\] (\d+)w — ")
_TAG_OLD = re.compile(r"^\[longform\] '(.*)' \(([\w-]+)\) is (\d+) words")
_KEEP_DRAFT = re.compile(r"publishing (original|draft as written|as written)")
_NEW_LEN = re.compile(r"(?:expanded|tightened|stripped .*?|passed .*?:.*?) (\d+) -> (\d+)\s?w")
_PUBLISHED = re.compile(r"^Published: ([\w-]+)/(.+?)\.md$")


def parse_log(lines) -> list[dict]:
    """Journal log lines -> [{published_at, section, file, title50, draft, final}] for every post that
    went through the length policy. Pure (lines in, dicts out)."""
    out, cur = [], None
    for raw in lines:
        m = _TS.match(raw.rstrip("\n"))
        if not m:
            continue
        ts, msg = m.groups()
        t = _TAG_NEW.match(msg) or _TAG_OLD.match(msg)
        if t and (cur is None or cur["title50"] != t.group(1)[:50]):
            cur = {"title50": t.group(1)[:50], "draft": int(t.group(3)), "final": int(t.group(3))}
        if cur is None:
            continue
        if msg.startswith("[longform]"):
            if _KEEP_DRAFT.search(msg):
                cur["final"] = cur["draft"]
            else:
                n = _NEW_LEN.search(msg)
                if n:
                    cur["final"] = int(n.group(2))
            continue
        p = _PUBLISHED.match(msg)
        if p:
            if slugify(cur["title50"])[:30].strip("-") in p.group(2):
                out.append({**cur, "published_at": datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").astimezone(),
                            "section": p.group(1), "file": p.group(2)})
            cur = None
    return out


def hugo_words(section: str, file: str, root: Path = HUGO_CONTENT) -> int | None:
    """Words in the published body on disk (front matter, byline and dateline removed), or None."""
    f = root / section / f"{file}.md"
    try:
        text = f.read_text()
    except OSError:
        return None
    text = re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)
    text = re.sub(r"\A\s*\*Published [^\n]*\*\s*\n+", "", text, count=1)
    return len(words(strip_dateline(text)))


def reconstruct(cur, posts: list[dict], root: Path = HUGO_CONTENT) -> list[dict]:
    """Log posts -> razor_blade rows (origin='log'). Reads ideal_reader_log; writes nothing."""
    rows = []
    for p in posts:
        cur.execute("SELECT title, words_before, words_after, applied FROM ideal_reader_log "
                    "WHERE title LIKE %s AND ts BETWEEN %s AND %s ORDER BY ts DESC LIMIT 1",
                    (p["title50"].replace("%", r"\%").replace("_", r"\_") + "%",
                     p["published_at"] - timedelta(minutes=READER_WINDOW_MIN), p["published_at"] + timedelta(minutes=2)))
        r = cur.fetchone()
        cut = (r[1] - r[2]) if r and r[3] else 0
        title = r[0] if r else p["title50"]
        draft, final = p["draft"], p["final"]
        pub = hugo_words(p["section"], p["file"], root)
        if pub is None:
            pub = final - cut
        removed = max(0, draft - final) + cut
        added = max(0, final - draft)
        residual = pub - (draft + added - removed)      # appended blocks (+) or later edits (-) the log can't see
        added, removed = added + max(0, residual), removed + max(0, -residual)
        rows.append({"published_at": p["published_at"], "slug": p["file"], "section": p["section"], "title": title,
                     "origin": "log", "draft_words": draft, "expanded_words": final, "reader_cut_words": cut,
                     "published_words": pub, "words_added": added, "words_removed": removed, "similarity": None,
                     "pct_changed": estimate_pct(draft, pub, added, removed), "headings": []})
    return rows


# ---------------------------------------------------------------- db

def connect(timeout_s: int = 10):
    import psycopg2
    conn = psycopg2.connect(nova_dsn.pg_dsn("nova_ops", f"connect_timeout={timeout_s}"),
                            options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
    conn.autocommit = True
    return conn


def ensure_schema(cur) -> None:
    import nova_pg_schema
    nova_pg_schema.safe_ddl(cur, SCHEMA)


def insert_row(cur, r: dict) -> None:
    cur.execute("INSERT INTO razor_blade (published_at, slug, section, title, origin, draft_words, expanded_words, "
                "reader_cut_words, published_words, words_added, words_removed, similarity, pct_changed, headings) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (slug, published_at) DO NOTHING",
                (r["published_at"], r["slug"], r["section"], (r["title"] or "")[:300], r["origin"], r["draft_words"],
                 r["expanded_words"], r["reader_cut_words"], r["published_words"], r["words_added"],
                 r["words_removed"], r["similarity"], r["pct_changed"], json.dumps(r["headings"])))


def has_publish_row(cur, slug: str, at: datetime) -> bool:
    cur.execute("SELECT 1 FROM razor_blade WHERE slug = %s AND origin = 'publish' AND published_at BETWEEN %s AND %s",
                (slug, at - timedelta(hours=1), at + timedelta(hours=1)))
    return cur.fetchone() is not None


def table_exists(cur) -> bool:
    cur.execute("SELECT to_regclass('razor_blade') IS NOT NULL")
    return bool((cur.fetchone() or (False,))[0])


def db_month_rows(cur) -> list[tuple]:
    if not table_exists(cur):
        return []
    cur.execute("SELECT to_char(date_trunc('month', published_at), 'YYYY-MM'), section, pct_changed, headings "
                "FROM razor_blade ORDER BY published_at")
    return cur.fetchall()


def print_months(stats: list[dict]) -> None:
    print("Monthly (continuity review):")
    if not stats:
        print("  no posts measured yet")
    for m in stats:
        secs = ", ".join(f"{s} {p}% (n={n})" for s, p, n in m["sections"])
        print(f"  {m['month']}  posts {m['posts']:>3}  median changed {m['median_pct']:>5}%  "
              f">{HEAVY_PCT:.0f}%: {m['heavy']:>3}  sections: {secs}")
        if m["headings"]:
            print("           most-edited headings: " + ", ".join(f"{h} ({w}w)" for h, w in m["headings"]))


def print_rows(rows: list[dict], n: int = 15) -> None:
    for r in rows[-n:]:
        print(f"  {r['published_at']:%Y-%m-%d %H:%M} {r['section']:<11} draft {r['draft_words']:>5} -> "
              f"expanded {r['expanded_words'] or '-':>5} -> cut {r['reader_cut_words'] or 0:>4} -> published "
              f"{r['published_words']:>5}  +{r['words_added']}/-{r['words_removed']}  {r['pct_changed']:>5}%  "
              f"{(r['title'] or '')[:40]}")


def scheduled(cur, today: date | None = None) -> bool:
    """On the 1st: notify last month's numbers once (dedup per month). -> notified."""
    today = today or date.today()
    if today.day != 1:
        return False
    last = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    stats = [m for m in monthly_stats(db_month_rows(cur)) if m["month"] == last]
    if not stats:
        return False
    m = stats[0]
    from nova_notify import notify
    return bool(notify(f"Razor Blade {last}: median {m['median_pct']}% changed between draft and post, "
                       f"{m['heavy']} of {m['posts']} changed >{HEAVY_PCT:.0f}%",
                       body="Most-edited sections: " + ", ".join(f"{s} {p}%" for s, p, _ in m["sections"]),
                       level="info", category="razor_blade", dedup_key=f"razor_blade:{last}",
                       meta={"month": last, "posts": m["posts"], "median_pct": m["median_pct"], "heavy": m["heavy"]}))


def selftest() -> int:
    d = "## One\nThe cat sat on the mat.\n## Two\nIt was a very good day indeed."
    p = "*Somewhere · Thursday, October 15, 2026 · 9:00 AM · 70F*\n\n## One\nThe cat sat on the mat today.\n## Two\nIt was good."
    m = measure(d, p)
    assert m["words_added"] == 3 and m["words_removed"] == 6, m   # "mat." -> "mat today." is -1 +2
    assert 0 < m["pct_changed"] < 50 and m["headings"][0]["heading"] == "Two", m
    assert measure("same words", "same words")["pct_changed"] == 0.0
    assert estimate_pct(100, 150, 50, 0) == 20.0
    print("selftest OK")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scheduled", action="store_true", help="daily; on the 1st notify last month's numbers")
    ap.add_argument("--dry-run", nargs="?", const=str(JOURNAL_LOG), metavar="LOG",
                    help="reconstruct from the journal log and print; write nothing")
    ap.add_argument("--backfill-log", metavar="LOG", help="write the log reconstruction (origin='log')")
    ap.add_argument("--report", action="store_true", help="monthly numbers")
    ap.add_argument("--selftest", action="store_true", help="offline check of the pure functions")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    path = a.dry_run or a.backfill_log
    if path and not Path(path).is_file():
        ap.error(f"no such log: {path}")
    if not (a.scheduled or path or a.report):
        ap.print_help()
        return 0
    conn = connect()
    cur = conn.cursor()
    try:
        if a.report:
            print_months(monthly_stats(db_month_rows(cur)))
            return 0
        if path:
            with open(path, errors="replace") as fh:
                rows = reconstruct(cur, parse_log(fh))
            if a.dry_run:
                print(f"[dry-run] {len(rows)} posts reconstructed from {path}")
                print_rows(rows)
                months = [(r["published_at"].strftime("%Y-%m"), r["section"], r["pct_changed"], r["headings"])
                          for r in rows]
                print_months(monthly_stats(db_month_rows(cur) + months))
                print("  nothing written")
                return 0
            ensure_schema(cur)
            n = 0
            for r in rows:
                if not has_publish_row(cur, r["slug"], r["published_at"]):
                    insert_row(cur, r)
                    n += 1
            log(f"backfill: {n} of {len(rows)} reconstructed posts written (origin='log')")
            return 0
        ensure_schema(cur)
        sent = scheduled(cur)
        log(f"scheduled: {'notified last month' if sent else 'nothing to notify'}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
